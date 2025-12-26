import importlib
import logging
import contextlib
from collections import OrderedDict
from typing import Any, Optional, cast

from src.translator.translator import (
    config,
    dividir_texto,
    limpiar_traduccion,
    _split_text_by_token_limit,
    try_ensure_model_loaded,
    # runtime model objects accessed from translator module
)


class LocalTranslator:
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
        if not try_ensure_model_loaded():
            logging.warning("LocalTranslator: model unavailable, returning original text")
            return text

        # import runtime tokenizer/model from the main translator module to avoid duplication
        from src.translator import translator as _translator
        tokenizer = getattr(_translator, 'tokenizer', None)
        model = getattr(_translator, 'model', None)
        if tokenizer is None or model is None:
            logging.warning("LocalTranslator: tokenizer/model not loaded after ensure_model_loaded(), returning original")
            return text

        partes = dividir_texto(text)
        logging.debug("LocalTranslator.translate: input len=%d parts=%d", len(text), len(partes))
        traducciones = []
        try:
            max_tokens = int(getattr(tokenizer, 'model_max_length', 512) or 512)
        except Exception:
            max_tokens = 512

        for parte in partes:
            safe_chunks = _split_text_by_token_limit(parte, tokenizer, max_tokens)
            if len(safe_chunks) > 1:
                logging.debug("LocalTranslator.translate: part was split into %d chunks", len(safe_chunks))
            for chunk in safe_chunks:
                try:
                    if tokenizer is None or model is None:
                        raise RuntimeError("tokenizer or model unavailable")
                    tokens = tokenizer(chunk, return_tensors="pt", padding=True, truncation=True, max_length=max_tokens)
                    try:
                        import torch
                        device = getattr(_translator, 'model_device', None)
                        if device is not None:
                            for k, v in tokens.items():
                                try:
                                    tokens[k] = v.to(device)
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    gen_num_beams = int(config.get('translator_gen_num_beams', 2) or 2)
                    gen_max_length = int(config.get('translator_gen_max_length', self.__class__._max_length) or self.__class__._max_length)
                    gen_early = bool(config.get('translator_early_stopping', True))
                    ctx = (importlib.import_module('torch').no_grad() if getattr(_translator, '_HAVE_TORCH', False) else contextlib.nullcontext())
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

        from src.translator import translator as _translator
        tokenizer = getattr(_translator, 'tokenizer', None)
        model = getattr(_translator, 'model', None)
        if tokenizer is None or model is None:
            logging.warning("LocalTranslator.translate_batch: tokenizer/model not loaded, returning originals")
            return [t for t in texts]

        try:
            batch_size = int(config.get('translator_batch_size', 16) or 16)
            if batch_size < 1:
                batch_size = 16
        except Exception:
            batch_size = 16

        cache_size = int(config.get('translator_cache_size', getattr(self.__class__, '_cache_size', 1024)) or getattr(self.__class__, '_cache_size', 1024))
        if not hasattr(self.__class__, '_part_cache'):
            self.__class__._part_cache = OrderedDict()

        all_parts = []
        parts_per_text = []
        for ti, t in enumerate(texts):
            partes = dividir_texto(t) if t and t.strip() else ['']
            expanded = []
            try:
                max_tokens = int(getattr(tokenizer, 'model_max_length', 512) or 512)
            except Exception:
                max_tokens = 512
            for p in partes:
                try:
                    chunks = _split_text_by_token_limit(p, tokenizer, max_tokens)
                except Exception:
                    chunks = [p]
                for c in chunks:
                    expanded.append(c)
            parts_per_text.append(len(expanded))
            for pi, p in enumerate(expanded):
                all_parts.append((ti, pi, p))

        translated_parts: list[Optional[str]] = [None] * len(all_parts)
        idx = 0
        total = len(all_parts)

        def cache_set(key, value):
            cache: "OrderedDict[str, str]" = cast(OrderedDict, self._part_cache)
            if key in cache:
                cache.move_to_end(key)
                cache[key] = value
            else:
                cache[key] = value
                if len(cache) > cache_size:
                    try:
                        cache.popitem(last=False)
                    except TypeError:
                        first_key = next(iter(cache))
                        cache.pop(first_key, None)

        while idx < total:
            end = min(idx + batch_size, total)
            batch_tuples = all_parts[idx:end]
            batch_texts = []
            batch_positions = []
            for i, (_, _, p) in enumerate(batch_tuples):
                if p in self._part_cache:
                    translated_parts[idx + i] = self._part_cache[p]
                else:
                    batch_texts.append(p)
                    batch_positions.append(idx + i)

            if batch_texts:
                try:
                    tokens = tokenizer(batch_texts, return_tensors='pt', padding=True, truncation=True, max_length=512)
                    try:
                        import torch
                        device = getattr(_translator, 'model_device', None)
                        if device is not None:
                            for k, v in tokens.items():
                                try:
                                    tokens[k] = v.to(device)
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    ctx = (importlib.import_module('torch').no_grad() if getattr(_translator, '_HAVE_TORCH', False) else contextlib.nullcontext())
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
                    for bi, text_part in enumerate(batch_texts):
                        try:
                            tokens = tokenizer(text_part, return_tensors='pt', padding=True, truncation=True, max_length=512)
                            try:
                                import torch
                                device = getattr(_translator, 'model_device', None)
                                if device is not None:
                                    for k, v in tokens.items():
                                        try:
                                            tokens[k] = v.to(device)
                                        except Exception:
                                            pass
                            except Exception:
                                pass
                            ctx = (importlib.import_module('torch').no_grad() if getattr(_translator, '_HAVE_TORCH', False) else contextlib.nullcontext())
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

        results: list[Optional[str]] = [None] * len(texts)
        per_text_parts = [[] for _ in range(len(texts))]
        for i, (ti, pi, _) in enumerate(all_parts):
            per_text_parts[ti].append(translated_parts[i] or '')
        for ti in range(len(texts)):
            results[ti] = ' '.join([p for p in per_text_parts[ti] if p])
        return results

__all__ = ["LocalTranslator"]
