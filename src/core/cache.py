import os
import json
import time
import threading
import logging

# File-level lock for cache and other file operations
file_lock = threading.Lock()

# Cache configuration (file-backed TTL cache)
# Allow overriding the cache directory via config (so it can be selectable in UI).
BASE_DIR = os.path.dirname(__file__)
CACHE_DIR = os.path.join(BASE_DIR, ".cache")
try:
    from src.core.config import config as _config
    _cfg_cache_dir = _config.get('cache_dir')
    if _cfg_cache_dir:
        CACHE_DIR = _cfg_cache_dir
except Exception:
    pass
CACHE_FILE = os.path.join(CACHE_DIR, "meta_cache.json")
CACHE_TTL = 60 * 60  # 1 hour


def _ensure_cache():
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        if not os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "w", encoding="utf-8") as cf:
                json.dump({}, cf)
    except Exception as e:
        logging.warning("No se pudo inicializar cache: %s", e)


def cache_get(key):
    try:
        _ensure_cache()
        with open(CACHE_FILE, "r", encoding="utf-8") as cf:
            data = json.load(cf)
        item = data.get(key)
        if not item:
            return None
        ts = item.get("ts", 0)
        if time.time() - ts > CACHE_TTL:
            return None
        return item.get("value")
    except Exception as e:
        logging.debug("Cache read error: %s", e)
        return None


def cache_set(key, value):
    try:
        _ensure_cache()
        with file_lock:
            with open(CACHE_FILE, "r", encoding="utf-8") as cf:
                try:
                    data = json.load(cf)
                except Exception:
                    data = {}
            data[key] = {"ts": time.time(), "value": value}
            with open(CACHE_FILE, "w", encoding="utf-8") as cf:
                json.dump(data, cf, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.debug("Cache write error: %s", e)
