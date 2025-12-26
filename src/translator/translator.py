import importlib
import importlib.util
import os
import re
import threading
import logging
import contextlib
import time
from collections import OrderedDict
from typing import Any, Optional, List, cast, TYPE_CHECKING

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# project imports
from src.core.config import config
from src.core.app_state import ui_queue
from src.core.utils import dividir_texto, limpiar_traduccion
from src.translator import translation_cache as _translation_cache
from src.translator.translator_batcher import run_batched_translation

# expose cache helpers under expected names used by this module
cache_get = _translation_cache.get
cache_set = _translation_cache.set
cache_batch_get = _translation_cache.batch_get
cache_batch_set = _translation_cache.batch_set

# Try to detect heavy ML deps (torch, sentencepiece, transformers) without
# importing them at module import time. Use importlib.util.find_spec which
# does not execute module code and is cheap compared to importing torch.
_HAVE_TORCH = importlib.util.find_spec('torch') is not None
_HAVE_SENTENCEPIECE = importlib.util.find_spec('sentencepiece') is not None
_HAVE_TRANSFORMERS = importlib.util.find_spec('transformers') is not None

# If any required heavy dependency is missing, avoid eager model loading paths.
_MODEL_LOADING_FORBIDDEN = not (_HAVE_TORCH and _HAVE_SENTENCEPIECE and _HAVE_TRANSFORMERS)


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
                # import heavy transformers classes lazily to avoid startup overhead
                from transformers import MarianMTModel, MarianTokenizer
                tokenizer = MarianTokenizer.from_pretrained(source)
                model = MarianMTModel.from_pretrained(source)
                try:
                    import torch
                    device_pref = config.get('translator_device', 'cpu')
                    device = torch.device('cuda' if (device_pref == 'cuda' and torch.cuda.is_available()) else 'cpu')
                    cast(Any, model).to(device)
                    global model_device
                    model_device = device
                except Exception:
                    pass
                logging.info("Modelo cargado")
            except Exception as _e:
                logging.warning("Local translation model load failed: %s", _e)
                global _MODEL_UNAVAILABLE
                _MODEL_UNAVAILABLE = True
                raise


def try_ensure_model_loaded() -> bool:
    try:
        ensure_model_loaded()
        return True
    except Exception:
        return False


def _split_text_by_token_limit(text: str, tokenizer_obj, max_tokens: int):
    if not text:
        return [""]
    text = text.strip()
    try:
        if hasattr(tokenizer_obj, 'encode'):
            enc = tokenizer_obj.encode(text, add_special_tokens=False)
            if len(enc) <= max_tokens:
                return [text]
        else:
            out = tokenizer_obj(text, return_tensors='pt', truncation=False)
            if out and 'input_ids' in out and out['input_ids'].size(1) <= max_tokens:
                return [text]
    except Exception:
        pass

    sents = re.split(r'(?<=[\.?\!。！？])\s+', text)
    chunks = []
    current = ''
    for s in sents:
        if not s:
            continue
        candidate = (current + ' ' + s).strip() if current else s
        try:
            out = tokenizer_obj(candidate, return_tensors='pt', truncation=True, padding=False, max_length=max_tokens)
            seq_len = out['input_ids'].size(1)
            if seq_len <= max_tokens:
                current = candidate
                continue
            else:
                if current:
                    chunks.append(current)
                approx = max(int(max_tokens * 2), 200)
                for i in range(0, len(s), approx):
                    chunks.append(s[i:i+approx])
                current = ''
        except Exception:
            if current:
                chunks.append(current)
            approx = max(int(max_tokens * 2), 200)
            for i in range(0, len(s), approx):
                chunks.append(s[i:i+approx])
            current = ''
    if current:
        chunks.append(current)
    if not chunks:
        logging.debug("translator._split_text_by_token_limit: no chunking performed for text len=%d (max_tokens=%d)", len(text), max_tokens)
        return [text]
    out_chunks = [c.strip() for c in chunks if c and c.strip()]
    logging.debug("translator._split_text_by_token_limit: split text len=%d into %d chunks (max_tokens=%d)", len(text), len(out_chunks), max_tokens)
    return out_chunks


def start_background_model_load():
    def _load():
        try:
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
       
    else:
        order = [backend]

    # Try each strategy until one constructs successfully
    for strat in order:
        try:
            if strat == 'deepl':
                key = _conf.get('deepl_api_key', '')
                if key:
                    try:
                        from src.translator.deepl import DeepLTranslator  # imported lazily
                    except Exception:
                        raise
                    tr = DeepLTranslator(key)
                    # quick smoke test (non-destructive) if safe
                    try:
                        _ = tr.translate('Hola')
                    except Exception:
                        raise
                    _announce_translator_backend('DeepL API')
                    return tr
                continue
            if strat == 'argos':
                # Explicit Argos selection: create the translator but do not
                # block waiting for heavy imports. Load Argos in background
                # and return the translator instance immediately so the UI
                # remains responsive. While loading, `ArgosTranslator.translate`
                # will act as a No-Op (returning original text) until ready.
                try:
                    from src.translator.argos import ArgosTranslator  # imported lazily
                    tr = ArgosTranslator()

                    def _bg_load_argos(t: ArgosTranslator):
                        try:
                            ok = t.ensure_loaded_safe() if hasattr(t, 'ensure_loaded_safe') else False
                            if ok:
                                _announce_translator_backend('Argos Translate (local)')
                                try:
                                    if ui_queue is not None:
                                        ui_queue.put(("debug_process", "Argos: cargado"))
                                except Exception:
                                    pass
                            else:
                                logging.debug('get_translator: Argos background load failed')
                        except Exception as e:
                            logging.debug('get_translator: Argos background loader exception: %s', e)

                    t = threading.Thread(target=_bg_load_argos, args=(tr,), daemon=True, name='argos-loader')
                    t.start()
                    try:
                        if ui_queue is not None:
                            ui_queue.put(("debug_process", "Argos: iniciando carga en background..."))
                    except Exception:
                        pass
                    # Announce selection now (will be updated when load completes)
                    _announce_translator_backend('Argos Translate (local - cargando)')
                    return tr
                except Exception:
                    logging.debug('get_translator: Argos unavailable')
                    continue
            if strat in ('m2m100', 'm2m'):
                try:
                    try:
                        from src.translator.m2m100 import M2MTranslator  # imported lazily
                    except Exception:
                        raise
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
                    try:
                        from src.translator.aventiq import AventIQTranslator  # imported lazily
                    except Exception:
                        raise
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
                    try:
                        from src.translator.local import LocalTranslator  # imported lazily
                    except Exception:
                        raise
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
        target_lang = config.get('translator_target_lang', 'es') or 'es'
        # Try persistent cache first
        try:
            cached = cache_get(text, target_lang)
            if cached is not None:
                # If the cached translation is identical to the source text (likely from
                # a previous NoOp or failed translation), ignore the cache so we attempt
                # a fresh translation with the currently available backend.
                try:
                    def _norm(s: str) -> str:
                        if s is None:
                            return ""
                        return " ".join(str(s).strip().split()).lower()
                    if _norm(cached) == _norm(text):
                        logging.debug("translator_translate: cache hit equals source, ignoring cached entry")
                    else:
                        logging.debug("translator_translate: cache hit for text len=%d", len(text) if text else 0)
                        try:
                            if ui_queue is not None:
                                ui_queue.put(("translator_progress", "cache_hit", 1))
                        except Exception:
                            pass
                        return cached
                except Exception:
                    # on any failure comparing, be conservative and use cached value
                    return cached
        except Exception:
            pass

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
        # save to persistent cache
        try:
            cache_set(text, target_lang, res)
        except Exception:
            pass
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
        target_lang = config.get('translator_target_lang', 'es') or 'es'
        # Prefetch persistent cache
        try:
            cached_map = cache_batch_get(texts, target_lang)
        except Exception:
            cached_map = {t: None for t in texts}

        results: List[Optional[str]] = [None] * len(texts)
        to_translate = []
        to_translate_indices = []
        hits = 0
        for i, t in enumerate(texts):
            c = cached_map.get(t)
            if c is not None:
                results[i] = c # pyright: ignore[reportCallIssue, reportArgumentType]
                hits += 1
            else:
                to_translate.append(t)
                to_translate_indices.append(i)

        try:
            if ui_queue is not None:
                ui_queue.put(("translator_progress", "cache_summary", {"total": len(texts), "hits": hits, "misses": len(texts)-hits}))
        except Exception:
            pass

        if not to_translate:
            logging.debug("translator_translate_batch: all texts served from cache (%d/%d)", hits, len(texts))
            return results

        tr = get_translator()
        logging.debug("translator_translate_batch: using %s for %d texts (to_translate=%d)", tr.__class__.__name__, len(texts), len(to_translate))
        try:
            debug_file = config.get('debug_log_file') or 'debug.log'
            with open(debug_file, 'a', encoding='utf-8') as df:
                df.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] translator_translate_batch: backend={tr.__class__.__name__} texts={len(texts)}\n")
        except Exception:
            pass
        try:
            chunk_size = int(config.get('translator_batch_chunk_size', 20) or 20)
        except Exception:
            chunk_size = 20
        try:
            max_attempts = int(config.get('translator_batch_max_attempts', 3) or 3)
        except Exception:
            max_attempts = 3

        _raw_fallback = getattr(tr, 'translate', None)
        if callable(_raw_fallback):
            # Wrap to ensure signature (str)->str for the batch runner and satisfy type checkers
            def _fallback_fn(s: str) -> str:
                try:
                    return str(_raw_fallback(s))
                except Exception:
                    return s
            fallback_translate = _fallback_fn
        else:
            fallback_translate = None

        # Translate only the missing texts
        translated_missing = run_batched_translation(
            to_translate,
            translator=tr,
            label_estado=label_estado,
            chunk_size=chunk_size,
            max_attempts=max_attempts,
            fallback_translate=fallback_translate,
        )  # type: ignore[reportArgumentType]

        # persist newly translated results
        try:
            mapping = {t: translated_missing[i] for i, t in enumerate(to_translate)}
            cache_batch_set(mapping, target_lang)  # type: ignore[reportCallIssue,reportArgumentType]
        except Exception:
            pass

        # fill results
        for pos, txt in enumerate(to_translate_indices):
            results[txt] = translated_missing[pos] # pyright: ignore[reportCallIssue, reportArgumentType]

        res = results
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
