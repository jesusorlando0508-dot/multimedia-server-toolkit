import os
import json
import threading
import logging
import queue
import tkinter as tk
from tkinter import filedialog, simpledialog, messagebox, ttk
import requests, io
try:
    from natsort import natsorted
except Exception:
    # Fallback if natsort is not installed: use built-in sorted as best-effort
    def natsorted(seq):
        try:
            return sorted(seq)
        except Exception:
            return list(seq)
import re
from typing import Any
try:
    from transformers import MarianMTModel, MarianTokenizer
except Exception:
    MarianMTModel = None
    MarianTokenizer = None
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from PIL import Image, ImageTk
import time
import random

# logging: start with INFO, later adjust based on config
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# UI queue for thread-safe UI updates (centralized in app_state)
from src.core.app_state import ui_queue

# Locks and helpers
from src.core.cache import cache_get, cache_set, _ensure_cache, CACHE_FILE, CACHE_TTL, file_lock
from src.core.utils import (
    limpiar_nombre_archivo,
    descargar_imagen,
    buscar_imagen_local,
    dividir_texto,
    limpiar_traduccion,
    resumir_texto,
    GENRE_MAP,
)
diccionario_comun = GENRE_MAP

# Requests session with retries
def create_session_with_retries(total_retries=3, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504)):
    session = requests.Session()
    retries = Retry(total=total_retries, backoff_factor=backoff_factor, status_forcelist=status_forcelist, allowed_methods=frozenset(['GET','POST']))
    adapter = HTTPAdapter(max_retries=retries)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

session = create_session_with_retries()

# Load config and network helpers from modules
from src.core.config import config, save_config
try:
    # Honor debug level from config (set after config is loaded)
    lvl = str(config.get('debug_level', 'INFO')).upper()
    if lvl == 'DEBUG':
        logging.getLogger().setLevel(logging.DEBUG)
    else:
        logging.getLogger().setLevel(logging.INFO)
    logging.debug('Logging level set to %s via config', lvl)
except Exception:
    pass
from src.gui.config_gui import ensure_config_via_gui
from src.core.network import (
    API_BASE,
    TMDB_API_BASE,
    tmdb_search,
    tmdb_get_genres,
    tmdb_get_episodes,
    obtener_episodios,
    buscar_anime_por_titulo,
    get_episodes_for_anime,
)

# Translator helpers (moved to translator.py)
from src.translator.translator import translator_translate, translator_translate_batch, start_background_model_load
from src.core.config import save_config
import platform
import subprocess
try:
    import psutil as _psutil_mod
except Exception:
    _psutil_mod = None


def traducir_texto(texto: str, label_estado=None) -> str:
    if not texto or not texto.strip():
        return ""
    stripped = texto.strip()
    if stripped in diccionario_comun:
        return diccionario_comun[stripped]
    try:
        if label_estado:
            ui_queue.put(("label_text", label_estado, "Traduciendo sinopsis..."))
        resultado = translator_translate(texto, label_estado=label_estado)
        return limpiar_traduccion(resultado, ui_queue=ui_queue, label=label_estado)
    except Exception as e:
        logging.warning("Error en traducción: %s", e)
        return texto


def traducir_lista(textos, label_estado=None):
    """Traducir una lista de textos. Devuelve una lista con las traducciones en las mismas posiciones.
    Los valores vacíos o los que están en el diccionario común se mantienen tal cual.
    """
    resultados = []
    textos_a_traducir = []
    indices = []

    for i, t in enumerate(textos):
        if not t or not t.strip():
            resultados.append("")
        elif t.strip() in diccionario_comun:
            resultados.append(diccionario_comun[t.strip()])
        else:
            resultados.append(None)
            indices.append(i)
            textos_a_traducir.append(t)

    if not textos_a_traducir:
        return resultados

    try:
        total = len(textos_a_traducir)
        if label_estado:
            ui_queue.put(("label_text", label_estado, f"Traduciendo {total} capítulos..."))
        traducciones = translator_translate_batch(textos_a_traducir, label_estado=label_estado)
        for idx, trad in enumerate(traducciones):
            resultados[indices[idx]] = limpiar_traduccion(trad, ui_queue=ui_queue, label=label_estado)
    except Exception as e:
        logging.warning("Error en traducción de lista: %s", e)
        # En caso de error, dejar el texto original en las posiciones pendientes
        for pos, orig in zip(indices, textos_a_traducir):
            resultados[pos] = orig

    return resultados


# main() moved to `ui.py` (doct.py acts as thin entrypoint)
from src.gui.ui import main as ui_main
import tkinter as _tk
from tkinter import filedialog as _filedialog, simpledialog as _simpledialog
from src.core.config import save_config as _save_config


def run_resource_probe_once(cfg):
    """Detect system resources a single time before the first configuration run."""
    try:
        first_run = bool(cfg.get('first_run', True))
        already_done = bool(cfg.get('resource_probe_done', False))
    except Exception:
        first_run = True
        already_done = False
    if not first_run or already_done:
        return

    probe_root = None
    try:
        probe_root = tk.Tk()
        probe_root.withdraw()
        messagebox.showinfo(
            "Analizando recursos",
            "Analizando recursos del sistema para optimizar las traducciones."
            " Esta comprobación solo se ejecutará una vez.",
        )
    except Exception:
        pass
    finally:
        try:
            if probe_root:
                probe_root.destroy()
        except Exception:
            pass

    try:
        ui_queue.put(("debug_log", "Analizando recursos del sistema..."))
    except Exception:
        pass

    try:
        sys_info = detect_system_resources()
        if sys_info:
            cfg.setdefault('system_resources', {}).update(sys_info)
            tune_for_resources(cfg)
            cfg['resource_probe_done'] = True
            save_config(cfg)
    except Exception as e:
        logging.debug("Resource probe failed: %s", e)


def ensure_initial_paths():
    """Prompt the user for required input/output paths on first run.

    This function will only prompt if the corresponding keys are not set in
    `config` (e.g., 'pages_output_dir' and 'media_root_dir'). It runs a
    brief hidden Tk root to display platform-native dialogs.
    """
    try:
        cfg = config
    except Exception:
        return

    # If required config is missing, show the configuration GUI which is friendlier
    # and allows the user to persist the settings before continuing.
    try:
        run_resource_probe_once(cfg)
    except Exception:
        pass

    try:
        # Only prompt the GUI on first run
        first = bool(cfg.get('first_run', True))
        if first:
            new_cfg: dict[str, Any] = ensure_config_via_gui(cfg)
            if new_cfg and isinstance(new_cfg, dict):
                try:
                    # mark as configured so the dialog won't show again
                    new_cfg['first_run'] = False
                    new_cfg['config_initialized'] = True
                    cfg.clear()
                    cfg.update(new_cfg)
                    save_config(cfg)
                except Exception:
                    pass
            return
    except Exception:
        # Fallback to previous lightweight dialogs if GUI fails
        pass

    # nothing to do if config already has required keys


if __name__ == "__main__":
    # Prompt for important paths before launching the main UI so defaults are not used.
    try:
        ensure_initial_paths()
    except Exception:
        pass
    # Kick off background loading of the local translation model to avoid
    # blocking the UI on the first translation request.
    if config.get('preload_translator_on_start', True):
        try:
            start_background_model_load()
        except Exception:
            pass
    ui_main()


def detect_system_resources() -> dict:
    """Detect basic system resources: CPU count, total RAM (GB), GPU availability.

    Returns a dict suitable for storing in config['system_resources'].
    This function attempts to import `psutil` and `torch` when available.
    """
    info = {}
    try:
        # CPU cores
        try:
            info['cpu_count'] = os.cpu_count() or 1
        except Exception:
            info['cpu_count'] = 1
        # RAM
        try:
            psutil_mod = None
            try:
                import importlib
                psutil_mod = importlib.import_module('psutil')
            except Exception:
                psutil_mod = _psutil_mod
            if psutil_mod and hasattr(psutil_mod, 'virtual_memory'):
                vm = psutil_mod.virtual_memory()
                info['total_ram_gb'] = round(getattr(vm, 'total', 0) / (1024 ** 3), 2)
            else:
                info['total_ram_gb'] = None
        except Exception:
            info['total_ram_gb'] = None
        # GPU detection (torch.cuda or nvidia-smi)
        has_gpu = False
        gpu_name = None
        try:
            import torch
            if torch.cuda.is_available():
                has_gpu = True
                gpu_name = torch.cuda.get_device_name(0) if hasattr(torch.cuda, 'get_device_name') else 'cuda'
        except Exception:
            # try nvidia-smi
            try:
                out = subprocess.check_output(['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'], stderr=subprocess.DEVNULL, timeout=2)
                if out:
                    has_gpu = True
                    gpu_name = out.decode('utf-8').splitlines()[0].strip()
            except Exception:
                has_gpu = False
                gpu_name = None
        info['has_gpu'] = bool(has_gpu)
        info['gpu_name'] = gpu_name

        # crude estimate of translation speed profile
        try:
            if info.get('has_gpu'):
                profile = 'fast'
            else:
                ram = info.get('total_ram_gb') or 0
                cores = info.get('cpu_count') or 1
                if ram >= 16 and cores >= 4:
                    profile = 'moderate'
                else:
                    profile = 'slow'
        except Exception:
            profile = 'unknown'
        info['translation_profile'] = profile
    except Exception:
        return {}
    return info


def tune_for_resources(cfg: dict):
    """Adjust conservative defaults based on detected resources.

    Sets `translator_batch_size`, `max_generation_threads`, and `torch_num_threads`.
    """
    try:
        sysr = cfg.get('system_resources') or {}
        profile = sysr.get('translation_profile', 'unknown')
        has_gpu = bool(sysr.get('has_gpu'))
        if profile == 'fast':
            cfg['translator_batch_size'] = cfg.get('translator_batch_size', 16)
            cfg['max_generation_threads'] = cfg.get('max_generation_threads', 3)
            cfg['torch_num_threads'] = cfg.get('torch_num_threads', 8)
            cfg['torch_num_interop_threads'] = cfg.get('torch_num_interop_threads', 4)
            cfg['translator_device'] = cfg.get('translator_device', 'cuda' if has_gpu else 'cpu')
            cfg['translator_gen_num_beams'] = cfg.get('translator_gen_num_beams', 2)
            cfg['translator_gen_max_length'] = cfg.get('translator_gen_max_length', 2048)
        elif profile == 'moderate':
            cfg['translator_batch_size'] = cfg.get('translator_batch_size', 6)
            cfg['max_generation_threads'] = cfg.get('max_generation_threads', 2)
            cfg['torch_num_threads'] = cfg.get('torch_num_threads', 4)
            cfg['torch_num_interop_threads'] = cfg.get('torch_num_interop_threads', 2)
            cfg['translator_device'] = 'cpu'
            cfg['translator_gen_num_beams'] = 1
            cfg['translator_gen_max_length'] = min(2048, cfg.get('translator_gen_max_length', 2048))
        else:
            # weak hardware
            cfg['translator_batch_size'] = cfg.get('translator_batch_size', 2)
            cfg['max_generation_threads'] = cfg.get('max_generation_threads', 1)
            cfg['torch_num_threads'] = cfg.get('torch_num_threads', 2)
            cfg['torch_num_interop_threads'] = cfg.get('torch_num_interop_threads', 1)
            cfg['translator_device'] = 'cpu'
            cfg['translator_gen_num_beams'] = 1
            cfg['translator_gen_max_length'] = min(2048, cfg.get('translator_gen_max_length', 2048))
    except Exception:
        pass
