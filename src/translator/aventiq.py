import importlib
import os
import threading
import logging
from collections import OrderedDict
from typing import Any, List, cast

from src.translator.translator import (
    config,
    dividir_texto,
    limpiar_traduccion,
    _split_text_by_token_limit,
    _MODEL_LOADING_FORBIDDEN,
)


class AventIQTranslator:
    _pipeline = None
    _lock = threading.Lock()
    _device_index = -1
    _part_cache = OrderedDict()
    _cache_size = int(config.get('translator_cache_size', 1536) or 1536)
    _max_length = int(config.get('translator_gen_max_length', 1536) or 1536)
    _num_beams = int(config.get('translator_gen_num_beams', 4) or 4)
    _tokenizer = None

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
            cls._tokenizer = tokenizer
            pipeline_factory = cast(Any, hf_pipeline)
            cls._pipeline = pipeline_factory(
                task='translation_en_to_es',
                model=model,
                tokenizer=tokenizer,
                device=device_index,
                max_length=cls._max_length,
                num_beams=cls._num_beams,
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
            cache: "OrderedDict[str, str]" = cast(OrderedDict, cls._part_cache)
            if key in cache:
                try:
                    cls._part_cache.move_to_end(key)
                except Exception:
                    pass
            cache[key] = value
            if len(cache) > getattr(cls, '_cache_size', 512):
                try:
                    cls._part_cache.popitem(last=False)
                except TypeError:
                    first = next(iter(cache))
                    cache.pop(first, None)
                except Exception:
                    pass
        except Exception:
            pass

    @classmethod
    def _run_pipeline(cls, inputs):
        if cls._pipeline is None:
            raise RuntimeError('AventIQ pipeline no inicializado')
        max_length = getattr(cls, '_max_length', 1536)
        outputs = cls._pipeline(inputs, max_length=max_length, num_beams=getattr(cls, '_num_beams', 4))
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
        max_tokens = getattr(self.__class__, '_max_length', 1536)
        traducciones = []
        for parte in partes:
            cached = self.__class__._cache_get(parte)
            if cached is not None:
                traducciones.append(cached)
                continue
            try:
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
                    self.__class__._cache_set(chunk, clean)
                combined = ' '.join(translated_chunks)
                self.__class__._cache_set(parte, combined)
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


__all__ = ["AventIQTranslator"]
