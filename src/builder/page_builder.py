import os
import json
import logging
import io
import time
import random
import threading
from PIL import Image, ImageTk
try:
    from natsort import natsorted as _natsorted
except Exception:
    def _natsorted(seq):
        try:
            return sorted(seq)
        except Exception:
            return list(seq)

# Expose a stable name for the rest of the module
natsorted = _natsorted  # type: ignore
from pathlib import Path

from src.core.config import config
from src.core.cache import file_lock
from src.core.utils import resumir_texto, descargar_imagen, buscar_imagen_local, limpiar_nombre_archivo, GENRE_MAP as diccionario_comun
from src.core.network import (
    session,
    get_episodes_for_anime,
    buscar_anime_por_titulo,
    buscar_anime_candidates,
    tmdb_search,
)
from src.core.app_state import gen_control
from src.translator.translator import translator_translate, translator_translate_batch
from src.synopsis_cleaner import clean_synopsis
from concurrent.futures import ThreadPoolExecutor, as_completed
from tkinter import simpledialog
import queue as _queue


def askstring_threadsafe(title, prompt, ui_queue=None, root=None, timeout=120, default=""):
    """Ask for a string from the user in a thread-safe way.

    - If called from the main thread, calls tkinter.simpledialog.askstring (parent=root if given).
    - If called from a worker thread and `ui_queue` is provided, posts a ('request_input', ...) message
      with a response Queue and waits up to `timeout` seconds for an answer. The UI must handle
      the 'request_input' message and put the answer into the provided response queue.
    - Otherwise falls back to returning `default`.
    """
    try:
        # If we're on the main thread, we can safely show the dialog
        if threading.current_thread() is threading.main_thread():
            if root:
                return simpledialog.askstring(title, prompt, parent=root) or default
            else:
                return simpledialog.askstring(title, prompt) or default

        # Worker thread path: try to request via ui_queue if available
        if ui_queue is not None:
            resp_q = _queue.Queue()
            try:
                ui_queue.put(("request_input", {"title": title, "prompt": prompt, "type": "string", "response_queue": resp_q}))
            except Exception:
                return default
            try:
                # Wait for the UI/main thread to respond
                res = resp_q.get(timeout=timeout)
                if res is None:
                    return default
                return res
            except Exception:
                return default

        # No UI queue and not main thread: cannot show dialog safely, fallback to default
        return default
    except Exception:
        return default
# Optional integration with renamer and extractor
try:
    from src.core import renombrar
except Exception:
    renombrar = None
try:
    # extractor filename includes dots, import by path
    import importlib.util
    # Allow extractor path to be configured via config; fallback to module-local path
    try:
        from src.core.config import config as _config
        extractor_path = _config.get('extractor_path') or os.path.join(os.path.dirname(__file__), 'extractor_html2.2.py')
    except Exception:
        extractor_path = os.path.join(os.path.dirname(__file__), 'extractor_html2.2.py')
    if os.path.exists(extractor_path):
        spec = importlib.util.spec_from_file_location('extractor_mod', extractor_path)
        extractor_mod = None
        # Be defensive: spec_from_file_location may return None and spec.loader may be None
        if spec is not None and getattr(spec, 'loader', None) is not None:
            try:
                extractor_mod = importlib.util.module_from_spec(spec)
                # spec.loader may be a loader object with exec_module; mypy/Pylance can't always see that
                spec.loader.exec_module(extractor_mod)  # type: ignore[attr-defined]
            except Exception:
                extractor_mod = None
    else:
        extractor_mod = None
except Exception:
    extractor_mod = None

logger = logging.getLogger(__name__)


def _is_video_file(name: str) -> bool:
    name = name.lower()
    return name.endswith('.mp4') or name.endswith('.mkv') or name.endswith('.avi') or name.endswith('.webm')


def _find_cover_in_folder(folder: str) -> str | None:
    """Try to find a cover image inside `folder`. Returns filename or None."""
    try:
        for candidate in os.listdir(folder):
            low = candidate.lower()
            if low.startswith('cover') or low.startswith('poster') or low.startswith('folder') or low.endswith('.jpg') or low.endswith('.jpeg') or low.endswith('.png') or low.endswith('.webp'):
                return candidate
    except Exception:
        return None
    return None


def scan_media_root(media_root: str) -> tuple[list[dict], list[dict]]:
    """Scan `media_root` and classify immediate subfolders into anime and movies.

    Returns (anime_list, movies_list) where each item is a simple dict:
      { 'id': folder_name, 'title': folder_name, 'folder': folder_name, 'num_videos': N, 'cover': cover_filename_or_none }

    The heuristic is simple:
      - If a subfolder contains 2 or more video files -> anime
      - If it contains exactly 1 video file -> movie
      - If it contains 0 video files -> ignored
    """
    anime = []
    movies = []
    try:
        base = Path(media_root)
        if not base.exists() or not base.is_dir():
            return anime, movies
        for entry in natsorted([p for p in base.iterdir() if p.is_dir()]):
            try:
                files = [f for f in natsorted(os.listdir(entry)) if os.path.isfile(os.path.join(entry, f))]
                video_files = [f for f in files if _is_video_file(f)]
                if not video_files:
                    continue
                cover = _find_cover_in_folder(str(entry))
                item = {
                    'id': entry.name,
                    'title': entry.name,
                    'folder': entry.name,
                    'num_videos': len(video_files),
                    'cover': cover
                }
                if len(video_files) >= 2:
                    anime.append(item)
                else:
                    movies.append(item)
            except Exception:
                continue
    except Exception:
        return anime, movies
    return anime, movies


def _write_json_atomic(path: str, data) -> bool:
    """Atomically write `data` (JSON-serializable) to `path`.

    - If the existing file content matches the new content, do nothing and return False.
    - If different, create a timestamped backup under `.vista/backups/` and keep only
      the last `json_backup_keep` backups (config, default 10).
    - Returns True if a write occurred, False if skipped because content identical.
    """
    try:
        p = Path(path)
        app_dir = Path(config.get('APP_DIR') or Path(__file__).resolve().parent / '.vista')
        backups_dir = app_dir / 'backups'
        try:
            backups_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        # prepare new content string for comparison
        new_text = json.dumps(data, indent=2, ensure_ascii=False)
        logging.debug("_write_json_atomic: preparing to write %s (new_len=%d)", p.as_posix(), len(new_text))
        # Also write a short persistent debug trace so users can inspect runtime behavior easily
        try:
            debug_file = config.get('debug_log_file') or 'debug.log'
            with open(debug_file, 'a', encoding='utf-8') as df:
                df.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] _write_json_atomic preparing to write {p.as_posix()} new_len={len(new_text)}\n")
        except Exception:
            pass

        # if existing file present and identical, skip write
        if p.exists():
            try:
                old_text = p.read_text(encoding='utf-8')
                logging.debug("_write_json_atomic: existing file %s (old_len=%d)", p.as_posix(), len(old_text))
                try:
                    debug_file = config.get('debug_log_file') or 'debug.log'
                    with open(debug_file, 'a', encoding='utf-8') as df:
                        df.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] _write_json_atomic existing {p.as_posix()} old_len={len(old_text)}\n")
                except Exception:
                    pass
                if old_text == new_text:
                    logging.debug("_write_json_atomic: no changes for %s, skipping write", p.as_posix())
                    return False
            except Exception as e:
                # continue with write if we cannot read old file
                logging.debug("_write_json_atomic: could not read existing file %s: %s", p.as_posix(), e)
                pass

            # create timestamped backup in backups_dir
            try:
                ts = time.strftime('%Y%m%d_%H%M%S')
                bak_name = f"{p.name}.bak.{ts}"
                bak_path = backups_dir / bak_name
                import shutil
                shutil.copy2(p, bak_path)
                logging.info("_write_json_atomic: created centralized backup %s for %s", bak_path.as_posix(), p.as_posix())
                try:
                    debug_file = config.get('debug_log_file') or 'debug.log'
                    with open(debug_file, 'a', encoding='utf-8') as df:
                        df.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] _write_json_atomic created backup {bak_path.as_posix()} for {p.as_posix()}\n")
                except Exception:
                    pass
                # prune old backups for this file
                try:
                    keep = int(config.get('json_backup_keep', 10) or 10)
                except Exception:
                    keep = 10
                # list backups for this basename
                files = sorted([f for f in backups_dir.iterdir() if f.is_file() and f.name.startswith(p.name + '.bak.')], key=lambda x: x.stat().st_mtime, reverse=True)
                for old in files[keep:]:
                    try:
                        old.unlink()
                    except Exception:
                        pass
            except Exception:
                logging.exception("_write_json_atomic: failed creating backup for %s", p.as_posix())

        # write tmp and replace atomically
        try:
            tmp = p.with_name(p.name + '.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(new_text)
            os.replace(str(tmp), str(p))
            logging.info("_write_json_atomic: successfully wrote %s (new_len=%d)", p.as_posix(), len(new_text))
            try:
                debug_file = config.get('debug_log_file') or 'debug.log'
                with open(debug_file, 'a', encoding='utf-8') as df:
                    df.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] _write_json_atomic wrote {p.as_posix()} new_len={len(new_text)}\n")
            except Exception:
                pass
            return True
        except Exception as e:
            logging.exception('Failed to write JSON %s: %s', path, e)
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            return False
    except Exception as e:
        logging.warning('Failed to write JSON %s: %s', path, e)
        return False


def build_and_write_media_indexes(media_root: str | None = None, write: bool = False) -> tuple[str, str]:
    """Scan media_root and optionally write/merge into anime.json and movies.json.

    When `write` is False, only performs a scan and returns paths where files
    would be written. When `write` is True, performs a safe merge/append
    operation using `_write_json_atomic` and avoids duplications.

    Returns (anime_path, movies_path) (either written paths or intended paths).
    """
    try:
        if not media_root:
            media_root = config.get('media_root_dir') or os.path.join(os.path.dirname(__file__), 'media_all')
        anime_list, movies_list = scan_media_root(media_root)

        anime_path = config.get('anime_json_path') or str(Path(__file__).with_name('anime.json'))
        movies_path = config.get('movies_json_path') or str(Path(__file__).with_name('movies.json'))

        if not write:
            return anime_path, movies_path

        # Build simple generated entries in the shape expected by merge function
        generated = []
        for a in anime_list:
            generated.append({
                'titulo': a.get('title') or a.get('id'),
                'ruta_anime': a.get('folder'),
                'num_videos': a.get('num_videos'),
                'type': 'anime'
            })
        for m in movies_list:
            generated.append({
                'titulo': m.get('title') or m.get('id'),
                'ruta_anime': m.get('folder'),
                'num_videos': m.get('num_videos'),
                'type': 'pelicula'
            })

        # Use the smarter merge function to update existing JSON files
        merge_and_write_generated_entries(generated)
        return anime_path, movies_path
    except Exception as e:
        logging.warning('build_and_write_media_indexes failed: %s', e)
        return '', ''


def merge_and_write_generated_entries(generated_entries: list) -> None:
    """Merge generated entries into the project's `anime.json` and `movies.json`.

    - Preserves existing entries that were not generated in this run (e.g. manual/test entries).
    - Replaces existing entries that match by `title`.
    - Writes anime.json and movies.json atomically via `_write_json_atomic`.
    """
    try:
        if not generated_entries:
            return
        # Separate by type
        anime_items = [e for e in generated_entries if (e.get('type') or 'anime') != 'pelicula']
        movie_items = [e for e in generated_entries if (e.get('type') or '').lower() == 'pelicula']

        # Resolve configured paths
        anime_path = config.get('anime_json_path') or str(Path(__file__).with_name('anime.json'))
        movies_path = config.get('movies_json_path') or str(Path(__file__).with_name('movies.json'))

        # helper to merge into a file
        def _merge_into(path_str, new_items):
            if not new_items:
                return
            p = Path(path_str)
            acquired = False
            try:
                try:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    if not p.exists():
                        p.write_text("[]", encoding='utf-8')
                except Exception:
                    pass
                try:
                    acquired = file_lock.acquire(timeout=10)
                except TypeError:
                    file_lock.acquire()
                    acquired = True
                existing = []
                if p.exists():
                    try:
                        with open(p, 'r', encoding='utf-8') as rf:
                            existing = json.load(rf) or []
                    except Exception:
                        existing = []
                if isinstance(existing, dict):
                    existing = [existing]
                if not isinstance(existing, list):
                    existing = []
                # perform smart merge: for each new_item, try to find existing by ruta_anime or title
                for new in new_items:
                    try:
                        matched = None
                        new_title = str(new.get('titulo') or new.get('title') or '').strip()
                        new_ruta = str(new.get('ruta_anime') or new.get('folder') or '').strip()
                        for ex in existing:
                            if not isinstance(ex, dict):
                                continue
                            ex_title = str(ex.get('titulo') or ex.get('title') or '').strip()
                            ex_ruta = str(ex.get('ruta_anime') or ex.get('folder') or '').strip()
                            if (new_ruta and ex_ruta and new_ruta == ex_ruta) or (new_title and ex_title and new_title == ex_title):
                                matched = ex
                                break
                        if matched:
                            # merge fields: only set fields that are missing or empty in existing
                            for k, v in (new.items() if isinstance(new, dict) else []):
                                try:
                                    if v is None:
                                        continue
                                    if k not in matched or (not matched.get(k) and matched.get(k) != 0):
                                        matched[k] = v
                                except Exception:
                                    continue
                        else:
                            existing.append(new)
                    except Exception:
                        # if merge fails for an item, append it to avoid data loss
                        try:
                            existing.append(new)
                        except Exception:
                            pass
                # ensure no duplicate titles/ruta remain (de-dup by ruta_anime then title)
                seen = set()
                cleaned = []
                for e in existing:
                    try:
                        key = (str(e.get('ruta_anime') or '').strip() or str(e.get('titulo') or e.get('title') or '').strip())
                        if key in seen or not key:
                            continue
                        seen.add(key)
                        cleaned.append(e)
                    except Exception:
                        continue
                _write_json_atomic(str(p), cleaned)
            except Exception as e:
                logging.warning('merge_and_write_generated_entries failed for %s: %s', path_str, e)
            finally:
                if acquired:
                    try:
                        file_lock.release()
                    except Exception:
                        pass

        _merge_into(anime_path, anime_items)
        _merge_into(movies_path, movie_items)
    except Exception as e:
        logging.warning('merge_and_write_generated_entries error: %s', e)


def traducir_texto(texto: str, label_estado=None, ui_queue=None):
    if not texto or not texto.strip():
        return ""
    stripped = texto.strip()
    if stripped in diccionario_comun:
        return diccionario_comun[stripped]
    try:
        # Notify UI about translation status (both general label and dedicated translation label)
        try:
            if ui_queue is not None:
                # update general status label if provided
                if label_estado:
                    ui_queue.put(("label_text", label_estado, "Traduciendo sinopsis..."))
                # update dedicated translation status line
                ui_queue.put(("translation_status", "Traduciendo sinopsis..."))
        except Exception:
            pass

        resultado = translator_translate(texto, label_estado=label_estado)
        # limpiar_traduccion está en utils; import aquí para evitar ciclo
        from src.core.utils import limpiar_traduccion
        out = limpiar_traduccion(resultado, ui_queue=ui_queue, label=label_estado)
        try:
            if ui_queue is not None:
                ui_queue.put(("translation_status", ""))
        except Exception:
            pass
        return out
    except Exception as e:
        logging.warning("Error en traducción: %s", e)
        return texto


def traducir_lista(textos, label_estado=None, ui_queue=None):
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
        if label_estado and ui_queue:
            ui_queue.put(("label_text", label_estado, f"Traduciendo {total} capítulos..."))
        try:
            if ui_queue is not None:
                ui_queue.put(("translation_status", f"Traduciendo {total} capítulos..."))
        except Exception:
            pass
        traducciones = translator_translate_batch(textos_a_traducir, label_estado=label_estado)
        try:
            if ui_queue is not None:
                ui_queue.put(("translation_status", ""))
        except Exception:
            pass
        # Defensive: if translator failed and returned an unexpected result (None or shorter list),
        # fall back to the original texts so we don't leave None entries in resultados.
        if not traducciones or len(traducciones) != len(textos_a_traducir):
            logging.warning("translate_batch returned unexpected result (len mismatch). Falling back to originals")
            traducciones = list(textos_a_traducir)
        from src.core.utils import limpiar_traduccion
        for idx, trad in enumerate(traducciones):
            resultados[indices[idx]] = limpiar_traduccion(trad, ui_queue=ui_queue, label=label_estado)
    except Exception as e:
        logging.warning("Error en traducción de lista: %s", e)
        for pos, orig in zip(indices, textos_a_traducir):
            resultados[pos] = orig

    return resultados


def generar_en_hilo_con_tipo(barra_progreso, label_estado, titulo_busqueda, carpeta_anime, anime, idioma="Japonés", label_meta=None, auto_confirm=False, label_titulo=None, label_sinopsis=None, etiqueta_imagen=None, ui_queue=None, root=None, skip_json_update=False):
    # Esta función procesa un solo anime (manual o detectado) y crea la página
    barra_progreso["value"] = 10
    # Web-facing prefix used in generated HTML (centralized in config)
    web_media_prefix = str(config.get('media_web_prefix') or '/media/')
    if not web_media_prefix.endswith('/'):
        web_media_prefix = web_media_prefix + '/'
    # Filesystem media root
    media_root_fs = Path(config.get('media_root_dir') or os.path.join(os.path.dirname(__file__), 'media_all'))
    # Honor stop/pause requests from UI
    try:
        if gen_control.stop_requested():
            if ui_queue:
                ui_queue.put(("label_text", label_estado, "⛔ Proceso detenido por el usuario."))
            return
        # If paused, block here until resumed or stopped
        gen_control.wait_if_paused()
    except Exception:
        pass

    if anime:
        # Preview + translations
        # respect pause/stop between heavy steps
        try:
            if gen_control.stop_requested():
                if ui_queue:
                    ui_queue.put(("label_text", label_estado, "⛔ Proceso detenido por el usuario."))
                return
            gen_control.wait_if_paused()
        except Exception:
            pass
        generar_preview(anime, carpeta_anime, label_titulo, label_sinopsis, etiqueta_imagen, label_meta, label_estado, ui_queue=ui_queue)
        if ui_queue:
            ui_queue.put(("label_text", label_estado, "Traduciendo sinopsis y géneros..."))
        # pause/stop check before translations
        try:
            if gen_control.stop_requested():
                if ui_queue:
                    ui_queue.put(("label_text", label_estado, "⛔ Proceso detenido por el usuario."))
                return
            gen_control.wait_if_paused()
        except Exception:
            pass

        # Try to fetch full metadata from Jikan (single /full endpoint) to reduce round-trips.
        try:
            # Defensive: ensure `anime` is a dict-like object to avoid optional-member warnings
            if anime is None:
                anime = {}
            # Only attempt to fetch full Jikan metadata when we have an explicit MAL id
            # or when the detected provider is explicitly 'jikan'. This avoids accidentally
            # passing a TMDB provider_id to Jikan which can produce incorrect replacements
            # of the `anime` object when the selected metadata provider was TMDB.
            mal_id_candidate = anime.get('mal_id')
            # If provider indicates jikan and there is a provider_id present, allow using it
            if not mal_id_candidate and (anime.get('provider') == 'jikan' or anime.get('provider') is None):
                mal_id_candidate = anime.get('provider_id')

            if mal_id_candidate and isinstance(mal_id_candidate, (int, str)):
                try:
                    import src.providers.provider_jikan as pj
                    t0 = time.time()
                    meta = pj.fetch_anime_metadata(str(mal_id_candidate))
                    t1 = time.time()
                    logger.debug("page_builder: fetch_anime_metadata(mal_id=%s) took %.2fs", mal_id_candidate, (t1 - t0))
                    if meta and meta.get('anime'):
                        # replace local 'anime' variable so the rest of the flow uses the richer object
                        anime = meta.get('anime')
                        # prefer genres returned by meta if available
                        raw_generos = meta.get('genres') or []
                        # keep themes available for merging later
                        themes_from_meta = meta.get('themes') or []
                except Exception as e:
                    logger.debug("page_builder: fetch_anime_metadata failed for %s: %s", mal_id_candidate, e)
        except Exception:
            pass

        sinopsis_trad = traducir_texto(anime.get("synopsis", "") or "Sinopsis no disponible.", label_estado=label_estado, ui_queue=ui_queue)
        # Defensive genre extraction
        raw_generos = []
        try:
            if isinstance(anime.get('genres'), list):
                for g in anime.get('genres'):
                    if isinstance(g, dict):
                        name = g.get('name') or g.get('title') or None
                        if name: raw_generos.append(name)
                    elif isinstance(g, str):
                        raw_generos.append(g)
        except Exception:
            raw_generos = []
        generos_trad = traducir_lista(raw_generos, label_estado=label_estado, ui_queue=ui_queue)

        # Contar archivos .mp4
        try:
            archivos = [f for f in natsorted(os.listdir(carpeta_anime)) if os.path.isfile(os.path.join(carpeta_anime, f)) and f.lower().endswith('.mp4')]
        except Exception:
            archivos = []
        num_videos = len(archivos)

        # Obtener títulos desde el proveedor (podría implicar paginación en Jikan)
        try:
            t_eps0 = time.time()
            provider_eps = get_episodes_for_anime(anime)
            t_eps1 = time.time()
            logger.debug("page_builder: get_episodes_for_anime took %.2fs for folder=%s", (t_eps1 - t_eps0), carpeta_anime)
        except Exception:
            provider_eps = []
        provider_titles = [ep.get("title") or None for ep in provider_eps] if provider_eps else []
        # Debug: log provider episodes count and a sample of titles
        try:
            logger.debug("page_builder: provider_eps count=%d for folder=%s", len(provider_eps) if provider_eps else 0, carpeta_anime)
            if provider_titles:
                sample = [t for t in provider_titles[:6]]
                logger.debug("page_builder: provider_titles sample (first 6)=%s", sample)
        except Exception:
            logger.debug("page_builder: error while logging provider titles")

        # Determinar el número efectivo de capítulos a mostrar.
        # Reglas:
        # - Si el tipo indica película/OVA/Special/ONA -> 1 capítulo
        # - Si el proveedor reporta episodios y no hay archivos locales -> usar la cuenta del proveedor
        # - Si hay archivos locales (.mp4) -> usar la cuenta de archivos (el usuario tiene esos ficheros)
        # - Si anime tiene campo 'episodes' válido -> usarlo como respaldo
        # - Si no hay información -> fallback a 10
        anime_type = None
        if anime:
            anime_type = (anime.get('media_type') or anime.get('type') or "").lower()
        episodes_field = None
        try:
            if anime and isinstance(anime.get('episodes'), int) and anime.get('episodes') > 0:
                episodes_field = int(anime.get('episodes'))
        except Exception:
            episodes_field = None

        # Treat as a single-item "movie-like" only when the type is an actual movie.
        # Do NOT treat OVA/ONA/Special as movie-like here because they can contain
        # multiple episodes; those should follow the provider/local counts.
        movie_like = False
        if anime_type:
            if 'movie' in anime_type:
                movie_like = True

        # Allow opt-in config to prefer provider counts over local files.
        # By default local files take precedence (safer for user's existing library).
        prefer_provider = config.get('prefer_provider_count', False)

        if movie_like:
            effective_count = 1
        elif provider_titles and num_videos == 0:
            # No local files -> use provider
            effective_count = len(provider_titles)
        elif num_videos > 0:
            # Local files exist.
            # If the user explicitly prefers provider counts, honor that
            if prefer_provider and provider_titles:
                effective_count = len(provider_titles)
            else:
                # New behavior: if provider reports more episodes than local files,
                # prefer showing the full list of provider episodes so the generated
                # page contains complete metadata. This avoids generating pages
                # with only one chapter when provider knows more.
                if provider_titles and len(provider_titles) > num_videos:
                    effective_count = len(provider_titles)
                else:
                    effective_count = num_videos
        elif episodes_field:
            effective_count = episodes_field
        else:
            effective_count = 10

        # Debug: log effective_count reason and values
        try:
            logger.debug("page_builder: computed effective_count=%d (movie_like=%s, provider_titles=%d, num_videos=%d, episodes_field=%s, prefer_provider=%s)",
                         effective_count, movie_like, len(provider_titles) if provider_titles else 0, num_videos, episodes_field, prefer_provider)
        except Exception:
            logger.debug("page_builder: error while logging effective_count")

        # Construir la lista de títulos respetando el effective_count
        titulos = []
        if provider_titles:
            # Use provider titles where possible, rellenar con genéricos si faltan
            for i in range(effective_count):
                if i < len(provider_titles) and provider_titles[i]:
                    titulos.append(provider_titles[i])
                else:
                    titulos.append(f"Capítulo {i+1}")
        else:
            # No hay títulos remotos; generar según effective_count
            titulos = [f"Capítulo {i+1}" for i in range(effective_count)]

        # Debug: log titles before translation
        try:
            logger.debug("page_builder: titulos (pre-translation) count=%d sample=%s", len(titulos), titulos[:8])
        except Exception:
            logger.debug("page_builder: error while logging titulos")

        # Allow pausing between title translations
        try:
            if gen_control.stop_requested():
                if ui_queue:
                    ui_queue.put(("label_text", label_estado, "⛔ Proceso detenido por el usuario."))
                return
            gen_control.wait_if_paused()
        except Exception:
            pass

        titulos_traducidos = traducir_lista(titulos, label_estado=label_estado, ui_queue=ui_queue) if titulos else []
        # carry any themes discovered from early metadata fetch into datos so crear_pagina can reuse without extra calls
        themes_from_meta_local = locals().get('themes_from_meta', []) or []
        datos = {
            "titulo": anime.get("title") or titulo_busqueda,
            "sinopsis": sinopsis_trad,
            "categoria": ", ".join(generos_trad),
            "ruta_anime": os.path.basename(carpeta_anime),
            "titulos_capitulos": titulos_traducidos,
            "ruta_portada": None,
            "idioma": idioma,
            # metadata para que crear_pagina pueda decidir tipo (anime vs pelicula)
            "provider": anime.get("provider") or ("tmdb" if anime.get("tmdb_id") or anime.get("provider_id") else None),
            "media_type": anime.get("media_type") or anime.get("type"),
            # preserve provider ids to allow page generation to enrich metadata (mal_id etc)
            "provider_id": anime.get("provider_id") or anime.get("tmdb_id"),
            "mal_id": anime.get("mal_id") or anime.get("malId") or anime.get("provider_id"),
            "themes": themes_from_meta_local
        }
        # include the full metadata object (if present) so crear_pagina can write richer JSON
        try:
            if isinstance(anime, dict):
                datos["metadata_full"] = anime
        except Exception:
            pass
    else:
        try:
            archivos = [f for f in natsorted(os.listdir(carpeta_anime)) if os.path.isfile(os.path.join(carpeta_anime, f)) and f.lower().endswith('.mp4')]
            num_videos = len(archivos)
        except Exception:
            num_videos = 0

        titulos_manual = [f"Capítulo {i+1}" for i in range(num_videos)] if num_videos > 0 else [f"Capítulo {i}" for i in range(1, 11)]
        # Use a thread-safe prompt helper: worker threads must not call tkinter dialogs directly.
        sinopsis_input = askstring_threadsafe("Sinopsis", "Ingresa la sinopsis del anime:", ui_queue=ui_queue, root=root, default="Sinopsis no disponible.")
        categorias_input = askstring_threadsafe("Categorías", "Categorías separadas por comas:", ui_queue=ui_queue, root=root, default="")
        datos = {
            "titulo": titulo_busqueda,
            "sinopsis": sinopsis_input or "Sinopsis no disponible.",
            "categoria": categorias_input or "",
            "ruta_anime": os.path.basename(carpeta_anime),
            "titulos_capitulos": titulos_manual,
            "ruta_portada": f"{web_media_prefix}default.jpg",
            "idioma": idioma
        }

    # Procesar portada
    img_local = buscar_imagen_local(carpeta_anime)
    url_portada = anime.get("images", {}).get("jpg", {}).get("large_image_url") if anime else ""
    web_media_prefix = str(config.get('media_web_prefix') or '/media/')
    if not web_media_prefix.endswith('/'):
        web_media_prefix = web_media_prefix + '/'
    if img_local:
        datos["ruta_portada"] = img_local
    elif url_portada:
        nombre_imagen = os.path.join(carpeta_anime, "1.jpg")
        if descargar_imagen(url_portada, nombre_imagen):
            datos["ruta_portada"] = f"{web_media_prefix}{os.path.basename(carpeta_anime)}/1.jpg".replace('\\', '/')
        else:
            datos["ruta_portada"] = f"{web_media_prefix}default.jpg".replace('\\', '/')
    else:
        datos["ruta_portada"] = f"{web_media_prefix}default.jpg".replace('\\', '/')

    # final pause/stop check before writing files
    try:
        if gen_control.stop_requested():
            if ui_queue:
                ui_queue.put(("label_text", label_estado, "⛔ Proceso detenido por el usuario."))
            return
        gen_control.wait_if_paused()
    except Exception:
        pass

    # Centralize pages output dir: prefer explicit config keys, with backwards compatibility
    carpeta_salida_cfg = config.get('pages_output_dir') or config.get('BASE_PAGES_DIR') or config.get('base_pages_dir')
    if not carpeta_salida_cfg:
        carpeta_salida_cfg = os.path.join(os.path.dirname(__file__), "pages")
        logging.warning("pages_output_dir/BASE_PAGES_DIR no está configurado — usando fallback '%s'. Configura la ruta en Ajustes.", carpeta_salida_cfg)
        try:
            config['pages_output_dir'] = carpeta_salida_cfg
            config['BASE_PAGES_DIR'] = carpeta_salida_cfg
            try:
                from src.core.config import save_config as _save_config
                _save_config(config)
            except Exception:
                pass
        except Exception:
            pass
    carpeta_salida = Path(carpeta_salida_cfg)
    nombre_pagina = limpiar_nombre_archivo(datos["titulo"]).replace(" ", "_").lower()
    # Debug: log final datos summary before creating page
    try:
        logger.debug("page_builder: creating page with datos keys=%s, titulos_count=%d", list(datos.keys()), len(datos.get('titulos_capitulos', [])))
    except Exception:
        logger.debug("page_builder: error while logging datos before create")
    res_entry = crear_pagina(datos, carpeta_salida, nombre_pagina, ui_queue=ui_queue, skip_json_update=skip_json_update)
    if skip_json_update:
        return res_entry
    if ui_queue:
        ui_queue.put(("progress", barra_progreso, 100))
        ui_queue.put(("label_text", label_estado, f"✅ Página {datos['titulo']} generada"))


def generar_automatico_en_hilo(barra_progreso, label_estado, carpetas, idioma_seleccionado, label_meta=None, label_titulo=None, label_sinopsis=None, etiqueta_imagen=None, ui_queue=None, root=None, run_options=None):
    total = len(carpetas)
    pendientes_manual = []
    # Defer JSON writes during batch if configured
    defer_flag = bool(config.get('defer_json_write', True))
    generated_entries = []
    # Determine worker thread count for generation (default 1 to keep behavior unchanged)
    # Determine number of worker threads for generation (default 1 to keep behavior unchanged)
    try:
        max_workers = int(config.get('max_generation_threads', 1) or 1)
        if max_workers < 1:
            max_workers = 1
    except Exception:
        max_workers = 1
    executor = ThreadPoolExecutor(max_workers=max_workers) if max_workers > 1 else None
    futures = []
    completed_count = 0

    t0_run = time.time()
    last_update_time = t0_run
    for idx, carpeta_anime in enumerate(carpetas, start=1):
        titulo_busqueda = os.path.basename(carpeta_anime)
        # Optionally run the renamer on the folder before processing
        try:
            ropts = run_options or {}
            rename_enabled = ropts.get('rename', False) or config.get('auto_rename_videos', False)
            rename_apply = ropts.get('rename_apply', False)
            rename_dry = ropts.get('rename_dry', False)
            if rename_enabled and renombrar:
                try:
                    if ui_queue:
                        renombrar.set_logger(lambda m: ui_queue.put(("debug_process", m)))
                    else:
                        renombrar.set_logger(lambda m: None)
                except Exception:
                    pass
                try:
                    # If dry-run requested, call with dry_run=True and do not apply changes
                    if rename_dry or not rename_apply:
                        summary = renombrar.procesar(carpeta_anime, auto_confirm=True, dry_run=True)
                        if ui_queue:
                            moves = len(summary.get('moves', []))
                            deletes = len(summary.get('deletes', []))
                            ui_queue.put(("debug_process", f"[dry-run] Renombrado planificado en {carpeta_anime}: {moves} moves, {deletes} deletes"))
                    else:
                        renombrar.procesar(carpeta_anime, auto_confirm=True, dry_run=False)
                        if ui_queue:
                            ui_queue.put(("debug_process", f"Renombrado automático realizado en {carpeta_anime}"))
                except Exception as e:
                    if ui_queue:
                        ui_queue.put(("debug_error", f"Error en renombrar.procesar: {e}"))
        except Exception:
            pass
        # Check for global stop/pause signals
        try:
            if gen_control.stop_requested():
                if ui_queue:
                    ui_queue.put(("label_text", label_estado, "⛔ Proceso detenido por el usuario."))
                break
            gen_control.wait_if_paused()
        except Exception:
            pass
        # Compute progress percent and ETA based on elapsed time so far
        try:
            processed = idx - 1
            elapsed = max(1e-6, time.time() - t0_run)
            avg_per_item = elapsed / max(1, processed) if processed > 0 else 0
            remaining = max(0, total - processed)
            eta_seconds = int(avg_per_item * remaining) if processed > 0 else 0
            if eta_seconds > 0:
                eta_str = time.strftime('%H:%M:%S', time.gmtime(eta_seconds))
            else:
                eta_str = '00:00:00'
            percent = int((processed / total) * 100) if total > 0 else 0
        except Exception:
            percent = int((idx-1)/total*100) if total > 0 else 0
            eta_str = '00:00:00'
            processed = idx - 1
        # Update UI with computed progress and ETA
        try:
            if ui_queue:
                ui_queue.put(("progress", barra_progreso, percent))
                ui_queue.put(("label_text", label_estado, f"[{processed}/{total}] {titulo_busqueda} — {percent}% — ETA: {eta_str}"))
            else:
                if label_estado:
                    try:
                        label_estado.config(text=f"[{processed}/{total}] {titulo_busqueda} — {percent}% — ETA: {eta_str}")
                    except Exception:
                        pass
            last_update_time = time.time()
        except Exception:
            pass
        if root:
            try:
                root.update_idletasks()
            except Exception:
                pass
        # Basic lookup via Jikan/TMDB
        # Prefer using any existing mal_id stored in the folder to avoid ambiguous searches
        try:
            # If the caller provided a content_type in run_options, honor it: 'anime'|'pelicula'|'serie'
            ropts = run_options or {}
            content_type = ropts.get('content_type', None)
            # If processing folders under a top-level 'peliculas' directory, default to TMDB movies
            # (user requested that movie/series lookups come exclusively from TMDB).
            try:
                parent_dir = os.path.basename(os.path.dirname(carpeta_anime)).lower()
            except Exception:
                parent_dir = ''
            default_context = None
            if parent_dir == 'peliculas':
                default_context = 'pelicula'

            # Load optional per-title overrides (tmdb_overrides.json next to this file)
            overrides = {}
            try:
                # Prefer configurable path from config, fallback to module-local file
                overrides_path = config.get('tmdb_overrides_path') or os.path.join(os.path.dirname(__file__), 'tmdb_overrides.json')
                if os.path.exists(overrides_path):
                    import json as _json
                    with open(overrides_path, 'r', encoding='utf-8') as _of:
                        overrides = _json.load(_of)
            except Exception:
                overrides = {}

            # If no explicit content_type provided, infer from folder name and files:
            # - If folder name contains season markers (season, temporada, s01) or there are multiple mp4 files,
            #   infer it's a 'serie' and force TV-only searches.
            # - Otherwise default to 'anime' (keeps historic behavior).
            if not content_type:
                # Check overrides first (exact folder title match)
                override_val = None
                try:
                    override_val = overrides.get(titulo_busqueda)
                except Exception:
                    override_val = None
                if override_val:
                    content_type = override_val
                else:
                    # If we have a default_context (e.g. parent is 'peliculas'), start from that.
                    if default_context:
                        content_type = default_context
                    else:
                        content_type = None
                    # Infer content type based ONLY on the folder name/title per user request.
                    import re as _re
                    name_lower = (titulo_busqueda or '').lower()
                    # Only infer 'serie' from folder name when the configured provider is TMDB
                    # or when default_context indicates we're under 'peliculas'. This avoids
                    # switching to TMDB when the user has chosen Jikan as metadata provider.
                    provider_pref = config.get('metadata_provider', 'jikan')
                    if _re.search(r"\bseason\b|\btemporada\b|\bs\d{1,2}\b", name_lower) and (provider_pref == 'tmdb' or default_context == 'pelicula'):
                        content_type = 'serie'
                    # If no inference and default_context present, content_type stays as default_context
                    if not content_type:
                        content_type = content_type or 'anime'

            # Decide effective provider: if processing under 'peliculas' or content_type explicitly pelicula/serie,
            # TMDB must be used exclusively. Otherwise respect configured provider.
            effective_provider = None
            try:
                configured_provider = config.get('metadata_provider', 'jikan')
            except Exception:
                configured_provider = 'jikan'
            # Decide effective_provider carefully:
            # - If content_type explicitly pelicula/serie -> TMDB
            # - Else if parent context is 'peliculas' AND configured provider is TMDB -> TMDB
            # - Otherwise honor the configured provider (this avoids calling TMDB when user chose Jikan)
            if content_type in ('pelicula', 'serie'):
                effective_provider = 'tmdb'
            elif default_context == 'pelicula' and configured_provider == 'tmdb':
                effective_provider = 'tmdb'
            else:
                effective_provider = configured_provider

            logging.debug("generar_automatico: folder=%s content_type=%s configured_provider=%s effective_provider=%s", titulo_busqueda, content_type, configured_provider, effective_provider)

            if content_type == 'anime':
                try:
                    anime = buscar_anime_por_titulo(titulo_busqueda, folder_path=carpeta_anime, provider_override=effective_provider)
                except TypeError:
                    # backward compatible: call without provider_override if older signature
                    anime = buscar_anime_por_titulo(titulo_busqueda)
            elif content_type == 'pelicula':
                anime = tmdb_search(titulo_busqueda, media_preference='movie', allow_when_config_is_jikan=True)
            else:
                # For 'serie' we MUST only search TV to avoid mixing with similarly-titled movies
                anime = tmdb_search(titulo_busqueda, media_preference='tv', allow_when_config_is_jikan=True)
        except Exception:
            # Fallback to original lookup if something goes wrong
            try:
                anime = buscar_anime_por_titulo(titulo_busqueda, folder_path=carpeta_anime)
            except Exception:
                anime = buscar_anime_por_titulo(titulo_busqueda)

        # Optional disambiguation and auto-tagging controlled by run_options or config
        try:
            ropts = run_options or {}
            check_folder = ropts.get('check_folder_name', config.get('check_folder_name', False))
            auto_tag_mal = ropts.get('auto_tag_mal_id', config.get('auto_tag_mal_id', False))
        except Exception:
            check_folder = config.get('check_folder_name', False)
            auto_tag_mal = config.get('auto_tag_mal_id', False)

        if check_folder:
            try:
                candidates = buscar_anime_candidates(titulo_busqueda, limit_per_variant=8, overall_limit=20)
                if candidates:
                    from difflib import SequenceMatcher
                    from src.core.utils import normalize_folder_name_for_search
                    folder_norm = os.path.basename(titulo_busqueda).strip().lower()
                    variants = normalize_folder_name_for_search(titulo_busqueda)
                    best_cand = None
                    best_score = 0.0
                    for cand in candidates:
                        cands = []
                        cands.extend([str(cand.get('title') or '').lower(), str(cand.get('title_english') or '').lower(), str(cand.get('title_japanese') or '').lower()])
                        try:
                            for t in (cand.get('titles') or []):
                                if isinstance(t, dict):
                                    cands.append((t.get('title') or '').lower())
                                else:
                                    cands.append(str(t).lower())
                        except Exception:
                            pass
                        joined = ' '.join([c for c in cands if c])
                        sim = SequenceMatcher(None, folder_norm, joined).ratio()
                        variant_boost = 0.0
                        for v in variants:
                            if v and v.lower() in joined:
                                variant_boost = 0.25
                                break
                        score = sim + variant_boost
                        if score > best_score:
                            best_score = score
                            best_cand = cand
                    if best_cand and (not anime or best_cand.get('mal_id') != anime.get('mal_id')) and best_score > 0.45:
                        anime = best_cand
                        if ui_queue:
                            ui_queue.put(("debug_process", f"[Disambiguation] selected mal_id={anime.get('mal_id')} for folder {titulo_busqueda} (score={best_score:.2f})"))
                        else:
                            logging.info("[Disambiguation] selected mal_id=%s for folder %s (score=%.2f)", anime.get('mal_id'), titulo_busqueda, best_score)
            except Exception as e:
                logging.debug("Error in folder-name disambiguation: %s", e)

        # If enabled, write MAL id into the media folder to persist association
        if auto_tag_mal and anime and anime.get('mal_id'):
            try:
                mal_path = os.path.join(carpeta_anime, 'mal_id.txt')
                if not os.path.exists(mal_path):
                    with open(mal_path, 'w', encoding='utf-8') as mf:
                        mf.write(str(anime.get('mal_id')))
                    if ui_queue:
                        ui_queue.put(("debug_process", f"Wrote MAL id {anime.get('mal_id')} to {mal_path}"))
                else:
                    try:
                        with open(mal_path, 'r', encoding='utf-8') as mf:
                            existing = mf.read().strip()
                        if existing != str(anime.get('mal_id')):
                            with open(mal_path + '.bak', 'w', encoding='utf-8') as bk:
                                bk.write(existing)
                            with open(mal_path, 'w', encoding='utf-8') as mf:
                                mf.write(str(anime.get('mal_id')))
                            if ui_queue:
                                ui_queue.put(("debug_process", f"Updated MAL id in {mal_path} (backup saved)"))
                    except Exception:
                        pass
            except Exception:
                pass

        # Submit generation task to executor if parallelism enabled, otherwise run inline
        gen_args = (
            barra_progreso,
            label_estado,
            titulo_busqueda,
            carpeta_anime,
            anime,
            idioma_seleccionado.get() if hasattr(idioma_seleccionado, 'get') else idioma_seleccionado,
            label_meta,
            True,
            label_titulo,
            label_sinopsis,
            etiqueta_imagen,
            ui_queue,
            root,
            defer_flag,
        )
        if executor:
            futures.append(executor.submit(generar_en_hilo_con_tipo, *gen_args))
        else:
            try:
                res = generar_en_hilo_con_tipo(*gen_args)
                if res and isinstance(res, dict):
                    generated_entries.append(res)
            except Exception as e:
                logging.exception("Error generating page for %s: %s", titulo_busqueda, e)
        if anime is None:
            pendientes_manual.append(carpeta_anime)

    time.sleep(random.uniform(1, 3))

    for idx, carpeta_anime in enumerate(pendientes_manual, start=1):
        # Honor stop/pause while processing pending manual items
        try:
            if gen_control.stop_requested():
                if ui_queue:
                    ui_queue.put(("label_text", label_estado, "⛔ Proceso detenido por el usuario."))
                break
            gen_control.wait_if_paused()
        except Exception:
            pass
        titulo_busqueda = os.path.basename(carpeta_anime)
        if label_estado:
            try:
                label_estado.config(text=f"[Pendiente {idx}/{len(pendientes_manual)}] {titulo_busqueda}")
            except Exception:
                pass
        if root:
            try:
                root.update_idletasks()
            except Exception:
                pass
        generar_en_hilo_con_tipo(
            barra_progreso,
            label_estado,
            titulo_busqueda,
            carpeta_anime,
            None,
            idioma_seleccionado.get() if hasattr(idioma_seleccionado, 'get') else idioma_seleccionado,
            label_meta,
            auto_confirm=True,
            label_titulo=label_titulo,
            label_sinopsis=label_sinopsis,
            etiqueta_imagen=etiqueta_imagen,
            ui_queue=ui_queue,
            root=root,
            skip_json_update=False
        )
        time.sleep(random.uniform(1, 3))
    # If we used an executor, wait for submitted tasks to finish and update progress
    if executor:
        try:
            total_submitted = len(futures)
            for fut in as_completed(futures):
                try:
                    res = fut.result()
                    if res and isinstance(res, dict):
                        generated_entries.append(res)
                except Exception as e:
                    logging.exception("Error in generation worker: %s", e)
                completed_count += 1
                # Update progress, percent and ETA
                try:
                    pct = int((completed_count / total) * 100) if total > 0 else 100
                    elapsed = time.time() - t0_run
                    avg = elapsed / max(1, completed_count)
                    remaining = max(0, total - completed_count)
                    eta_seconds = int(avg * remaining)
                    eta_str = time.strftime('%H:%M:%S', time.gmtime(eta_seconds)) if eta_seconds > 0 else '00:00:00'
                    if ui_queue:
                        ui_queue.put(("progress", barra_progreso, pct))
                        ui_queue.put(("label_text", label_estado, f"[{completed_count}/{total}] Procesando... — {pct}% — ETA: {eta_str}"))
                except Exception:
                    pass
        finally:
            try:
                executor.shutdown(wait=False)
            except Exception:
                pass
    # Finished (or aborted) automatic run
    if barra_progreso:
        try:
            barra_progreso["value"] = 100
        except Exception:
            pass
    if label_estado:
        try:
            label_estado.config(text="✅ Proceso automático completado!")
        except Exception:
            pass
    # Notify UI that the generation run finished so it can clean up controls
    if ui_queue:
        try:
            ui_queue.put(("generation_finished",))
        except Exception:
            pass
    # If we deferred JSON writes, merge and write generated entries now
    try:
        if defer_flag and generated_entries:
            merge_and_write_generated_entries(generated_entries)
            if ui_queue:
                try:
                    ui_queue.put(("debug_process", f"Merged {len(generated_entries)} generated entries into JSON indexes."))
                except Exception:
                    pass
    except Exception as e:
        logging.warning('Error while merging/writing deferred JSON entries: %s', e)
    # Optionally run HTML extractor to regenerate aggregate JSON
    try:
        if config.get('auto_run_extractor', False) and extractor_mod:
            try:
                pages_dir = config.get('pages_output_dir')
                if not pages_dir:
                    pages_dir = os.path.join(os.path.dirname(__file__), "pages")
                    logging.warning("pages_output_dir no está configurado en config.json — usando fallback '%s'. Configura la ruta en Ajustes.", pages_dir)
                    try:
                        config['pages_output_dir'] = pages_dir
                        try:
                            from src.core.config import save_config as _save_config
                            _save_config(config)
                        except Exception:
                            pass
                    except Exception:
                        pass
                out_path = os.path.join(pages_dir, 'anime_info.json')
                if ui_queue:
                    ui_queue.put(("debug_process", f"Iniciando extractor en {pages_dir} -> {out_path}"))
                # pass ui_queue so extractor can emit progress updates
                extractor_mod.extract_folder(pages_dir, out_path, config.get('json_link_prefix', ''), ui_queue=ui_queue)
                if ui_queue:
                    ui_queue.put(("debug_process", f"Extractor completado: {out_path}"))
            except Exception as e:
                if ui_queue:
                    ui_queue.put(("debug_error", f"Error al ejecutar extractor: {e}"))
    except Exception:
        pass

# Template handling
# Allow overriding the template path via config 'template_path' or at runtime
DEFAULT_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "template.html")
_configured_template_path = config.get('template_path', DEFAULT_TEMPLATE_PATH)


def _load_template_from_path(path=None):
    """Load template content from `path` (string). If path is None, use configured value.
    Returns the loaded template string (or a built-in fallback).
    """
    _path = path or _configured_template_path or DEFAULT_TEMPLATE_PATH
    try:
        if os.path.exists(_path):
            with open(_path, "r", encoding="utf-8") as tf:
                return tf.read()
    except Exception:
        pass
    # fallback minimal template
    return """<!DOCTYPE html>
<html lang="es">
<head>
    <meta http-equiv="Content-Type" content="text/html; charset=UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{titulo}</title>
    <link rel="stylesheet" href="1styles.css">
</head>
<body>
    <header>
        <h1>{titulo}</h1>
    </header>
    <main>
        <div class="row">
            <div class="image-container">
                <img src="{ruta_portada}" alt="Portada">
            </div>
            <div class="content">
                <h2>Sinopsis</h2>
                <p>{sinopsis}</p>
                <div class="tags">
                    {tags}
                </div>
            </div>
        </div>
        <h2>Lista de Capítulos</h2>
        <ul id="videoListEs" class="episode-list">
            {capitulos}
        </ul>
        <div class="button-container">
            <button class="control-button" onclick="history.back()">Atrás</button>
        </div>
    </main>
    <script src="data.js"></script>
</body>
</html>
"""


# Current html template used by crear_pagina; can be overridden via set_template_from_path
html_template = _load_template_from_path(None)


def set_template_from_path(path):
    """Set the module's html_template by loading the file at `path`.
    If loading fails, falls back to existing template.
    Returns True on success, False otherwise.
    """
    global html_template, _configured_template_path
    try:
        content = _load_template_from_path(path)
        if content:
            html_template = content
            if path:
                _configured_template_path = path
            logging.info("Template loaded from %s", path)
            return True
    except Exception as e:
        logging.warning("Failed to set template from %s: %s", path, e)
    return False


def crear_pagina(datos, carpeta_salida, nombre_pagina, ui_queue=None, auto_confirm=True, skip_json_update=False):
    try:
        t_start_total = time.time()
        # Map TMDB numeric genre ids to names for display in the HTML.
        try:
            from src.core.utils import map_tmdb_genres
            # Prefer merged genres/themes from metadata_full or datos['themes'] when available
            display_genres = []
            try:
                meta = datos.get('metadata_full') or {}
                if isinstance(meta, dict):
                    # collect names from meta['genres'] (dicts or strings)
                    for g in (meta.get('genres') or []):
                        if isinstance(g, dict):
                            name = g.get('name') or g.get('title')
                        else:
                            name = str(g)
                        if name and name not in display_genres:
                            display_genres.append(name)
                    # collect themes
                    for t in (meta.get('themes') or []):
                        if isinstance(t, dict):
                            name = t.get('name') or t.get('title')
                        else:
                            name = str(t)
                        if name and name not in display_genres:
                            display_genres.append(name)
            except Exception:
                display_genres = []
            # include any themes carried inside `datos` (from earlier fetch) to avoid extra API calls.
            try:
                for t in (datos.get('themes') or []):
                    if t and t not in display_genres:
                        display_genres.append(t)
            except Exception:
                pass

            # If we didn't find any merged names, fall back to mapping the textual categoria
            if not display_genres:
                display_genres = map_tmdb_genres(datos.get("categoria", ""))
        except Exception:
            display_genres = [t.strip() for t in datos.get("categoria", "").split(",") if t.strip()]
        # Translate genre/tag names to Spanish when possible and update datos so other parts that read categoria get readable names
        try:
            # diccionario_comun is imported at module level as GENRE_MAP
            translated = []
            for g in display_genres:
                if not g:
                    continue
                key = str(g).strip()
                # Try exact, then lowercase, then title-cased matches in the common dictionary
                spanish = diccionario_comun.get(key) or diccionario_comun.get(key.lower()) or diccionario_comun.get(key.title()) or key
                translated.append(spanish)
            display_genres = translated
        except Exception:
            pass
        datos["categoria"] = ", ".join(display_genres)
        tags_html = "".join([f'<a href="Carusel.html?tag={tag.strip()}" class="tag">{tag.strip()}</a>'
                             for tag in display_genres if tag.strip()])

        capitulos_html = ""
        web_media_prefix = str(config.get('media_web_prefix') or '/media/')
        if not web_media_prefix.endswith('/'):
            web_media_prefix = web_media_prefix + '/'
        titulos_list = datos.get("titulos_capitulos") or []
        # Defensive: ensure titulos_list is a list of strings
        try:
            if not isinstance(titulos_list, (list, tuple)):
                titulos_list = [str(titulos_list)]
            titulos_list = [str(t) if t is not None else f"Capítulo {idx+1}" for idx, t in enumerate(titulos_list)]
        except Exception:
            titulos_list = ["Capítulo 1"]

        for i, titulo_cap in enumerate(titulos_list, start=1):
            num_cap = str(i).zfill(2)
            ruta_video = f"{web_media_prefix}{datos.get('ruta_anime','')}/{num_cap}.mp4"
            ruta_video = ruta_video.replace('\\', '/')
            capitulos_html += f'<li data-src="{ruta_video}">{titulo_cap}</li>\n'

        # Debug: log expected vs generated counts and a small sample
        try:
            logging.debug("crear_pagina: expected titulos count=%d for %s", len(titulos_list), datos.get('titulo'))
            sample = titulos_list[:8]
            logging.debug("crear_pagina: titulos sample=%s", sample)
            # quick check of generated lines
            generated_count = capitulos_html.count('<li')
            logging.debug("crear_pagina: generated <li> count=%d", generated_count)
            if generated_count != len(titulos_list):
                logging.warning("crear_pagina: mismatch between titulos_list (%d) and generated <li> (%d) for %s", len(titulos_list), generated_count, datos.get('titulo'))
        except Exception:
            logging.debug("crear_pagina: error while logging capitulos info")

        if datos.get("sinopsis"):
            datos["sinopsis"] = clean_synopsis(datos["sinopsis"], emit=lambda m: ui_queue.put(("debug_process", m)) if ui_queue else None)
        html_content = html_template.format(
            titulo=datos.get("titulo", ""),
            sinopsis=datos.get("sinopsis", ""),
            ruta_portada=datos.get("ruta_portada", ""),
            tags=tags_html,
            capitulos=capitulos_html
        )

        # Ensure carpeta_salida is a Path
        carpeta_salida = Path(carpeta_salida)
        output_path = carpeta_salida / f"{nombre_pagina}.html"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # Overwrite existing HTML directly (atomic replace below) — no separate .bak files
        # Atomic write for HTML: write to tmp then replace
        try:
            tmp_html = output_path.with_name(output_path.name + '.tmp')
            with open(tmp_html, 'w', encoding='utf-8') as f:
                f.write(html_content)
            os.replace(str(tmp_html), str(output_path))
            logging.debug('crear_pagina: wrote html atomically to %s', output_path.as_posix())
        except Exception as e:
            logging.exception('crear_pagina: failed writing html %s: %s', output_path.as_posix(), e)

        # The aggregate JSON file (anime list) must live outside pages_output_dir per policy.
        try:
            from src.core.config import config as _conf
            # Prefer explicit OUTPUT_JSON_PATH, then legacy anime_json_path, then default under carpeta_salida
            ruta_json_cfg = _conf.get('OUTPUT_JSON_PATH') or _conf.get('anime_json_path') or None
        except Exception:
            ruta_json_cfg = None

        if ruta_json_cfg:
            ruta_json = Path(ruta_json_cfg).resolve()
        else:
            ruta_json = (carpeta_salida / "animes.json").resolve()

        logging.debug("crear_pagina: initial ruta_json resolved to %s", ruta_json.as_posix())
        # If the configured path does not exist, prefer existing candidates in common locations
        try:
            if not ruta_json.exists():
                alt_candidates = [
                    ruta_json.with_name('anime.json'),
                    ruta_json.with_name('animes.json'),
                    Path(__file__).with_name('anime.json'),
                    Path(__file__).with_name('animes.json'),
                    Path(os.getcwd()) / 'anime.json',
                    Path(os.getcwd()) / 'animes.json',
                ]
                for c in alt_candidates:
                    if c.exists():
                        logging.debug("crear_pagina: switching ruta_json to existing candidate %s", c.as_posix())
                        ruta_json = c.resolve()
                        break
        except Exception as e:
            logging.debug("crear_pagina: error resolving ruta_json: %s", e)
        # Protección de acceso concurrente a animes.json
        logging.debug("crear_pagina: attempting to acquire file_lock for %s", ruta_json)
        acquired = False
        try:
            try:
                acquired = file_lock.acquire(timeout=10)
            except TypeError:
                # Older threading.Lock may not support timeout parameter; fall back to blocking
                file_lock.acquire()
                acquired = True
            if not acquired:
                logging.warning("crear_pagina: could not acquire file_lock within timeout, proceeding without lock for %s", ruta_json)

            animes = []
            if os.path.exists(ruta_json):
                try:
                    logging.debug("crear_pagina: reading existing json %s", ruta_json)
                    with open(ruta_json, "r", encoding="utf-8") as jf:
                        animes = json.load(jf)
                    logging.debug("crear_pagina: loaded existing json entries=%d", len(animes) if isinstance(animes, list) else 1)
                except json.JSONDecodeError:
                    logging.warning("animes.json existe pero no es JSON válido. Se sobrescribirá.")
                except Exception as e:
                    logging.warning("crear_pagina: error leyendo %s: %s", ruta_json, e)

            # Defensive normalization: ensure `animes` is a list of dicts.
            if isinstance(animes, dict):
                animes = [animes]
            if not isinstance(animes, list):
                logging.warning("animes.json tiene formato inesperado (%s). Se reemplazará.", type(animes))
                animes = []

            # Defensive: ensure `datos` is a mapping-like object before using .get
            if not isinstance(datos, dict):
                logging.warning("crear_pagina: 'datos' esperado como dict, recibido %s. Abortando append.", type(datos))
                datos = {"titulo": str(datos)}

            # Only consider existing entries that are dicts with a 'title' field
            # Find existing entry by title (if present) and update it; otherwise append new
            exists = False
            existing_idx = None
            for i, a in enumerate(animes):
                try:
                    if isinstance(a, dict) and a.get('title') == datos.get('titulo'):
                        exists = True
                        existing_idx = i
                        break
                except Exception:
                    continue
            if not exists:
                # Decide el campo 'type' para animes.json: por defecto 'anime',
                # pero si la fuente es TMDB y media_type indica 'movie', usar 'pelicula'.
                tipo_json = "anime"
                prov = datos.get("provider")
                media_t = (datos.get("media_type") or "").lower() if datos.get("media_type") else ""
                # When provider is TMDB, respect the media_type to decide the JSON 'type'
                if prov == "tmdb":
                    if media_t == "tv" or media_t == "serie":
                        tipo_json = "serie"
                    elif media_t == "movie":
                        tipo_json = "pelicula"
                    else:
                        tipo_json = "pelicula"
                else:
                    if media_t and "movie" in media_t:
                        tipo_json = "pelicula"

                # Prepare genres list: prefer textual names; if numeric ids are present,
                # map them using the TMDB mapping provided by utils.map_tmdb_genres
                from src.core.utils import map_tmdb_genres
                raw_categoria = datos.get("categoria", "")
                genres_list = map_tmdb_genres(raw_categoria)

                # Prefer themes already carried inside `datos` (from earlier fetch) to avoid extra API calls.
                themes_list = datos.get('themes') or []
                if not themes_list:
                    # We'll attempt to enrich with Jikan 'themes' (and fallback genres) when available,
                    # but MUST do it outside the file_lock to avoid deadlock (cache uses same lock).
                    try:
                        if prov != 'tmdb':
                            mid = datos.get('mal_id')
                            if mid:
                                try:
                                    import src.providers.provider_jikan as pj
                                    # fetch_anime_metadata will use cache_get/cache_set; run it outside any lock
                                    meta = pj.fetch_anime_metadata(str(mid))
                                    if meta:
                                        if not genres_list and meta.get('genres'):
                                            genres_list = meta.get('genres')
                                        themes_list = meta.get('themes') or []
                                except Exception:
                                    pass
                    except Exception:
                        themes_list = []

                # Merge genres and themes into a single 'genres' list for the final JSON.
                # Preserve original order: first genres_list then themes not already present.
                merged = []
                try:
                    for g in (genres_list or []):
                        if g and g not in merged:
                            merged.append(g)
                    for t in (themes_list or []):
                        if t and t not in merged:
                            merged.append(t)
                except Exception:
                    merged = genres_list or []

                # Normalize merged genres to a list of strings (matching anime.json structure)
                try:
                    norm_genres = []
                    for g in (merged or []):
                        if isinstance(g, dict):
                            name = g.get('name') or g.get('title') or str(g)
                        else:
                            name = str(g)
                        name = name.strip()
                        if name and name not in norm_genres:
                            norm_genres.append(name)
                except Exception:
                    norm_genres = [str(t).strip() for t in (merged or []) if t]

                nuevo_anime = {
                    "title": datos.get("titulo"),
                    "link": f"{config.get('json_link_prefix', '')}{nombre_pagina}.html",
                    "image": datos.get("ruta_portada"),
                    # ensure genres is a simple list of strings
                    "genres": norm_genres,
                    "type": tipo_json,
                    "idioma": datos.get("idioma", "Japonés"),
                    # write full translated synopsis into the index to preserve content
                    "synopsis": datos.get("sinopsis", ""),
                    # keep a short summary field for quick displays
                    "short_synopsis": resumir_texto(datos.get("sinopsis", ""))
                }

                # Append only the minimal structure to keep the same format as anime.json
                if exists and existing_idx is not None:
                    # Update existing entry in-place to preserve ordering
                    animes[existing_idx].update(nuevo_anime)
                    logging.debug("crear_pagina: updated existing entry index=%d title=%s", existing_idx, nuevo_anime.get('title'))
                else:
                    animes.append(nuevo_anime)
                    logging.debug("crear_pagina: appended nuevo_anime title=%s genres=%s", nuevo_anime.get('title'), nuevo_anime.get('genres'))
                # If caller requested to skip JSON update (batch mode), return the minimal entry
                if skip_json_update:
                    try:
                        return nuevo_anime
                    except Exception:
                        return None

            # Ensure directory exists for the aggregate JSON
            try:
                ruta_json.parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass

            # Do not create adjacent .bak files next to the project JSON.
            # Use centralized `_write_json_atomic` which stores backups under `.vista/backups`.
            logging.debug('crear_pagina: will update aggregate json at %s (no adjacent .bak will be created)', ruta_json.as_posix())

                # Write atomically: write to a tmp file then replace.
            try:
                # use centralized atomic writer which handles backups and skip-if-unchanged
                wrote = _write_json_atomic(str(ruta_json), animes)
                if wrote:
                    logging.info("crear_pagina: atomically wrote aggregate json to %s (entries=%d)", ruta_json.as_posix(), len(animes))
                    try:
                        debug_file = config.get('debug_log_file') or 'debug.log'
                        with open(debug_file, 'a', encoding='utf-8') as df:
                            df.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] crear_pagina: wrote aggregate json {ruta_json.as_posix()} entries={len(animes)}\n")
                    except Exception:
                        pass
                else:
                    logging.debug("crear_pagina: no changes detected for %s; write skipped (entries=%d)", ruta_json.as_posix(), len(animes))
                    try:
                        debug_file = config.get('debug_log_file') or 'debug.log'
                        with open(debug_file, 'a', encoding='utf-8') as df:
                            df.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] crear_pagina: write skipped for {ruta_json.as_posix()} entries={len(animes)}\n")
                    except Exception:
                        pass
            except Exception as e:
                logging.exception("crear_pagina: failed to write aggregate json to %s: %s", ruta_json.as_posix(), e)
        finally:
            if acquired:
                try:
                    file_lock.release()
                except Exception:
                    pass

        # Notificación o log según modo
        try:
            elapsed = time.time() - t_start_total
            logging.debug('crear_pagina: finished page %s in %.2fs', datos.get('titulo'), elapsed)
        except Exception:
            pass
        if not auto_confirm:
            if ui_queue:
                ui_queue.put(("show_info", "✅ Éxito", f"Página generada y animes.json actualizado:\n{output_path}"))
        else:
            logging.info(f"[Batch] ✅ {datos.get('titulo')} generado automáticamente en {output_path}")

    except Exception as e:
        if ui_queue:
            ui_queue.put(("show_error", "Error", f"Ocurrió un error al generar la página:\n{e}"))


def generar_preview(anime, carpeta_anime, label_titulo, label_sinopsis, etiqueta_imagen, label_meta=None, label_estado=None, ui_queue=None):
    if not anime:
        return
    # Encolar actualizaciones de UI para ser procesadas en el hilo principal
    if ui_queue:
        ui_queue.put(("label_text", label_titulo, anime.get("title")))
        sinopsis_corta = resumir_texto(anime.get("synopsis") or "Sinopsis no disponible.", max_len=150)
        ui_queue.put(("label_text", label_sinopsis, sinopsis_corta))
    url_img = anime.get("images", {}).get("jpg", {}).get("large_image_url", "")
    if url_img:
        try:
            response = session.get(url_img, timeout=10)
            img = Image.open(io.BytesIO(response.content)).resize((150, 220))
            img_tk = ImageTk.PhotoImage(img)
            if ui_queue:
                ui_queue.put(("label_image", etiqueta_imagen, img_tk))
        except Exception as e:
            logging.warning("Error cargando imagen de preview: %s", e)
    # Obtener conteo de episodios desde proveedor configurado y archivos locales para mostrar en meta
    try:
        remote_eps = get_episodes_for_anime(anime) if anime else []
        num_remote = len(remote_eps) if remote_eps else 0
    except Exception:
        num_remote = 0
    try:
        archivos = [f for f in natsorted(os.listdir(carpeta_anime)) if os.path.isfile(os.path.join(carpeta_anime, f)) and f.lower().endswith('.mp4')]
        num_videos = len(archivos)
    except Exception:
        num_videos = 0

    provider_name = config.get('metadata_provider', 'jikan').capitalize()
    meta_text = f"{provider_name}: {num_remote} eps | Local: {num_videos} videos"
    if ui_queue:
        ui_queue.put(("label_text", label_meta, meta_text))
    # Si el proveedor remoto tiene más episodios que los videos locales, avisar al usuario
    if num_remote > 0 and num_remote > num_videos:
        aviso = f"⚠ {provider_name} tiene {num_remote} episodios, pero hay {num_videos} videos locales. Se procesarán los videos locales."
        if ui_queue:
            ui_queue.put(("label_text", label_estado, aviso))
