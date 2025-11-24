import logging
import threading
from typing import Optional, TYPE_CHECKING, Any, cast, List
from src.core.utils import dividir_texto, limpiar_traduccion
import importlib
import re
import time
import textwrap
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from src.core.config import config
try:
    from src.core.app_state import ui_queue
except Exception:
    ui_queue = None
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict
import functools
import contextlib
import subprocess
import shutil
import os

# Try to detect heavy ML deps (torch, sentencepiece). If missing, we will
# avoid attempting to load the local Marian model and fall back gracefully.
_HAVE_TORCH = False
_HAVE_SENTENCEPIECE = False
try:
    importlib.import_module('torch')
    _HAVE_TORCH = True
except Exception:
    _HAVE_TORCH = False
try:
    importlib.import_module('sentencepiece')
    _HAVE_SENTENCEPIECE = True
except Exception:
    _HAVE_SENTENCEPIECE = False

_MODEL_LOADING_FORBIDDEN = not (_HAVE_TORCH and _HAVE_SENTENCEPIECE)
if not _MODEL_LOADING_FORBIDDEN:
    try:
        from transformers import MarianMTModel, MarianTokenizer
    except Exception:
        # If transformers import fails, mark model loading as forbidden
        _MODEL_LOADING_FORBIDDEN = True

# Simple session for translator (separate from main session to avoid coupling)

def create_session_with_retries(total_retries=3, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504)):
    session = requests.Session()
    retries = Retry(total=total_retries, backoff_factor=backoff_factor, status_forcelist=status_forcelist, allowed_methods=frozenset(['GET','POST']))
    adapter = HTTPAdapter(max_retries=retries)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

session = create_session_with_retries()

# Translator model globals
if TYPE_CHECKING:
    # These imports are only for type checking when transformers is available
    from transformers import MarianTokenizer, MarianMTModel  # type: ignore

model_name = config.get("local_marian_model_name", "Helsinki-NLP/opus-mt-en-es")
model_lock = threading.Lock()
# tokenizer/model may be instances from `transformers` or None when deps missing
tokenizer: Optional['MarianTokenizer'] = None
model: Optional['MarianMTModel'] = None
_MODEL_UNAVAILABLE: bool = False
model_device: Any = None
_last_announced_backend: Optional[str] = None


def _announce_translator_backend(name: str):
    """Send a human-friendly notification when a translator backend is selected."""
    global _last_announced_backend
    if _last_announced_backend == name:
        return
    _last_announced_backend = name
    message = f"Traductor en uso: {name}"
    logging.info(message)
    try:
        if ui_queue is not None:
            ui_queue.put(("debug_process", message))
    except Exception:
        pass


def _resolve_local_marian_source() -> str:
    try:
        local_path = config.get('local_marian_model_path')
    except Exception:
        local_path = None
    if local_path:
        expanded = os.path.expanduser(str(local_path))
        if os.path.isdir(expanded):
            return expanded
    try:
        configured = config.get('local_marian_model_name')
        if configured:
            return configured
    except Exception:
        pass
    return model_name


def ensure_model_loaded():
    global tokenizer, model
    if _MODEL_LOADING_FORBIDDEN:
        raise RuntimeError("Local translation model not available in this environment (missing heavy deps)")
    if tokenizer is not None and model is not None:
        return
    with model_lock:
        if tokenizer is None or model is None:
            source = _resolve_local_marian_source()
            logging.info("Cargando modelo de traducción por primera vez: %s", source)
            try:
                tokenizer = MarianTokenizer.from_pretrained(source)
                model = MarianMTModel.from_pretrained(source)
                # place model on configured device if torch is available
                try:
                    import torch
                    device_pref = config.get('translator_device', 'cpu')
                    device = torch.device('cuda' if (device_pref == 'cuda' and torch.cuda.is_available()) else 'cpu')
                    # cast model to Any to avoid Pylance overload/signature issues
                    cast(Any, model).to(device)
                    # record model device separately (models don't expose a writable .device property)
                    global model_device
                    model_device = device
                except Exception:
                    # leave model on default device
                    pass
                logging.info("Modelo cargado")
            except Exception as _e:
                # mark unavailable so callers can fallback
                logging.warning("Local translation model load failed: %s", _e)
                global _MODEL_UNAVAILABLE
                _MODEL_UNAVAILABLE = True
                raise


def try_ensure_model_loaded() -> bool:
    """Attempt to ensure model loaded, but return False instead of raising.

    Callers can use this to detect availability without exception handling.
    """
    try:
        ensure_model_loaded()
        return True
    except Exception:
        return False


def _split_text_by_token_limit(text: str, tokenizer_obj, max_tokens: int):
    """Split `text` into pieces that when tokenized do not exceed `max_tokens`.
    Tries sentence-aware splitting first, then falls back to coarse char slices.
    Returns a list of text chunks (at least one)."""
    if not text:
        return [""]
    text = text.strip()
    # quick path: try tokenizer.encode if available
    try:
        if hasattr(tokenizer_obj, 'encode'):
            enc = tokenizer_obj.encode(text, add_special_tokens=False)
            if len(enc) <= max_tokens:
                return [text]
        else:
            # use tokenizer(...) to estimate length
            out = tokenizer_obj(text, return_tensors='pt', truncation=False)
            if out and 'input_ids' in out and out['input_ids'].size(1) <= max_tokens:
                return [text]
    except Exception:
        # ignore and try splitting
        pass

    # sentence split (handles English and punctuations commonly used)
    sents = re.split(r'(?<=[\.\?\!。！？])\s+', text)
    chunks = []
    current = ''
    for s in sents:
        if not s:
            continue
        candidate = (current + ' ' + s).strip() if current else s
        try:
            out = tokenizer_obj(candidate, return_tensors='pt', truncation=True, padding=False, max_length=max_tokens)
            # input_ids shape: (batch, seq_len)
            seq_len = out['input_ids'].size(1)
            if seq_len <= max_tokens:
                current = candidate
                continue
            else:
                if current:
                    chunks.append(current)
                # sentence itself too long => fallback to char-chunking
                approx = max(int(max_tokens * 2), 200)
                for i in range(0, len(s), approx):
                    chunks.append(s[i:i+approx])
                current = ''
        except Exception:
            # tokenizer failed -> fallback to coarse splits
            if current:
                chunks.append(current)
            approx = max(int(max_tokens * 2), 200)
            for i in range(0, len(s), approx):
                chunks.append(s[i:i+approx])
            current = ''
    if current:
        chunks.append(current)
    # Ensure non-empty
    if not chunks:
        logging.debug("translator._split_text_by_token_limit: no chunking performed for text len=%d (max_tokens=%d)", len(text), max_tokens)
        return [text]
    out_chunks = [c.strip() for c in chunks if c and c.strip()]
    logging.debug("translator._split_text_by_token_limit: split text len=%d into %d chunks (max_tokens=%d)", len(text), len(out_chunks), max_tokens)
    return out_chunks


def start_background_model_load():
    """Spawn a daemon thread to load the translation model in background.

    Call this during UI startup to avoid blocking the first translation call.
    """
    def _load():
        try:
            # Skip background load if environment lacks required libs
            if _MODEL_LOADING_FORBIDDEN:
                logging.debug("Background model load skipped (heavy deps missing)")
                return
            ensure_model_loaded()
        except Exception as e:
            logging.debug("Background model load failed: %s", e)

    t = threading.Thread(target=_load, daemon=True, name='translator-preload')
    t.start()


class TranslatorBase:
    def translate(self, text: str) -> str:
        raise NotImplementedError()

    def translate_batch(self, texts: list) -> list:
        raise NotImplementedError()


class LocalTranslator(TranslatorBase):
    # small class-level LRU cache for parts to avoid re-translating identical paragraphs
    _part_cache = OrderedDict()
    _cache_size = int(config.get('translator_cache_size', 1024) or 1024)
    try:
        _max_length = int(config.get('translator_gen_max_length', 2056) or 2056)
    except Exception:
        _max_length = 512
    def translate(self, text: str) -> str:
        if not text or not text.strip():
            return ""
        # Prefer a non-exception check for model availability
        if not try_ensure_model_loaded():
            logging.warning("LocalTranslator: model unavailable, returning original text")
            return text
        # Ensure global tokenizer/model are present before proceeding
        tokenizer = globals().get('tokenizer')
        model = globals().get('model')
        if tokenizer is None or model is None:
            logging.warning("LocalTranslator: tokenizer/model not loaded after ensure_model_loaded(), returning original")
            return text
        partes = dividir_texto(text)
        logging.debug("LocalTranslator.translate: input len=%d parts=%d", len(text), len(partes))
        traducciones = []
        # compute tokenizer max tokens conservatively
        try:
            max_tokens = int(getattr(tokenizer, 'model_max_length', 512) or 512)
        except Exception:
            max_tokens = 512

        for parte in partes:
            # split each part further so tokenizer won't truncate it
            safe_chunks = _split_text_by_token_limit(parte, tokenizer, max_tokens)
            if len(safe_chunks) > 1:
                logging.debug("LocalTranslator.translate: part was split into %d chunks", len(safe_chunks))
            for chunk in safe_chunks:
                try:
                    if tokenizer is None or model is None:
                        raise RuntimeError("tokenizer or model unavailable")
                    tokens = tokenizer(chunk, return_tensors="pt", padding=True, truncation=True, max_length=max_tokens)
                    # move tensors to model device if set
                    try:
                        import torch
                        device = globals().get('model_device', None)
                        if device is not None:
                            for k, v in tokens.items():
                                try:
                                    tokens[k] = v.to(device)
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    # run generation under no_grad when torch available
                    gen_num_beams = int(config.get('translator_gen_num_beams', 2) or 2)
                    gen_max_length = int(config.get('translator_gen_max_length', self.__class__._max_length) or self.__class__._max_length)
                    gen_early = bool(config.get('translator_early_stopping', True))
                    ctx = (importlib.import_module('torch').no_grad() if _HAVE_TORCH else contextlib.nullcontext())
                    with ctx:
                        if not hasattr(model, 'generate'):
                            raise RuntimeError('model.generate not available')
                        translated_tokens = model.generate(**tokens, num_beams=gen_num_beams, early_stopping=gen_early, max_length=gen_max_length, use_cache=True)
                    if hasattr(tokenizer, 'decode'):
                        traduccion = cast(Any, tokenizer).decode(translated_tokens[0], skip_special_tokens=True)
                    else:
                        traduccion = str(translated_tokens[0])
                    traduccion = limpiar_traduccion(traduccion)
                except Exception as e:
                    logging.debug("LocalTranslator.translate part failed, returning original part: %s", e)
                    traduccion = chunk
                traducciones.append(traduccion)
        return " ".join(traducciones)

    def translate_batch(self, texts: list) -> list:
        
        if not texts:
            return []
        if not try_ensure_model_loaded():
            logging.warning("LocalTranslator: model unavailable, returning originals")
            return [t for t in texts]

        tokenizer = globals().get('tokenizer')
        model = globals().get('model')
        if tokenizer is None or model is None:
            logging.warning("LocalTranslator.translate_batch: tokenizer/model not loaded, returning originals")
            return [t for t in texts]


        # allow configuring the internal generation batch size via config
        try:
            batch_size = int(config.get('translator_batch_size', 16) or 16)
            if batch_size < 1:
                batch_size = 16
        except Exception:
            batch_size = 16

        # small LRU cache for translated parts to avoid repeated work (class-level)
        cache_size = int(config.get('translator_cache_size', getattr(self.__class__, '_cache_size', 1024)) or getattr(self.__class__, '_cache_size', 1024))
        if not hasattr(self.__class__, '_part_cache'):
            self.__class__._part_cache = OrderedDict()

        # Split texts into parts and keep mapping to their parent index
        all_parts = []  # list of (text_idx, part_idx, part_text)
        parts_per_text = []
        for ti, t in enumerate(texts):
            partes = dividir_texto(t) if t and t.strip() else ['']
            expanded = []
            try:
                max_tokens = int(getattr(tokenizer, 'model_max_length', 512) or 512)
            except Exception:
                max_tokens = 512
            for p in partes:
                # further split by token limit
                try:
                    chunks = _split_text_by_token_limit(p, tokenizer, max_tokens)
                except Exception:
                    chunks = [p]
                for c in chunks:
                    expanded.append(c)
            parts_per_text.append(len(expanded))
            for pi, p in enumerate(expanded):
                all_parts.append((ti, pi, p))

        logging.debug("LocalTranslator.translate_batch: inputs=%d batch_size=%d cache_size=%d", len(texts), batch_size, cache_size)
        # Translate in batches with caching
        translated_parts: list[Optional[str]] = [None] * len(all_parts)
        idx = 0
        total = len(all_parts)

        def cache_set(key, value):
            # cast to plain dict for typing so Pylance accepts __setitem__ operations
            cache: dict = cast(dict, self._part_cache)
            if key in cache:
                cache.move_to_end(key)
                cache[key] = value
            else:
                cache[key] = value
                if len(cache) > cache_size:
                    cache.popitem(last=False)

        while idx < total:
            end = min(idx + batch_size, total)
            batch_tuples = all_parts[idx:end]
            # prepare batch_texts and keep the target positions
            batch_texts = []
            batch_positions = []
            for i, (_, _, p) in enumerate(batch_tuples):
                if p in self._part_cache:
                    translated_parts[idx + i] = self._part_cache[p]
                    logging.debug("LocalTranslator.translate_batch: cache hit for part (len=%d)", len(p))
                else:
                    batch_texts.append(p)
                    batch_positions.append(idx + i)

            if batch_texts:
                try:
                    if tokenizer is None or model is None:
                        raise RuntimeError('tokenizer or model unavailable')
                    tokens = tokenizer(batch_texts, return_tensors='pt', padding=True, truncation=True, max_length=512)
                    # move tensors to model device if set
                    try:
                        import torch
                        device = globals().get('model_device', None)
                        if device is not None:
                            for k, v in tokens.items():
                                try:
                                    tokens[k] = v.to(device)
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    ctx = (importlib.import_module('torch').no_grad() if _HAVE_TORCH else contextlib.nullcontext())
                    gen_num_beams = int(config.get('translator_gen_num_beams', 2) or 2)
                    gen_max_length = int(config.get('translator_gen_max_length', 512) or 512)
                    gen_early = bool(config.get('translator_early_stopping', True))
                    with ctx:
                        if not hasattr(model, 'generate'):
                            raise RuntimeError('model.generate not available')
                        translated_tokens = cast(Any, model).generate(**tokens, num_beams=gen_num_beams, early_stopping=gen_early, max_length=gen_max_length, use_cache=True)
                    decoded = []
                    for tkn in translated_tokens:
                        if hasattr(tokenizer, 'decode'):
                            decoded.append(cast(Any, tokenizer).decode(tkn, skip_special_tokens=True))
                        else:
                            decoded.append(str(tkn))
                    for bi, dec in enumerate(decoded):
                        clean = limpiar_traduccion(dec)
                        pos = batch_positions[bi]
                        translated_parts[pos] = clean
                        try:
                            cache_set(batch_texts[bi], clean)
                        except Exception:
                            pass
                except Exception as e:
                    logging.warning("translator.batch generation failed for batch %d-%d: %s", idx, end, e)
                    # fallback: handle sequentially
                    for bi, text_part in enumerate(batch_texts):
                        try:
                            if tokenizer is None or model is None:
                                raise RuntimeError('tokenizer or model unavailable')
                            tokens = tokenizer(text_part, return_tensors='pt', padding=True, truncation=True, max_length=512)
                            try:
                                import torch
                                device = globals().get('model_device', None)
                                if device is not None:
                                    for k, v in tokens.items():
                                        try:
                                            tokens[k] = v.to(device)
                                        except Exception:
                                            pass
                            except Exception:
                                pass
                            ctx = (importlib.import_module('torch').no_grad() if _HAVE_TORCH else contextlib.nullcontext())
                            with ctx:
                                if not hasattr(model, 'generate'):
                                    raise RuntimeError('model.generate not available')
                                translated_tokens = cast(Any, model).generate(**tokens, max_length=self.__class__._max_length)
                            if hasattr(tokenizer, 'decode'):
                                decoded = cast(Any, tokenizer).decode(translated_tokens[0], skip_special_tokens=True)
                            else:
                                decoded = str(translated_tokens[0])
                            clean = limpiar_traduccion(decoded)
                            pos = batch_positions[bi]
                            translated_parts[pos] = clean
                            try:
                                cache_set(text_part, clean)
                            except Exception:
                                pass
                        except Exception as ee:
                            logging.debug("translator.fallback failed for part: %s", ee)
                            pos = batch_positions[bi]
                            translated_parts[pos] = text_part
            idx = end

        # Reconstruct per-text translations
        results: list[Optional[str]] = [None] * len(texts)
        per_text_parts = [[] for _ in range(len(texts))]
        for i, (ti, pi, _) in enumerate(all_parts):
            per_text_parts[ti].append(translated_parts[i] or '')
        for ti in range(len(texts)):
            results[ti] = ' '.join([p for p in per_text_parts[ti] if p])
        return results


class DeepLTranslator(TranslatorBase):
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.endpoint = "https://api-free.deepl.com/v2/translate"

    def translate(self, text: str) -> str:
        if not text or not text.strip():
            return ""
        try:
            resp = session.post(self.endpoint, data={"auth_key": self.api_key, "text": text, "target_lang": "ES"}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            translations = data.get("translations") or []
            if not translations or not isinstance(translations, list):
                return text
            first = translations[0] or {}
            txt = first.get("text") if isinstance(first, dict) else None
            return limpiar_traduccion(txt or text)
        except Exception as e:
            logging.warning("DeepL translation failed: %s", e)
            return text

    def translate_batch(self, texts: list) -> list:
        resultados = []
        for t in texts:
            resultados.append(self.translate(t))
        return resultados



class M2MTranslator(TranslatorBase):
    """Optimized M2M100 translator for CPU-constrained systems.

    Features:
      - Uses local model dir if `config['m2m_model_path']` is set and exists.
      - Respects `translator_device` and `translator_batch_size` config keys.
      - Sets conservative generation params (num_beams=1, use_cache=True, max_length=256).
      - Mini LRU cache for translated parts (default capacity from config or 1024).
      - Splits long texts with `dividir_texto()`.
      - Limits torch threads for more stable CPU inference on old hardware.
    """
    _tokenizer = None
    _model = None
    _lock = threading.Lock()
    _model_name = 'facebook/m2m100_418M'
    _device = None
    # class-level LRU cache for parts
    _part_cache = OrderedDict()
    _max_length = int(config.get('translator_gen_max_length', 512) or 512)

    @classmethod
    def ensure_loaded(cls):
        if cls._model is not None and cls._tokenizer is not None:
            return
        with cls._lock:
            if cls._model is not None and cls._tokenizer is not None:
                return
            try:
                from transformers import M2M100ForConditionalGeneration, M2M100Tokenizer
            except Exception as e:
                logging.warning('M2M100 transformers not available: %s', e)
                raise

            logging.info('Cargando M2M100 model %s', cls._model_name)
            # Respect local dir or alternate repo if configured
            local_source = None
            repo_source = cls._model_name
            try:
                configured_repo = config.get('m2m_model_name') or repo_source
                if isinstance(configured_repo, str) and configured_repo.strip():
                    repo_source = configured_repo.strip()
            except Exception:
                pass
            try:
                m2m_path = config.get('m2m_model_path')
            except Exception:
                m2m_path = None

            if m2m_path:
                expanded = os.path.expanduser(str(m2m_path))
                if os.path.isdir(expanded):
                    local_source = expanded
                elif '/' in str(m2m_path) and not os.path.exists(expanded):
                    # Treat as repo id (user entered remote name in path)
                    repo_source = str(m2m_path)
                else:
                    logging.warning('Configured m2m_model_path does not exist: %s', expanded)

            if local_source:
                cls._tokenizer = M2M100Tokenizer.from_pretrained(local_source)
                cls._model = M2M100ForConditionalGeneration.from_pretrained(local_source)
            else:
                cls._tokenizer = M2M100Tokenizer.from_pretrained(repo_source)
                cls._model = M2M100ForConditionalGeneration.from_pretrained(repo_source)

            # If torch present, set sensible thread limits for older CPUs and move model
            try:
                import torch
                # Reduce thread usage on old multi-core systems for stability
                try:
                    torch.set_num_threads(int(config.get('torch_num_threads', 1) or 4))
                except Exception:
                    pass
                try:
                    torch.set_num_interop_threads(int(config.get('torch_num_interop_threads', 4) or 4))
                except Exception:
                    pass
                device_pref = config.get('translator_device', 'cpu')
                device = torch.device('cuda' if (device_pref == 'cuda' and torch.cuda.is_available()) else 'cpu')
                # cast model to Any to avoid type-checker overload issues
                cast(Any, cls._model).to(device)
                cls._device = device
            except Exception:
                # If torch not present or moving fails, leave model on default device
                cls._device = None

            # initialize class cache size from config
            try:
                cls._cache_size = int(config.get('translator_cache_size', 1024) or 1024)
                if cls._cache_size < 1:
                    cls._cache_size = 1024
            except Exception:
                cls._cache_size = 1024
            try:
                cls._max_length = int(config.get('translator_gen_max_length', cls._max_length) or cls._max_length)
            except Exception:
                pass

    @classmethod
    def _cache_get(cls, key):
        try:
            with cls._lock:
                if key in cls._part_cache:
                    cls._part_cache.move_to_end(key)
                    return cls._part_cache[key]
        except Exception:
            pass
        return None

    @classmethod
    def _cache_set(cls, key, value):
        try:
            with cls._lock:
                # cast to dict for typing; underlying is OrderedDict but we want
                # to avoid Pylance issues with __setitem__ overloads
                cache: dict = cast(dict, cls._part_cache)
                if key in cache:
                    try:
                        cls._part_cache.move_to_end(key)
                    except Exception:
                        pass
                    cache[key] = value
                else:
                    cache[key] = value
                    if len(cache) > getattr(cls, '_cache_size', 1024):
                        try:
                            cls._part_cache.popitem(last=False)
                        except Exception:
                            pass
        except Exception:
            pass

    def translate(self, text: str, src='en', tgt='es') -> str:
        # Requirement 5: validate input type
        if not isinstance(text, str):
            return ""
        if not text or not text.strip():
            return ""

        try:
            self.ensure_loaded()
        except Exception:
            logging.warning('M2MTranslator: model not available, returning original')
            return text

        # defensive: ensure tokenizer/model are present
        if self.__class__._tokenizer is None or self.__class__._model is None:
            logging.warning('M2MTranslator: tokenizer or model missing after ensure_loaded')
            return text

        # Ensure model/tokenizer loaded
        if getattr(self.__class__, '_tokenizer', None) is None or getattr(self.__class__, '_model', None) is None:
            logging.warning('M2MTranslator: tokenizer/model not loaded after ensure_loaded(), returning original')
            return text

        partes = dividir_texto(text)
        traducciones = []
        try:
            tokenizer = self._tokenizer
            model = self._model
            assert tokenizer is not None and model is not None
            max_tokens = int(getattr(tokenizer, 'model_max_length', self.__class__._max_length) or self.__class__._max_length)
        except Exception:
            tokenizer = self._tokenizer
            model = self._model
            max_tokens = self.__class__._max_length
        for parte in partes:
            # Check cache
            cached = self.__class__._cache_get(parte)
            if cached is not None:
                traducciones.append(cached)
                continue
            try:
                tokenizer = self._tokenizer
                model = self._model
                assert tokenizer is not None and model is not None
                try:
                    tokenizer.src_lang = src
                except Exception:
                    pass
                try:
                    safe_chunks = _split_text_by_token_limit(parte, tokenizer, max_tokens)
                except Exception:
                    safe_chunks = [parte]
                translated_chunks = []
                for chunk in safe_chunks:
                    chunk_cached = self.__class__._cache_get(chunk)
                    if chunk_cached is not None:
                        translated_chunks.append(chunk_cached)
                        continue
                    inputs = tokenizer(chunk, return_tensors='pt', truncation=True, padding=True, max_length=max_tokens)
                    try:
                        import torch
                        device = getattr(self.__class__, '_device', None)
                        if device is not None:
                            for k, v in inputs.items():
                                inputs[k] = v.to(device)
                    except Exception:
                        pass
                    ctx = (importlib.import_module('torch').no_grad() if _HAVE_TORCH else contextlib.nullcontext())
                    gen_num_beams = int(config.get('translator_gen_num_beams', 1) or 1)
                    gen_max_length = int(config.get('translator_gen_max_length', self.__class__._max_length) or self.__class__._max_length)
                    gen_kwargs = dict(num_beams=gen_num_beams, use_cache=True, max_length=gen_max_length)
                    with ctx:
                        forced_bos = None
                        try:
                            forced_bos = tokenizer.get_lang_id(tgt)
                        except Exception:
                            forced_bos = None
                        if forced_bos is not None:
                            gen = cast(Any, model).generate(**inputs, forced_bos_token_id=forced_bos, **gen_kwargs)
                        else:
                            gen = cast(Any, model).generate(**inputs, **gen_kwargs)
                    decoded = cast(Any, tokenizer).decode(gen[0], skip_special_tokens=True)
                    clean = limpiar_traduccion(decoded)
                    translated_chunks.append(clean)
                    try:
                        self.__class__._cache_set(chunk, clean)
                    except Exception:
                        pass
                combined = ' '.join(translated_chunks)
                try:
                    self.__class__._cache_set(parte, combined)
                except Exception:
                    pass
                traducciones.append(combined)
            except Exception as e:
                logging.debug('M2MTranslator.translate part failed, returning original part: %s', e)
                traducciones.append(parte)

        return ' '.join(traducciones)

    def translate_batch(self, texts: list, src='en', tgt='es') -> list:
        # validate input
        if texts is None:
            return []
        # Ensure model loaded
        try:
            self.ensure_loaded()
        except Exception:
            logging.warning('M2MTranslator: model not available, returning originals')
            return [t for t in texts]

        if self.__class__._tokenizer is None or self.__class__._model is None:
            logging.warning('M2MTranslator: tokenizer or model missing after ensure_loaded')
            return [t for t in texts]

        if getattr(self.__class__, '_tokenizer', None) is None or getattr(self.__class__, '_model', None) is None:
            logging.warning('M2MTranslator.translate_batch: tokenizer/model not loaded, returning originals')
            return [t for t in texts]

        # respect batch size
        try:
            batch_size = int(config.get('translator_batch_size', 8) or 8)
            if batch_size < 1:
                batch_size = 8
        except Exception:
            batch_size = 8

        results = [None] * len(texts)

        # Prepare all parts split and mapping
        all_parts = []  # (text_idx, part_idx, part_text)
        tokenizer = getattr(self.__class__, '_tokenizer', None)
        try:
            max_tokens = int(getattr(tokenizer, 'model_max_length', self.__class__._max_length) or self.__class__._max_length) if tokenizer else self.__class__._max_length
        except Exception:
            max_tokens = self.__class__._max_length
        for ti, t in enumerate(texts):
            if not isinstance(t, str):
                parts = ['']
            else:
                parts = dividir_texto(t) if t and t.strip() else ['']
            expanded = []
            for p in parts:
                if tokenizer is not None:
                    try:
                        chunks = _split_text_by_token_limit(p, tokenizer, max_tokens)
                    except Exception:
                        chunks = [p]
                else:
                    chunks = [p]
                expanded.extend(chunks)
            for pi, p in enumerate(expanded):
                all_parts.append((ti, pi, p))

        # translate parts in batches with cache checks
        translated_parts: list[Optional[str]] = [None] * len(all_parts)
        idx = 0
        total = len(all_parts)

        while idx < total:
            end = min(idx + batch_size, total)
            batch = all_parts[idx:end]

            # collect texts that need real translation and remember positions
            to_translate = []
            positions = []
            for i, (_, _, p) in enumerate(batch):
                cached = self.__class__._cache_get(p)
                if cached is not None:
                    translated_parts[idx + i] = cached
                else:
                    to_translate.append(p)
                    positions.append(idx + i)

            if to_translate:
                try:
                    tokenizer = self._tokenizer
                    model = self._model
                    # help the type checker: ensure tokenizer/model are not None
                    assert tokenizer is not None and model is not None
                    # set source lang
                    try:
                        tokenizer.src_lang = src
                    except Exception:
                        pass

                    inputs = tokenizer(to_translate, return_tensors='pt', padding=True, truncation=True, max_length=max_tokens)
                    # move tensors
                    try:
                        import torch
                        device = getattr(self.__class__, '_device', None)
                        if device is not None:
                            for k, v in inputs.items():
                                inputs[k] = v.to(device)
                    except Exception:
                        pass

                    ctx = (importlib.import_module('torch').no_grad() if _HAVE_TORCH else contextlib.nullcontext())
                    gen_num_beams = int(config.get('translator_gen_num_beams', 1) or 1)
                    gen_max_length = int(config.get('translator_gen_max_length', 512) or 512)
                    gen_kwargs = dict(num_beams=gen_num_beams, use_cache=True, max_length=gen_max_length)
                    with ctx:
                        forced_bos = None
                        try:
                            forced_bos = tokenizer.get_lang_id(tgt)
                        except Exception:
                            forced_bos = None
                        if forced_bos is not None:
                            gen = cast(Any, model).generate(**inputs, forced_bos_token_id=forced_bos, **gen_kwargs)
                        else:
                            gen = cast(Any, model).generate(**inputs, **gen_kwargs)

                    decoded = [cast(Any, tokenizer).decode(g, skip_special_tokens=True) for g in gen]
                    for di, dec in enumerate(decoded):
                        clean = limpiar_traduccion(dec)
                        pos = positions[di]
                        translated_parts[pos] = clean
                        try:
                            self.__class__._cache_set(to_translate[di], clean)
                        except Exception:
                            pass
                except Exception as e:
                    logging.warning('M2MTranslator.batch generation failed %s', e)
                    # fallback to per-item sequential
                    for tii, txtpart in enumerate(to_translate):
                        try:
                            tokenizer = self._tokenizer
                            model = self._model
                            # help the type checker: ensure tokenizer/model are not None
                            assert tokenizer is not None and model is not None
                            try:
                                tokenizer.src_lang = src
                            except Exception:
                                pass
                            tokens = tokenizer(txtpart, return_tensors='pt', truncation=True, padding=True, max_length=max_tokens)
                            try:
                                import torch
                                device = getattr(self.__class__, '_device', None)
                                if device is not None:
                                    for k, v in tokens.items():
                                        tokens[k] = v.to(device)
                            except Exception:
                                pass
                            ctx = (importlib.import_module('torch').no_grad() if _HAVE_TORCH else contextlib.nullcontext())
                            with ctx:
                                forced_bos = None
                                try:
                                    forced_bos = tokenizer.get_lang_id(tgt)
                                except Exception:
                                    forced_bos = None
                                if forced_bos is not None:
                                    g = cast(Any, model).generate(**tokens, forced_bos_token_id=forced_bos, **gen_kwargs)
                                else:
                                    g = cast(Any, model).generate(**tokens, **gen_kwargs)
                            dec = cast(Any, tokenizer).decode(g[0], skip_special_tokens=True)
                            clean = limpiar_traduccion(dec)
                            pos = positions[tii]
                            translated_parts[pos] = clean
                            try:
                                self.__class__._cache_set(txtpart, clean)
                            except Exception:
                                pass
                        except Exception as ee:
                            logging.debug('M2MTranslator.fallback failed for part: %s', ee)
                            pos = positions[tii]
                            translated_parts[pos] = txtpart

            idx = end

        # Reconstruct per-text results
        per_text_parts = {}
        for i, (ti, pi, _) in enumerate(all_parts):
            per_text_parts.setdefault(ti, []).append(translated_parts[i] or '')

        results = []
        for ti in range(len(texts)):
            parts = per_text_parts.get(ti, [])
            results.append(' '.join([p for p in parts if p]))

        return results


class AventIQTranslator(TranslatorBase):
    """Translator that leverages the AventIQ-AI EN->ES seq2seq model."""

    _pipeline = None
    _lock = threading.Lock()
    _device_index = -1
    _part_cache = OrderedDict()
    _cache_size = int(config.get('translator_cache_size', 1536) or 1536)
    _max_length = int(config.get('translator_gen_max_length', 1536) or 1536)  # FIX AVENTIQ
    _num_beams = int(config.get('translator_gen_num_beams', 4) or 4)  # FIX AVENTIQ
    _tokenizer = None  # FIX AVENTIQ

    @classmethod
    def _resolve_source(cls) -> str:
        path = config.get('aventiq_model_path') if isinstance(config, dict) else None
        try:
            if path:
                expanded = os.path.expanduser(str(path))
                if os.path.isdir(expanded):
                    return expanded
        except Exception:
            pass
        try:
            repo = config.get('aventiq_model_name')
            if repo:
                return repo
        except Exception:
            pass
        return 'AventIQ-AI/English-To-Spanish'

    @classmethod
    def ensure_loaded(cls):
        if cls._pipeline is not None:
            return
        if _MODEL_LOADING_FORBIDDEN:
            raise RuntimeError('AventIQTranslator: dependencias pesadas faltan (torch/sentencepiece).')
        with cls._lock:
            if cls._pipeline is not None:
                return
            try:
                from transformers import AutoModelForSeq2SeqLM, AutoTokenizer, pipeline as hf_pipeline
            except Exception as e:
                logging.warning('AventIQTranslator: transformers import failed: %s', e)
                raise
            source = cls._resolve_source()
            logging.info('Cargando modelo AventIQ desde %s', source)
            tokenizer = AutoTokenizer.from_pretrained(source)
            model = AutoModelForSeq2SeqLM.from_pretrained(source)
            device_index = -1
            try:
                if str(config.get('translator_device', 'cpu')).lower() == 'cuda':
                    import torch
                    if torch.cuda.is_available():
                        device_index = 0
            except Exception:
                device_index = -1
            cls._device_index = device_index
            # FIX AVENTIQ: refresh cache/max_length/num_beams before pipeline creation
            try:
                cls._cache_size = int(config.get('translator_cache_size', cls._cache_size) or cls._cache_size)
            except Exception:
                pass
            try:
                cls._max_length = int(config.get('translator_gen_max_length', cls._max_length) or cls._max_length)
            except Exception:
                pass
            try:
                cls._num_beams = int(config.get('translator_gen_num_beams', cls._num_beams) or cls._num_beams)
            except Exception:
                pass
            cls._tokenizer = tokenizer  # FIX AVENTIQ
            cls._pipeline = hf_pipeline(
                task='translation_en_to_es',
                model=model,
                tokenizer=tokenizer,
                device=device_index,
                max_length=cls._max_length,  # FIX AVENTIQ
                num_beams=cls._num_beams,  # FIX AVENTIQ
            )

    @classmethod
    def _cache_get(cls, key: str):
        try:
            if key in cls._part_cache:
                cls._part_cache.move_to_end(key)
                return cls._part_cache[key]
        except Exception:
            pass
        return None

    @classmethod
    def _cache_set(cls, key: str, value: str):
        try:
            cache: dict = cast(dict, cls._part_cache)
            if key in cache:
                cls._part_cache.move_to_end(key)
            cache[key] = value
            if len(cache) > getattr(cls, '_cache_size', 512):
                cls._part_cache.popitem(last=False)
        except Exception:
            pass

    @classmethod
    def _run_pipeline(cls, inputs):
        if cls._pipeline is None:
            raise RuntimeError('AventIQ pipeline no inicializado')
        max_length = getattr(cls, '_max_length', 1536)  # FIX AVENTIQ
        outputs = cls._pipeline(inputs, max_length=max_length, num_beams=getattr(cls, '_num_beams', 4))  # FIX AVENTIQ
        return outputs

    def translate(self, text: str) -> str:
        if not isinstance(text, str):
            return ""
        stripped = text.strip()
        if not stripped:
            return ""
        try:
            self.ensure_loaded()
        except Exception as e:
            logging.warning('AventIQTranslator unavailable: %s', e)
            return text
        partes = dividir_texto(stripped)
        tokenizer = getattr(self.__class__, '_tokenizer', None)
        max_tokens = getattr(self.__class__, '_max_length', 1536)  # FIX AVENTIQ
        traducciones = []
        for parte in partes:
            cached = self.__class__._cache_get(parte)
            if cached is not None:
                traducciones.append(cached)
                continue
            try:
                # FIX AVENTIQ: prevent truncation by chunking large texts
                safe_chunks = [parte]
                if tokenizer is not None and max_tokens:
                    try:
                        safe_chunks = _split_text_by_token_limit(parte, tokenizer, max_tokens)
                    except Exception:
                        safe_chunks = [parte]
                translated_chunks: List[str] = []
                for chunk in safe_chunks:
                    chunk_cached = self.__class__._cache_get(chunk)
                    if chunk_cached is not None:
                        translated_chunks.append(chunk_cached)
                        continue
                    result = self.__class__._run_pipeline(chunk)
                    if isinstance(result, list):
                        translated = result[0].get('translation_text') if result and isinstance(result[0], dict) else None
                    else:
                        translated = result
                    if not translated:
                        translated = chunk
                    clean = limpiar_traduccion(str(translated))
                    translated_chunks.append(clean)
                    self.__class__._cache_set(chunk, clean)  # FIX AVENTIQ
                combined = ' '.join(translated_chunks)
                self.__class__._cache_set(parte, combined)  # FIX AVENTIQ
                traducciones.append(combined)
            except Exception as e:
                logging.debug('AventIQTranslator part failed: %s', e)
                traducciones.append(parte)
        return ' '.join(traducciones)

    def translate_batch(self, texts: list) -> list:
        if not texts:
            return []
        resultados = []
        for t in texts:
            try:
                resultados.append(self.translate(t))
            except Exception:
                resultados.append(t)
        return resultados


def get_translator():
    try:
        backend = config.get("translator_backend", "local")
    except Exception:
        backend = "local"
    logging.debug("get_translator: selected backend=%s", backend)
    # Allow explicit backends, but support 'auto' for intelligent fallback
    order = []
    try:
        _conf = config
    except Exception:
        _conf = {}

    if backend == 'auto':
        # priority: Deepl (if key), M2M local, Local Marian, Colab endpoint (if provided)
        if _conf.get('deepl_api_key'):
            order.append('deepl')
        order.append('m2m100')
        order.append('aventiq')
        order.append('local')
        # Colab/Drive-based backend removed from project; do not add here
    else:
        order = [backend]

    # Try each strategy until one constructs successfully
    for strat in order:
        try:
            if strat == 'deepl':
                key = _conf.get('deepl_api_key', '')
                if key:
                    tr = DeepLTranslator(key)
                    # quick smoke test (non-destructive) if safe
                    try:
                        _ = tr.translate('Hola')
                    except Exception:
                        raise
                    _announce_translator_backend('DeepL API')
                    return tr
                continue
            if strat in ('m2m100', 'm2m'):
                try:
                    tr = M2MTranslator()
                    # ensure model can be loaded without forcing heavy load now
                    try:
                        tr.ensure_loaded()
                    except Exception as e:
                        logging.debug('get_translator: M2M unavailable: %s', e)
                        raise
                    _announce_translator_backend('M2M100 (local)')
                    return tr
                except Exception:
                    logging.debug('get_translator: M2M unavailable')
                    continue
            if strat == 'aventiq':
                try:
                    tr = AventIQTranslator()
                    tr.ensure_loaded()
                    _announce_translator_backend('AventIQ (local)')
                    return tr
                except Exception as e:
                    logging.debug('get_translator: AventIQ unavailable: %s', e)
                    continue
            if strat == 'local' or strat == 'marian':
                try:
                    # LocalTranslator will raise if model can't be loaded
                    tr = LocalTranslator()
                    # try a quick ensure to detect problems early
                    try:
                        ensure_model_loaded()
                    except Exception:
                        logging.debug('get_translator: local marian model unavailable')
                        raise
                    _announce_translator_backend('Marian local')
                    return tr
                except Exception:
                    continue
            # Colab backend intentionally omitted — removed from project
        except Exception:
            continue

    # final fallback: No-op translator
    class NoOpTranslator(TranslatorBase):
        def translate(self, text: str) -> str:
            return text or ""
        def translate_batch(self, texts: list) -> list:
            return [t for t in (texts or [])]
    _announce_translator_backend('NoOp translator (sin backend disponible)')
    return NoOpTranslator()


def translator_translate(text, label_estado=None):
    try:
        tr = get_translator()
        logging.debug("translator_translate: using %s for text len=%d", tr.__class__.__name__, len(text) if text else 0)
        # persistent debug trace
        try:
            debug_file = config.get('debug_log_file') or 'debug.log'
            with open(debug_file, 'a', encoding='utf-8') as df:
                df.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] translator_translate: backend={tr.__class__.__name__} text_len={len(text) if text else 0}\n")
        except Exception:
            pass
        res = tr.translate(text)
        logging.debug("translator_translate: result len=%d", len(res) if res else 0)
        try:
            debug_file = config.get('debug_log_file') or 'debug.log'
            with open(debug_file, 'a', encoding='utf-8') as df:
                df.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] translator_translate: result_len={len(res) if res else 0}\n")
        except Exception:
            pass
        return res
    except Exception as e:
        logging.warning("Translator error: %s", e)
        return text


def translator_translate_batch(texts, label_estado=None):
    try:
        if texts is None:
            logging.warning("Translator batch called with None, returning empty list")
            return []
        tr = get_translator()
        logging.debug("translator_translate_batch: using %s for %d texts", tr.__class__.__name__, len(texts))
        try:
            debug_file = config.get('debug_log_file') or 'debug.log'
            with open(debug_file, 'a', encoding='utf-8') as df:
                df.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] translator_translate_batch: backend={tr.__class__.__name__} texts={len(texts)}\n")
        except Exception:
            pass
        res = tr.translate_batch(texts)
        try:
            debug_file = config.get('debug_log_file') or 'debug.log'
            with open(debug_file, 'a', encoding='utf-8') as df:
                df.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] translator_translate_batch: result_count={len(res) if res else 0}\n")
        except Exception:
            pass
        try:
            logging.debug("translator_translate_batch: result count=%d", len(res) if res else 0)
        except Exception:
            pass
        return res
    except Exception as e:
        logging.warning("Translator batch error: %s", e)
        # On error, return the originals so callers can safely continue
        try:
            return [t for t in (texts or [])]
        except Exception:
            return []
