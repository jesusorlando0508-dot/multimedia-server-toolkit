import importlib
import importlib.util
import os
import threading
import logging
from collections import OrderedDict
from typing import Any, Optional, cast

from src.translator.translator import (
    config,
    dividir_texto,
    limpiar_traduccion,
    _split_text_by_token_limit,
    _HAVE_TORCH,
)


class M2MTranslator:
    _tokenizer = None
    _model = None
    _lock = threading.Lock()
    _model_name = 'facebook/m2m100_418M'
    _device = None
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
                    repo_source = str(m2m_path)
                else:
                    logging.warning('Configured m2m_model_path does not exist: %s', expanded)

            if local_source:
                cls._tokenizer = M2M100Tokenizer.from_pretrained(local_source)
                cls._model = M2M100ForConditionalGeneration.from_pretrained(
                    local_source,
                    low_cpu_mem_usage=False,
                    device_map=None
                )
            else:
                cls._tokenizer = M2M100Tokenizer.from_pretrained(repo_source)
                cls._model = M2M100ForConditionalGeneration.from_pretrained(
                    repo_source,
                    low_cpu_mem_usage=False,
                    device_map=None
                )
            try:
                import torch
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
                cast(Any, cls._model).to(device)
                cls._device = device
            except Exception:
                cls._device = None

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
                cache: "OrderedDict[str, str]" = cast(OrderedDict, cls._part_cache)
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
                        except TypeError:
                            first = next(iter(cache))
                            cache.pop(first, None)
                        except Exception:
                            pass
        except Exception:
            pass

    def translate(self, text: str, src='en', tgt='es') -> str:
        if not isinstance(text, str):
            return ""
        if not text or not text.strip():
            return ""

        try:
            self.ensure_loaded()
        except Exception:
            logging.warning('M2MTranslator: model not available, returning original')
            return text

        if self.__class__._tokenizer is None or self.__class__._model is None:
            logging.warning('M2MTranslator: tokenizer or model missing after ensure_loaded')
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
        if texts is None:
            return []
        try:
            self.ensure_loaded()
        except Exception:
            logging.warning('M2MTranslator: model not available, returning originals')
            return [t for t in texts]

        if self.__class__._tokenizer is None or self.__class__._model is None:
            logging.warning('M2MTranslator: tokenizer or model missing after ensure_loaded')
            return [t for t in texts]

        try:
            batch_size = int(config.get('translator_batch_size', 8) or 8)
            if batch_size < 1:
                batch_size = 8
        except Exception:
            batch_size = 8

        results = [None] * len(texts)
        all_parts = []
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

        translated_parts: list[Optional[str]] = [None] * len(all_parts)
        idx = 0
        total = len(all_parts)

        while idx < total:
            end = min(idx + batch_size, total)
            batch = all_parts[idx:end]
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
                    assert tokenizer is not None and model is not None
                    try:
                        tokenizer.src_lang = src
                    except Exception:
                        pass

                    inputs = tokenizer(to_translate, return_tensors='pt', padding=True, truncation=True, max_length=max_tokens)
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
                    for tii, txtpart in enumerate(to_translate):
                        try:
                            tokenizer = self._tokenizer
                            model = self._model
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

        per_text_parts = {}
        for i, (ti, pi, _) in enumerate(all_parts):
            per_text_parts.setdefault(ti, []).append(translated_parts[i] or '')

        results = []
        for ti in range(len(texts)):
            parts = per_text_parts.get(ti, [])
            results.append(' '.join([p for p in parts if p]))

        return results


__all__ = ["M2MTranslator"]
