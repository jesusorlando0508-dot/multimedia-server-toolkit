"""Helpers to perform batched title translation with logging and UI feedback."""
from __future__ import annotations

import logging
from math import ceil
from typing import Callable, Sequence

try:
    from src.core.app_state import ui_queue
except Exception:  # pragma: no cover - UI queue optional
    ui_queue = None

DEFAULT_BATCH_SIZE = 20
MAX_ATTEMPTS = 3


def _emit(level: int, message: str) -> None:
    logging.log(level, message)
    if ui_queue is None:
        return
    channel = "debug_error" if level >= logging.WARNING else "debug_process"
    try:
        ui_queue.put((channel, message))
    except Exception:
        pass


def _update_label(label, message: str) -> None:
    if not label or ui_queue is None:
        return
    try:
        ui_queue.put(("label_text", label, message))
    except Exception:
        pass


def run_batched_translation(
    texts: Sequence[str],
    *,
    translator,
    label_estado=None,
    chunk_size: int = DEFAULT_BATCH_SIZE,
    max_attempts: int = MAX_ATTEMPTS,
    fallback_translate: Callable[[str], str] | None = None,
) -> list[str]:
    if not texts:
        return []

    chunk = max(1, int(chunk_size or DEFAULT_BATCH_SIZE))
    attempts = max(1, int(max_attempts or MAX_ATTEMPTS))
    total = len(texts)
    batches = ceil(total / chunk)
    results: list[str] = [""] * total

    for batch_idx in range(batches):
        start = batch_idx * chunk
        end = min(start + chunk, total)
        block = list(texts[start:end])
        header = f"[Batch {batch_idx + 1}/{batches}] Traduciendo títulos {start + 1}–{end} de {total}"
        _emit(logging.INFO, header)
        _update_label(label_estado, header)

        translated_block: list[str] | None = None
        for attempt_idx in range(1, attempts + 1):
            attempt_msg = f"DEBUG | batch {batch_idx + 1} | intento {attempt_idx} de {attempts}"
            _emit(logging.DEBUG, attempt_msg)
            try:
                translated = translator.translate_batch(block)
                if not isinstance(translated, list) or len(translated) != len(block):
                    raise ValueError("Respuesta inválida del traductor")
                translated_block = translated
                break
            except Exception as exc:
                fail_msg = f"Lote {batch_idx + 1}: intento {attempt_idx} falló ({exc})"
                _emit(logging.WARNING, fail_msg)

        if translated_block is None and fallback_translate is not None:
            fallback_msg = f"Usando fallback provider para batch {batch_idx + 1}"
            _emit(logging.INFO, fallback_msg)
            try:
                translated_block = [fallback_translate(item) for item in block]
            except Exception as exc:
                error_msg = f"Fallback provider falló en batch {batch_idx + 1}: {exc}"
                _emit(logging.ERROR, error_msg)

        if translated_block is None:
            translated_block = block

        results[start:end] = translated_block
        progress = f"Progreso global: {end}/{total}"
        _emit(logging.INFO, progress)
        _update_label(label_estado, progress)

    final_msg = f"INFO | Traducción completada ({total}/{total})"
    _emit(logging.INFO, final_msg)
    _emit(logging.INFO, "INFO | Generando página…")
    _emit(logging.INFO, "INFO | Actualizando JSON…")
    _update_label(label_estado, "Traducción completada")
    return results
