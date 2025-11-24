import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import logging
import time
import os
import re
from src.core.cache import cache_get, cache_set

API_BASE = "https://api.jikan.moe/v4"


def create_session_with_retries(total_retries=3, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504)):
    session = requests.Session()
    retries = Retry(total=total_retries, backoff_factor=backoff_factor, status_forcelist=status_forcelist, allowed_methods=frozenset(['GET','POST']))
    adapter = HTTPAdapter(max_retries=retries)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session


session = create_session_with_retries()
logger = logging.getLogger(__name__)


def find_local_mal_id(folder_path):
    """Search for common local files that may contain a MAL id and return the normalized id string or None."""
    candidates = [
        'id_mal.json', 'id_mal.txt',
        'mal_id.json', 'mal_id.txt',
        'mal.json', 'id.json'
    ]
    import json as _json
    for fn in candidates:
        path = os.path.join(folder_path, fn)
        if not os.path.exists(path):
            continue
        try:
            if fn.endswith('.json'):
                with open(path, 'r', encoding='utf-8') as fh:
                    jd = _json.load(fh)
                    mid = jd.get('id_mal') or jd.get('mal_id') or jd.get('id') or jd.get('mal')
            else:
                with open(path, 'r', encoding='utf-8') as fh:
                    mid = fh.read().strip()
            if mid:
                try:
                    return str(int(mid))
                except Exception:
                    m = re.search(r"(\d+)", str(mid))
                    if m:
                        return m.group(1)
                    return str(mid).strip()
        except Exception:
            continue
    return None


def fetch_anime_by_id(mal_id):
    """Fetch anime object by MAL id.

    Try to use the `/anime/{id}/full` endpoint (returns more fields in a single call)
    as a first option to reduce round-trips. Fall back to `/anime/{id}` if the
    full endpoint is unavailable or fails.
    """
    try:
        # Prefer the /full endpoint to retrieve as much metadata as possible in one request.
        try:
            resp = session.get(f"{API_BASE}/anime/{mal_id}/full", timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data and data.get('data'):
                return data['data']
        except Exception:
            # Fallback to the standard endpoint
            resp = session.get(f"{API_BASE}/anime/{mal_id}", timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data and data.get('data'):
                return data['data']
    except Exception as e:
        logging.debug("provider_jikan.fetch_anime_by_id: failed for %s: %s", mal_id, e)
    return None


def _extract_genres_and_themes_from_obj(anime_obj):
    """Return tuple (genres, themes) as lists of names extracted from a Jikan anime object.

    Each element in the original lists is usually a dict with a 'name' key. We normalize to
    simple lists of strings and defend against missing keys or unexpected shapes.
    """
    genres = []
    themes = []
    try:
        for g in anime_obj.get('genres', []) or []:
            try:
                name = g.get('name') if isinstance(g, dict) else str(g)
                if name:
                    genres.append(name)
            except Exception:
                continue
    except Exception:
        genres = []
    try:
        for t in anime_obj.get('themes', []) or []:
            try:
                name = t.get('name') if isinstance(t, dict) else str(t)
                if name:
                    themes.append(name)
            except Exception:
                continue
    except Exception:
        themes = []
    return genres, themes


def fetch_anime_metadata(mal_id, use_cache=True):
    """Fetch anime object from Jikan and return a small dict with cached genres and themes.

    Returns a dict with keys: 'anime' (raw jikan object or None), 'genres' (list of names),
    'themes' (list of names). Results are cached under key `anime:jikan:{mal_id}` to avoid
    repeated calls when generating JSON/pages.
    """
    cache_key = f"anime:jikan:{mal_id}"
    if use_cache:
        cached = cache_get(cache_key)
        if cached is not None:
            return cached

    anime = fetch_anime_by_id(mal_id)
    genres, themes = ([], [])
    if anime:
        genres, themes = _extract_genres_and_themes_from_obj(anime)

    result = {"anime": anime, "genres": genres, "themes": themes}
    try:
        cache_set(cache_key, result)
    except Exception:
        # cache_set is best-effort
        pass
    return result


def obtener_episodios(anime_id):
    cache_key = f"episodes:jikan:{anime_id}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    episodios = []
    def _find_mal_link(obj):
        """Recursively search JSON-like object for a string containing 'myanimelist.net' or '#episodes' and return it."""
        if isinstance(obj, str):
            if 'myanimelist.net' in obj or '#episodes' in obj:
                return obj
            return None
        if isinstance(obj, dict):
            for k, v in obj.items():
                try:
                    res = _find_mal_link(v)
                except Exception:
                    res = None
                if res:
                    return res
            return None
        if isinstance(obj, (list, tuple)):
            for v in obj:
                try:
                    res = _find_mal_link(v)
                except Exception:
                    res = None
                if res:
                    return res
            return None
        return None

    logger.debug("provider_jikan.obtener_episodios: starting fetch for anime %s", anime_id)
    try:
        page = 1
        while True:
            # per-page retry loop for transient errors / rate limits
            attempt = 0
            max_attempts = 4
            data = None
            while attempt < max_attempts:
                try:
                    response = session.get(f"{API_BASE}/anime/{anime_id}/episodes", params={"page": page}, timeout=15)
                    # Handle explicit 429 with Retry-After
                    if response.status_code == 429:
                        ra = response.headers.get('Retry-After')
                        if ra is not None:
                            try:
                                wait = int(ra)
                            except Exception:
                                wait = min(2 ** attempt, 10)
                        else:
                            wait = min(2 ** attempt, 10)
                        logging.warning("provider_jikan.obtener_episodios: rate limited on page %s for %s, sleeping %s seconds", page, anime_id, wait)
                        time.sleep(wait)
                        attempt += 1
                        continue
                    response.raise_for_status()
                    data = response.json()
                    break
                except Exception as e:
                    logging.debug("provider_jikan.obtener_episodios: transient error fetching page %s for %s: %s", page, anime_id, e)
                    attempt += 1
                    time.sleep(min(2 ** attempt, 10))
                    continue

            if data is None:
                logging.warning("provider_jikan.obtener_episodios: failed to fetch page %s for %s after %s attempts", page, anime_id, max_attempts)
                break
            if data and "data" in data:
                page_items = data.get("data") or []
                episodios.extend(page_items)
                # Log per-page fetch count
                logger.debug("provider_jikan.obtener_episodios: fetched %d episodes from page %s for anime %s", len(page_items), page, anime_id)

                # Detect possible MyAnimeList redirect/link in the response payload and log it
                try:
                    mal_link = _find_mal_link(data)
                    if mal_link:
                        logger.debug("provider_jikan.obtener_episodios: detected MyAnimeList link in Jikan response for anime %s: %s", anime_id, mal_link)
                        # Also log how many episodes were present in that same response/page
                        logger.debug("provider_jikan.obtener_episodios: episodes in this Jikan response/page: %d", len(page_items))
                except Exception as e:
                    logger.debug("provider_jikan.obtener_episodios: error while scanning for MAL links: %s", e)
                # pagination may exist; be defensive if missing
                has_next = False
                try:
                    pag = data.get("pagination", {}) or {}
                    # Jikan may include 'has_next_page' or 'last_visible_page'
                    if pag.get('has_next_page') is not None:
                        has_next = bool(pag.get('has_next_page'))
                    else:
                        # fallback: compare page to last_visible_page if present
                        last = pag.get('last_visible_page')
                        if isinstance(last, int):
                            has_next = page < int(last)
                except Exception:
                    has_next = False
                if not has_next:
                    break
                page += 1
            else:
                break

        if episodios:
            cache_set(cache_key, episodios)
            logger.debug("provider_jikan.obtener_episodios: total episodes fetched for %s: %d", anime_id, len(episodios))
    except Exception as e:
        logging.warning("provider_jikan.obtener_episodios: No se pudieron obtener todos los episodios para %s: %s", anime_id, e)
    return episodios


def buscar_anime_candidates(titulo, limit_per_variant=8, overall_limit=20):
    """Return a list of candidate anime result dicts from Jikan for the given folder name/title."""
    from src.core.utils import normalize_folder_name_for_search
    candidates = []
    seen_ids = set()
    variants = normalize_folder_name_for_search(titulo)
    try:
        for v in variants:
            if not v:
                continue
            try:
                resp = session.get(f"{API_BASE}/anime", params={"q": v, "limit": limit_per_variant}, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                results = data.get("data") or []
                for r in results:
                    mid = r.get('mal_id')
                    if mid and mid not in seen_ids:
                        candidates.append(r)
                        seen_ids.add(mid)
                        if len(candidates) >= overall_limit:
                            return candidates
            except Exception:
                continue
    except Exception:
        pass
    return candidates


def buscar_anime_por_titulo_jikan(titulo):
    """Search Jikan by title (used when no local id is present). Returns best match or None."""
    from src.core.utils import normalize_folder_name_for_search
    cache_key = f"search:jikan:{titulo}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    best_result = None
    best_score = 0.0
    try:
        import re
        from difflib import SequenceMatcher

        season_num_rx_local = re.compile(r"\bseason\s*(\d{1,2})\b|\btemporada\s*(\d{1,2})\b|\btemp(?:\.|orada)?\s*(\d{1,2})\b|\bs(\d{1,2})\b", re.IGNORECASE)
        roman_map_local = {"I":1, "II":2, "III":3, "IV":4, "V":5, "VI":6, "VII":7, "VIII":8, "IX":9, "X":10}

        variants = normalize_folder_name_for_search(titulo)
        for v in variants:
            if not v:
                continue

            season_num = None
            mseason = season_num_rx_local.search(v)
            if mseason:
                for g in mseason.groups():
                    if g:
                        try:
                            season_num = int(g)
                            break
                        except Exception:
                            pass

            mroman = re.search(r"\b(I|II|III|IV|V|VI|VII|VIII|IX|X)\b", v, re.IGNORECASE)
            if not season_num and mroman:
                rn = mroman.group(1).upper()
                season_num = roman_map_local.get(rn)

            try:
                response = session.get(f"{API_BASE}/anime", params={"q": v, "limit": 8}, timeout=10)
                response.raise_for_status()
                data = response.json()
                results = data.get("data") or []
                if not results:
                    continue

                for cand in results:
                    cand_titles = []
                    try:
                        for t in [cand.get('title') or '', cand.get('title_english') or '', cand.get('title_japanese') or '']:
                            if t:
                                cand_titles.append(t.strip().lower())
                    except Exception:
                        cand_titles = [((cand.get('title') or '') or '').strip().lower()]

                    try:
                        syns = cand.get('titles') or []
                        for s in syns:
                            if isinstance(s, dict):
                                t = s.get('title') or ''
                            else:
                                t = str(s)
                            if t:
                                cand_titles.append(t.strip().lower())
                    except Exception:
                        pass

                    query_norm = v.strip().lower()
                    sim_scores = [SequenceMatcher(None, query_norm, ct).ratio() for ct in cand_titles if ct]
                    base_sim = max(sim_scores) if sim_scores else 0.0

                    tokens = re.findall(r"\w+", query_norm)
                    tokens = [t for t in tokens if t and len(t) > 1]
                    token_ratio = 0.0
                    if tokens:
                        joined = ' '.join(cand_titles)
                        matched = sum(1 for t in tokens if t in joined)
                        token_ratio = matched / len(tokens)

                    score = 0.6 * token_ratio + 0.35 * base_sim

                    if season_num:
                        season_found = False
                        for ct in cand_titles:
                            if f"season {season_num}" in ct or re.search(rf"\b{season_num}(st|nd|rd|th)?\b", ct):
                                if not re.search(rf"\b(19|20)\d{{2}}\b", ct):
                                    season_found = True
                                    break
                            m_r = re.search(r"\b(I|II|III|IV|V|VI|VII|VIII|IX|X)\b", ct, re.IGNORECASE)
                            if m_r:
                                rn = m_r.group(1).upper()
                                try:
                                    if roman_map_local.get(rn) == season_num:
                                        season_found = True
                                        break
                                except Exception:
                                    pass
                        if season_found:
                            score += 0.28

                    try:
                        my = re.search(r"\b(19|20)\d{2}\b", v)
                        if my:
                            year_match_val = my.group(0)
                            aired = cand.get('year') or ''
                            if aired and str(aired).startswith(year_match_val):
                                score += 0.15
                    except Exception:
                        pass

                    try:
                        te = (cand.get('title_english') or '').strip().lower()
                        if te and te == query_norm:
                            score += 0.25
                    except Exception:
                        pass

                    if score > best_score:
                        best_score = score
                        best_result = cand

                if best_score >= 0.78:
                    break
            except Exception:
                continue

        if best_result:
            cache_set(cache_key, best_result)
            return best_result
        return None
    except Exception as e:
        logging.warning("provider_jikan.buscar_anime_por_titulo_jikan '%s' (variants=%s): %s", titulo, normalize_folder_name_for_search(titulo), e)
        return None
