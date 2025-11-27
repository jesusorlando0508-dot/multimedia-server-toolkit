import json
import logging
import os
from pathlib import Path
from typing import Optional

# === Paths base ===
BASE = Path(__file__).resolve().parent
# Store configuration inside a hidden folder in the project root so
# everything seleccionado (rutas, credenciales) queda junto al repo.
APP_DIR = BASE / ".vista"
APP_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_PATH = APP_DIR / "config.json"
SECRETS_PATH = APP_DIR / ".secrets.json"
# Keep .env inside the hidden app dir so all selections are colocadas allí
ENV_PATH = APP_DIR / ".env"

SENSITIVE_KEYS = {
    "deepl_api_key",
    "tmdb_access_token",
    "dev_password",
}

# === Default Config (empieza en cero) ===
DEFAULT_CONFIG = {
    # Carpetas sin ruta predefinida
    "BASE_PAGES_DIR": "",
    # New unified media root (contains anime and movies folders)
    "media_root_dir": "",
    "json_link_prefix": "/pages/",
    # JSON y plantillas existentes en el proyecto
    "anime_json_path": "anime.json",
    "movies_json_path": "movies.json",
    "extractor_anime_json_path": "anime_info.json",
    "template_path": "template.html",
    "tmdb_overrides_path": "tmdb_overrides.json",
    "tmdb_gen_path": "tmdb_gen.json",
    "extractor_path": "extractor_html2.2.py",
    "cache_dir": ".cache",
    # Opciones del programa
    "translator_backend": "auto",
    "translator_backend_preference": "auto",
    "translator_auto_download_models": False,
    "translator_models_dir": "models",
    "local_marian_model_name": "Helsinki-NLP/opus-mt-en-es",
    "local_marian_model_path": "",
    "aventiq_model_name": "AventIQ-AI/English-To-Spanish",
    "aventiq_model_path": "",
    "translator_models_setup_done": False,
    "metadata_provider": "jikan",
    # default dev password so Dev Mode can be used without manual edits
    "dev_password": "",
    # mark whether initial configuration GUI was shown/saved
    "config_initialized": False,
    # debug level default (DEBUG to provide detailed module+translator logs)
    "debug_level": "DEBUG",
    # Mark whether this is the first run (used to show initial configuration and perform resource detection)
    "first_run": True,
    # Track whether system resource detection already ran
    "resource_probe_done": False,
    "system_resources": {},
    "auto_rename_videos": False,
    "auto_persist_mal": False,
    "check_folder_name": False,
    "auto_tag_mal_id": False,
    "auto_run_extractor": False,
    "debug_log_file": "debug.log",
    # Optional local path (or repo id) for an M2M100 model
    "m2m_model_path": "",
    # Default Hugging Face repo to use when no local path is provided
    "m2m_model_name": "facebook/m2m100_418M",
    # Web prefix where media will be served (server maps /media -> media_root_dir)
    "media_web_prefix": "/media/",
    # Defer writing aggregate JSON until end of automatic runs (improves consistency)
    "defer_json_write": True,
    # JSON backup retention in `.vista/backups`
    "json_backup_keep": 10,
    # Translator generation tuning
    "translator_gen_num_beams": 3,
    "translator_gen_max_length": 2056,
    "translator_early_stopping": True,
    "translator_device": "cpu",
    "torch_num_threads": 2,
    "torch_num_interop_threads": 2,
    "max_generation_threads": 1,
    "preload_translator_on_start": True,
    # Colab/Drive integration removed; related keys deprecated
}


def load_config():
    cfg = DEFAULT_CONFIG.copy()
    try:
        if CONFIG_PATH.exists():
            with CONFIG_PATH.open("r", encoding="utf-8") as f:
                cfg.update(json.load(f) or {})
        # cargar secrets encima
        if SECRETS_PATH.exists():
            with SECRETS_PATH.open("r", encoding="utf-8") as sf:
                sec = json.load(sf) or {}
                for k in SENSITIVE_KEYS:
                    if k in sec:
                        cfg[k] = sec[k]
    except Exception as e:
        logging.warning("No se pudo cargar config: %s", e)
    return cfg


def save_config(cfg: dict):
    try:
        if CONFIG_PATH.exists():
            try:
                os.chmod(CONFIG_PATH, 0o600)
            except Exception:
                pass
        # filtrar claves sensibles
        safe = {k: v for k, v in cfg.items() if k not in SENSITIVE_KEYS}
        CONFIG_PATH.write_text(json.dumps(safe, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logging.warning("No se pudo guardar config: %s", e)


def save_secrets(secrets: dict):
    try:
        data = {}
        if SECRETS_PATH.exists():
            data = json.loads(SECRETS_PATH.read_text(encoding="utf-8")) or {}
        for k, v in secrets.items():
            if k in SENSITIVE_KEYS:
                data[k] = v
        tmp = SECRETS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(SECRETS_PATH))
        try:
            os.chmod(str(SECRETS_PATH), 0o600)
        except Exception:
            pass
    except Exception as e:
        logging.warning("No se pudo guardar secrets: %s", e)


def save_env_key(key: str, value: Optional[str]):
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    if value is None:
        env.pop(key, None)
    else:
        env[key] = value
    ENV_PATH.write_text("\n".join(f"{k}={v}" for k, v in env.items()), encoding="utf-8")
    try:
        os.chmod(str(ENV_PATH), 0o600)
    except Exception:
        pass


config = load_config()