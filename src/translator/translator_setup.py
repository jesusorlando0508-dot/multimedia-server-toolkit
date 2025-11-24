"""Helpers to prepare translation models (download or manual selection).

This module shows simple Tk dialogs so that users can choose whether to
 auto-download the supported translation models (Marian, M2M100, AventIQ)
 or point the application to copies that they already downloaded manually.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, List, Tuple
import tkinter as tk
from tkinter import filedialog, messagebox

from src.core.config import save_config

try:
    from huggingface_hub import snapshot_download
except Exception:  # pragma: no cover - optional dependency
    snapshot_download = None  # type: ignore

MODEL_SPECS: List[Dict[str, str]] = [
    {
        "path_key": "local_marian_model_path",
        "name_key": "local_marian_model_name",
        "default_repo": "Helsinki-NLP/opus-mt-en-es",
        "label": "Marian (Helsinki-NLP)",
    },
    {
        "path_key": "m2m_model_path",
        "name_key": "m2m_model_name",
        "default_repo": "facebook/m2m100_418M",
        "label": "M2M100 418M",
    },
    {
        "path_key": "aventiq_model_path",
        "name_key": "aventiq_model_name",
        "default_repo": "AventIQ-AI/English-To-Spanish",
        "label": "AventIQ-AI",
    },
]

REQUIRED_COMMON = ("config.json", "tokenizer.json")
WEIGHT_FILES = ("pytorch_model.bin", "model.safetensors", "adapter_model.bin")


def _create_temp_root(parent: tk.Misc | None) -> Tuple[tk.Misc | None, bool]:
    if parent is not None:
        return parent, False
    try:
        root = tk.Tk()
        root.withdraw()
        return root, True
    except Exception:
        return None, False


def _human_size(num_bytes: int | float | None) -> str:
    if not num_bytes:
        return "0 B"
    step = 1024.0
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num_bytes)
    for unit in units:
        if size < step:
            return f"{size:.2f} {unit}"
        size /= step
    return f"{size:.2f} PB"


def _model_dir_from_config(cfg: Dict[str, object]) -> Path:
    base = cfg.get("translator_models_dir") or ""
    if not base:
        base = Path(__file__).resolve().parent / "models"
    return Path(base).expanduser().resolve()


def _verify_model_dir(path: Path) -> Tuple[bool, str, str]:
    if not path or not path.exists():
        return False, "Carpeta inexistente", "0 B"
    missing = [fname for fname in REQUIRED_COMMON if not (path / fname).exists()]
    if all(not list(path.glob(pattern)) for pattern in WEIGHT_FILES):
        missing.append("pesos (.bin/.safetensors)")
    size_bytes = 0
    for pattern in WEIGHT_FILES:
        for weight_file in path.glob(pattern):
            try:
                size_bytes += weight_file.stat().st_size
            except Exception:
                pass
    size_label = _human_size(size_bytes)
    if missing:
        return False, f"Faltan: {', '.join(missing)}", size_label
    return True, "Listo", size_label


def _snapshot_model(repo_id: str, target_dir: Path) -> Path:
    if snapshot_download is None:
        raise RuntimeError(
            "huggingface_hub no está instalado. Instala 'huggingface_hub' para descargar modelos automáticamente."
        )
    safe_name = repo_id.replace("/", "_")
    dest = target_dir / safe_name
    dest.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=repo_id,
        local_dir=str(dest),
        local_dir_use_symlinks=False,
        resume_download=True,
    )
    return dest


def _download_models(cfg: Dict[str, object]) -> List[str]:
    summaries: List[str] = []
    models_dir = _model_dir_from_config(cfg)
    models_dir.mkdir(parents=True, exist_ok=True)
    for spec in MODEL_SPECS:
        repo = str(cfg.get(spec["name_key"], spec["default_repo"])) or spec["default_repo"]
        try:
            local_dir = _snapshot_model(repo, models_dir)
            cfg[spec["path_key"]] = str(local_dir)
            ok, msg, size_label = _verify_model_dir(local_dir)
            summaries.append(f"{spec['label']}: {size_label} ({'OK' if ok else msg})")
        except Exception as exc:
            logging.warning("No se pudo descargar %s: %s", spec["label"], exc)
            summaries.append(f"{spec['label']}: Error al descargar ({exc})")
    return summaries


def _manual_select_models(cfg: Dict[str, object], parent: tk.Misc | None) -> List[str]:
    summaries: List[str] = []
    for spec in MODEL_SPECS:
        initial = cfg.get(spec["path_key"], "") or ""
        path = filedialog.askdirectory(
            parent=parent,
            title=f"Selecciona la carpeta del modelo {spec['label']}",
            initialdir=initial if initial else os.getcwd(),
        )
        if not path:
            summaries.append(f"{spec['label']}: Sin cambios (cancelado)")
            continue
        cfg[spec["path_key"]] = path
        ok, msg, size_label = _verify_model_dir(Path(path))
        summaries.append(f"{spec['label']}: {size_label} ({'OK' if ok else msg})")
    return summaries


def _needs_setup(cfg: Dict[str, object]) -> bool:
    if not cfg.get("translator_models_setup_done"):
        return True
    for spec in MODEL_SPECS:
        path = cfg.get(spec["path_key"])
        if not path or not Path(str(path)).exists():
            return True
    return False


def prompt_translator_model_setup(cfg: Dict[str, object], force: bool = False, parent: tk.Misc | None = None) -> bool:
    """Prompt the user to download or select translation models.

    Returns True if the configuration was updated.
    """
    if not force and not _needs_setup(cfg):
        return False

    root, created = _create_temp_root(parent)
    summaries: List[str] = []
    try:
        choice = True
        try:
            choice = messagebox.askyesno(
                "Modelos de traducción",
                "¿Deseas descargar automáticamente los modelos compatibles (Marian, M2M100 418M y AventIQ-AI)?\n"
                "Si seleccionas 'No' podrás indicar carpetas que ya tengan los modelos descargados.",
                parent=root,
            )
        except Exception:
            choice = True
        if choice:
            summaries = _download_models(cfg)
        else:
            summaries = _manual_select_models(cfg, parent=root)
        cfg["translator_models_setup_done"] = True
        save_config(cfg)
    finally:
        if created and root is not None:
            try:
                root.destroy()
            except Exception:
                pass

    if summaries:
        try:
            msg = "\n".join(summaries)
            messagebox.showinfo("Modelos configurados", msg, parent=parent or root)
        except Exception:
            pass
    return True


def verify_configured_models(cfg: Dict[str, object]) -> List[str]:
    """Return a short status summary for each configured model."""
    summaries: List[str] = []
    for spec in MODEL_SPECS:
        path = cfg.get(spec["path_key"], "") or ""
        if not path:
            summaries.append(f"{spec['label']}: sin ruta configurada")
            continue
        ok, msg, size_label = _verify_model_dir(Path(path))
        if ok:
            summaries.append(f"{spec['label']}: {size_label} (OK)")
        else:
            summaries.append(f"{spec['label']}: {msg}")
    return summaries
