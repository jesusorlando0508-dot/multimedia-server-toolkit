import json
import time
import os
from pathlib import Path
from typing import Optional, Dict, List, Any

from src.core.cache import file_lock
try:
    from src.core.config import APP_DIR
except Exception:
    APP_DIR = Path('.')
from src.core.config import config
try:
    from src.core.app_state import ui_queue
except Exception:
    ui_queue = None
import re
import hashlib

CACHE_NAME = "translation_cache.json"
DEFAULT_TTL = int(config.get('translator_cache_ttl_seconds', 60 * 60 * 24 * 30) or (60 * 60 * 24 * 30))


def _cache_path() -> Path:
    try:
        p = APP_DIR
    except Exception:
        p = Path('.')
    p.mkdir(parents=True, exist_ok=True)
    return p / CACHE_NAME


def _load_cache() -> Dict[str, Dict]:
    path = _cache_path()
    try:
        if not path.exists():
            return {}
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_cache(data: Dict[str, Dict]):
    path = _cache_path()
    try:
        with file_lock:
            tmp = path.with_suffix('.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(str(tmp), str(path))
    except Exception:
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def _normalize_text(text: str) -> str:
    """Normalize text for better cache hit-rate.

    Normalization steps:
    - strip leading/trailing whitespace
    - collapse internal whitespace to single spaces
    - lowercase (configurable)
    - remove surrounding quotes
    """
    if not text:
        return ""
    try:
        s = str(text).strip()
        # remove surrounding quotes
        s = re.sub(r"^[\"'`]+|[\"'`]+$", "", s)
        # collapse whitespace
        s = re.sub(r"\s+", " ", s)
        if bool(config.get('translator_cache_normalize_lower', True)):
            s = s.lower()
        return s
    except Exception:
        return str(text or "")


def _make_key(text: str, target_lang: str) -> str:
    """Create a compact cache key using SHA256 of normalized text."""
    try:
        norm = _normalize_text(text or "")
        # Use a short hex digest to avoid very long keys
        h = hashlib.sha256(norm.encode('utf-8')).hexdigest()
        return f"{target_lang}::{h}"
    except Exception:
        return f"{target_lang}::" + (text or "")


def get(text: str, target_lang: str) -> Optional[str]:
    if not config.get('translator_cache_enabled', True):
        return None
    try:
        key = _make_key(text, target_lang)
        data = _load_cache()
        entry = data.get(key)
        if not entry:
            try:
                if ui_queue is not None:
                    ui_queue.put(("translation_cache", "miss", text[:200]))
            except Exception:
                pass
            return None
        try:
            if ui_queue is not None:
                ui_queue.put(("translation_cache", "hit", text[:200]))
        except Exception:
            pass
        ts = entry.get('ts', 0)
        ttl = int(config.get('translator_cache_ttl_seconds', DEFAULT_TTL) or DEFAULT_TTL)
        if ttl > 0 and (time.time() - ts) > ttl:
            # expired
            try:
                with file_lock:
                    data.pop(key, None)
                    _save_cache(data)
            except Exception:
                pass
            return None
        return entry.get('value')
    except Exception:
        return None


def set(text: str, target_lang: str, value: str) -> None:
    if not config.get('translator_cache_enabled', True):
        return
    try:
        key = _make_key(text, target_lang)
        data = _load_cache()
        data[key] = {'ts': time.time(), 'value': value}
        _save_cache(data)
        try:
            if ui_queue is not None:
                ui_queue.put(("translation_cache", "set", text[:200]))
        except Exception:
            pass
    except Exception:
        pass


def batch_get(texts: List[str], target_lang: str) -> Dict[str, Optional[str]]:
    out = {}
    try:
        for t in texts:
            out[t] = get(t, target_lang)
    except Exception:
        for t in texts:
            out[t] = None
    return out


def batch_set(map_items: Dict[str, str], target_lang: str) -> None:
    try:
        data = _load_cache()
        for text, val in map_items.items():
            key = _make_key(text, target_lang)
            data[key] = {'ts': time.time(), 'value': val}
        _save_cache(data)
        try:
            if ui_queue is not None:
                ui_queue.put(("translation_cache", "batch_set", len(map_items)))
        except Exception:
            pass
    except Exception:
        pass


def get_stats() -> Dict[str, Any]:
    """Return simple statistics about the translation cache.

    Returns a dict with keys: `entries` (int), `sample_keys` (list of short previews).
    """
    try:
        data = _load_cache()
        entries = len(data)
        # provide a small preview of keys (first 5), decode partial info if possible
        sample = []
        for i, k in enumerate(data.keys()):
            if i >= 5:
                break
            try:
                sample.append(k)
            except Exception:
                sample.append(str(k)[:64])
        return {"entries": entries, "sample_keys": sample}
    except Exception:
        return {"entries": 0, "sample_keys": []}


def clear() -> Dict[str, Any]:
    """Clear the persistent translation cache file.

    Returns a summary dict similar to `get_stats()` representing the
    state before clearing (entries and sample_keys).
    """
    try:
        path = _cache_path()
        data = _load_cache()
        summary = {"entries": len(data), "sample_keys": list(data.keys())[:5]}
        # remove file under file lock to avoid races with writers
        try:
            with file_lock:
                if path.exists():
                    try:
                        path.unlink()
                    except Exception:
                        try:
                            # fallback to os.remove
                            os.remove(str(path))
                        except Exception:
                            pass
        except Exception:
            # best-effort: try unlink without lock
            try:
                if path.exists():
                    path.unlink()
            except Exception:
                try:
                    if path.exists():
                        os.remove(str(path))
                except Exception:
                    pass
        try:
            if ui_queue is not None:
                ui_queue.put(("translation_cache", "cleared", summary.get('entries')))
        except Exception:
            pass
        return summary
    except Exception:
        return {"entries": 0, "sample_keys": []}
