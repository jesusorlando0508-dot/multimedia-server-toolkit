import os
import re
import logging
import queue
from typing import Optional, List, Any

from src.core.network import session
from src.core.config import config

# Utilities for filenames, text and images


def limpiar_nombre_archivo(nombre: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", nombre)


def dividir_texto(texto: str, max_chars: int = 2048) -> List[str]:
    """Divide texto largo en una lista de trozos de como máximo `max_chars`.

    Siempre devuelve una lista (vacía si `texto` es vacío).
    """
    partes: List[str] = []
    if not texto:
        return partes
    while len(texto) > max_chars:
        corte = texto.rfind(" ", 0, max_chars)
        if corte == -1:
            corte = max_chars
        partes.append(texto[:corte])
        texto = texto[corte:].lstrip()
    if texto:
        partes.append(texto)
    return partes


def limpiar_traduccion(texto: str, ui_queue: Optional[queue.Queue] = None, label: Optional[Any] = None) -> str:
    texto_original = texto
    # remove known bad rewrite attributions
    texto = re.sub(r"(escrito por mal rewrite|written by mal rewrite|mal[_ ]?rewrite)", "", texto, flags=re.IGNORECASE)

    # Remove trailing parenthetical/source attributions such as "(Fuente: MAL)", "(source: myanimelist)", etc.
    try:
        texto = re.sub(r"[\(\[]\s*(?:fuente|source|via|credits|cr[eé]ditos)\b[^\)\]]*[\)\]]\s*$", "", texto, flags=re.IGNORECASE).strip()
    except Exception:
        pass

    # Remove trailing 'Fuente: ...' or 'Source: ...' or '— Fuente: ...' at end of string
    try:
        # Enforce an explicit ':' or '-' before stripping to avoid truncating phrases como "la fuente de poder"
        texto = re.sub(r"(?:\s*[—\-]\s*)?(?:fuente|source|via|credits|cr[eé]ditos)\b\s*[:\-]\s*.+$", "", texto, flags=re.IGNORECASE).strip()
    except Exception:
        pass

    # Final cleanup: strip whitespace then surrounding quotes
    texto = texto.strip()
    texto = texto.strip('"\'')

    if texto != texto_original:
        aviso = f"[AVISO] Se eliminaron frases de traducción:\n'{texto_original}' → '{texto}'"
        try:
            print(aviso)
        except UnicodeEncodeError:
            safe_aviso = aviso.encode('ascii', errors='replace').decode('ascii', errors='replace')
            print(safe_aviso)
        except Exception:
            logging.debug("No se pudo mostrar aviso de traducción", exc_info=True)
        if ui_queue and label:
            try:
                if hasattr(ui_queue, 'put'):
                    ui_queue.put(("label_text", label, aviso))
                else:
                    logging.debug("ui_queue provided but has no put() method")
            except Exception:
                logging.debug("No se pudo encolar aviso de traducción")
    return texto


def resumir_texto(texto: str, max_len: int = 250) -> str:
    if not texto:
        return ""
    if len(texto) <= max_len:
        return texto
    return texto[:max_len].rsplit(' ', 1)[0] + "…"


def descargar_imagen(url: str, ruta_destino: str) -> bool:
    try:
        response = session.get(url, timeout=10)
        response.raise_for_status()
        if "image" not in response.headers.get("Content-Type", ""):
            return False
        os.makedirs(os.path.dirname(ruta_destino), exist_ok=True)
        with open(ruta_destino, "wb") as f:
            f.write(response.content)
        return True
    except Exception as e:
        logging.warning("No se pudo descargar imagen %s: %s", url, e)
        return False


def buscar_imagen_local(carpeta: str) -> Optional[str]:
    for ext in ["jpg", "jpeg", "png", "gif", "webp"]:
        ruta = os.path.join(carpeta, f"1.{ext}")
        if os.path.exists(ruta):
            # Build a web-accessible path for the image using configured media prefix
            web_prefix = config.get('media_web_prefix') or '/media/'
            # Ensure prefix ends with '/'
            if not web_prefix.endswith('/'):
                web_prefix = web_prefix + '/'
            return os.path.join(web_prefix, os.path.basename(carpeta), f"1.{ext}").replace('\\', '/')
    return None


# Diccionario común de géneros (para traducciones rápidas)
GENRE_MAP = {
    "Action": "Acción", "Adventure": "Aventura", "Comedy": "Comedia", "Drama": "Drama",
    "Romance": "Romance", "Fantasy": "Fantasía", "Horror": "Terror", "Sci-Fi": "Ciencia ficción",
    "Science Fiction": "Ciencia ficción", "Mystery": "Misterio", "Music": "Música", "Sports": "Deportes",
    "Slice of Life": "Recuentos de la vida", "Supernatural": "Sobrenatural", "Ecchi": "Ecchi", "Mecha": "Mecha",
    "Shounen": "Shounen", "Family": "Familia", "Thriller": "Suspense", "Crime": "Crimen",
    "History": "Histórico", "Documentary": "Documental", "Western": "Western", "Biography": "Biografía",
    "Animation": "Animación", "Kids": "Infantil", "Talk": "Talk show", "Reality": "Reality",
    "Sport": "Deporte", "Game-Show": "Concurso",
}


# TMDB genre mapping (load once). This file is expected to be named `tmdb_gen.json` and
# contain a mapping from TMDB genre id (as string) to localized name. We try to load it
# from the module directory first, then the current working directory. If absent, the map
# will be empty.
TMDB_GENRE_MAP = {}
try:
    import json as _json
    # Prefer a user-configurable path from config, fall back to module dir then cwd
    try:
        from config import config as _config
        _mapping_path = _config.get('tmdb_gen_path') or os.path.join(os.path.dirname(__file__), "tmdb_gen.json")
    except Exception:
        _mapping_path = os.path.join(os.path.dirname(__file__), "tmdb_gen.json")
    if not os.path.exists(_mapping_path):
        _mapping_path = os.path.join(os.getcwd(), "tmdb_gen.json")
    if os.path.exists(_mapping_path):
        with open(_mapping_path, "r", encoding="utf-8") as _fh:
            TMDB_GENRE_MAP = _json.load(_fh) or {}
except Exception:
    TMDB_GENRE_MAP = {}


def map_tmdb_genres(raw_categoria: str):
    """Convierte una cadena separada por comas que puede contener ids TMDB
    o nombres ya traducidos en una lista de nombres legibles.

    Ejemplos:
      '28,12' -> ['Acción', 'Aventura']
      'Action,Comedy' -> ['Action', 'Comedy'] (sin mapping disponible)
    """
    if not raw_categoria:
        return []
    tokens = [t.strip() for t in raw_categoria.split(",") if t.strip()]
    out = []
    for tag in tokens:
        # if numeric id, try mapping
        if tag.isdigit() and tag in TMDB_GENRE_MAP:
            out.append(TMDB_GENRE_MAP[tag])
        elif tag.isdigit() and str(int(tag)) in TMDB_GENRE_MAP:
            out.append(TMDB_GENRE_MAP[str(int(tag))])
        elif tag in TMDB_GENRE_MAP:
            out.append(TMDB_GENRE_MAP[tag])
        else:
            out.append(tag)
    return out


def normalize_folder_name_for_search(name: str):
    """Normalize a folder name into one or more search-friendly title variants.

    Returns a list of candidate strings ordered from most to least specific.
    The function removes common rip/codec/resolution tokens, bracketed content,
    replaces separators with spaces, and yields variants with/without year.
    """
    if not name:
        return [""]
    s = name
    # remove file extensions if present
    s = re.sub(r"\.[a-zA-Z0-9]{1,4}$", "", s)
    # replace separators with spaces
    s = re.sub(r'[\._\-]+', ' ', s)
    # remove bracketed content [..], (..), {..}
    s = re.sub(r"\[.*?\]|\(.*?\)|\{.*?\}", "", s)
    # remove common tags like resolutions, codecs, languages, release tags
    tags_rx = r"\b(480p|720p|1080p|2160p|4k|8k|x264|x265|h264|h265|hevc|bdrip|bluray|bdrip|web[- ]?dl|webdl|web|dvd|dvdrip|amzn|mp4|mkv|aac|flac|eng|esp|sub|subbed|dubbed|dub)\b"
    s = re.sub(tags_rx, '', s, flags=re.IGNORECASE)
    # remove leftover multiple spaces
    s = re.sub(r'\s{2,}', ' ', s).strip()

    candidates = []
    if s:
        candidates.append(s)
    # variant without year
    s_no_year = re.sub(r"\b(19|20)\d{2}\b", '', s).strip()
    if s_no_year and s_no_year != s:
        candidates.append(s_no_year)
    # detect roman numerals used as season suffixes (I, II, III, IV, V, VI...)
    roman_rx = re.compile(r"\b(M{0,3})(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})\b", re.IGNORECASE)
    # common short season tokens
    season_rx = re.compile(r"\bseason\b|\btemp(?:orada)?\b|\btemp\b|\bs\d{1,2}\b|\btemp\s*\d+\b|\bepisodio\b", re.IGNORECASE)
    # also extract explicit numeric season like 'Season 2' or 'Temporada 2'
    season_num_rx = re.compile(r"\bseason\s*(\d{1,2})\b|\btemporada\s*(\d{1,2})\b|\btemp(?:\.|\s)*(\d{1,2})\b|\bs(\d{1,2})\b", re.IGNORECASE)

    # remove season/episode hints
    s_seasonless = re.sub(r"\bseason\b|\bs\d{1,2}\b|\bep\d+\b|\bepisode\b|\btemp(?:orada)?\b|\btemp\b", '', s_no_year, flags=re.IGNORECASE).strip()
    if s_seasonless and s_seasonless not in candidates:
        candidates.append(s_seasonless)

    # If name contains an explicit season number, create variant without it and variant with 'Season N' spelled out
    m = season_num_rx.search(s)
    if m:
        # find the first matching group that's not None
        for g in m.groups():
            if g:
                try:
                    n = int(g)
                    # variant without the season number
                    without = season_num_rx.sub('', s).strip()
                    if without and without not in candidates:
                        candidates.append(without)
                    # variant with english 'Season N' appended (some providers expect this)
                    with_season = f"{without} Season {n}" if without else f"Season {n}"
                    if with_season and with_season not in candidates:
                        candidates.append(with_season)
                except Exception:
                    pass
                break

    # detect simple roman numerals (common suffixes like II, III, IV)
    # We look for short roman numerals at the end or near the end
    m2 = re.search(r"\b(I|II|III|IV|V|VI|VII|VIII|IX|X)\b", s, flags=re.IGNORECASE)
    roman_map = {"I":1, "II":2, "III":3, "IV":4, "V":5, "VI":6, "VII":7, "VIII":8, "IX":9, "X":10}
    if m2:
        roman = m2.group(1).upper()
        num = roman_map.get(roman)
        if num:
            without_roman = re.sub(r"\b(I|II|III|IV|V|VI|VII|VIII|IX|X)\b", '', s, flags=re.IGNORECASE).strip()
            if without_roman and without_roman not in candidates:
                candidates.append(without_roman)
            # variant with 'Season N'
            with_season = f"{without_roman} Season {num}" if without_roman else f"Season {num}"
            if with_season and with_season not in candidates:
                candidates.append(with_season)
    # ensure uniqueness and strip
    seen = set()
    out = []
    for c in candidates:
        cc = c.strip()
        if not cc:
            continue
        if cc.lower() in seen:
            continue
        seen.add(cc.lower())
        out.append(cc)
    if not out:
        return [name]
    return out
