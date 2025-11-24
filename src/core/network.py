import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import time
import logging
import difflib
import os

from src.core.cache import cache_get, cache_set
from src.core.config import config
# Provider modules (optional). Import as modules to avoid symbol clashes and allow delegating.
try:
    from src.providers import provider_jikan as pj
except Exception:
    pj = None
try:
    from src.providers import provider_tmdb as pt
except Exception:
    pt = None

API_BASE = "https://api.jikan.moe/v4"
TMDB_API_BASE = "https://api.themoviedb.org/3"


def create_session_with_retries(total_retries=3, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504)):
    session = requests.Session()
    retries = Retry(total=total_retries, backoff_factor=backoff_factor, status_forcelist=status_forcelist, allowed_methods=frozenset(['GET','POST']))
    adapter = HTTPAdapter(max_retries=retries)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session


session = create_session_with_retries()


def obtener_episodios(anime_id):
    """Return episode list for a Jikan anime id. Delegate to provider_jikan if available; otherwise fetch paginated results and cache them."""
    try:
        if pj and hasattr(pj, 'obtener_episodios'):
            return pj.obtener_episodios(anime_id)
    except Exception:
        pass

    cache_key = f"episodes:jikan:{anime_id}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    episodios = []
    try:
        page = 1
        while True:
            resp = session.get(f"{API_BASE}/anime/{anime_id}/episodes", params={"page": page}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data and "data" in data:
                episodios.extend(data["data"])
                if not data.get("pagination", {}).get("has_next_page", False):
                    break
                page += 1
            else:
                break
        if episodios:
            cache_set(cache_key, episodios)
    except Exception as e:
        logging.warning("obtener_episodios: no se pudieron obtener todos los episodios para %s: %s", anime_id, e)
    return episodios


def tmdb_get_genres(api_key=None):
    # Delegate to provider module if available
    try:
        if pt and hasattr(pt, 'tmdb_get_genres'):
            return pt.tmdb_get_genres(api_key=api_key)
    except Exception:
        pass
    # fallback to local implementation
    try:
        key = api_key or config.get("tmdb_api_key", "")
        if not key:
            return {}
        cache_key = f"tmdb_genres:{key}"
        cached = cache_get(cache_key)
        if cached is not None:
            return cached

        mapping = {}
        try:
            resp_tv = session.get(f"{TMDB_API_BASE}/genre/tv/list", params={"api_key": key}, timeout=10)
            resp_tv.raise_for_status()
            data_tv = resp_tv.json()
            for g in data_tv.get("genres", []):
                mapping[g["id"]] = g["name"]
        except Exception:
            pass

        try:
            resp_mv = session.get(f"{TMDB_API_BASE}/genre/movie/list", params={"api_key": key}, timeout=10)
            resp_mv.raise_for_status()
            data_mv = resp_mv.json()
            for g in data_mv.get("genres", []):
                mapping[g["id"]] = g["name"]
        except Exception:
            pass

        if mapping:
            cache_set(cache_key, mapping)
        return mapping
    except Exception:
        return {}


def tmdb_search(query, media_preference='auto', allow_when_config_is_jikan=False):
    # Delegate to provider module if available
    try:
        if pt and hasattr(pt, 'tmdb_search'):
            # provider_tmdb has its own API key handling; pass through media_preference flag
            return pt.tmdb_search(query, media_preference=media_preference, allow_when_config_is_jikan=allow_when_config_is_jikan)
    except Exception:
        pass
    # Fallback: existing local implementation
    try:
        # Defensive guard: if the global configured provider is Jikan and the caller
        # did not explicitly allow TMDB calls in that situation, skip contacting TMDB.
        try:
            if config.get('metadata_provider', 'jikan') == 'jikan' and not allow_when_config_is_jikan:
                logging.debug("tmdb_search: blocked because global provider is 'jikan' and allow flag not set")
                return None
        except Exception:
            pass

        key = config.get("tmdb_api_key", "")
        if not key:
            return None
        use_v4 = config.get("tmdb_use_v4", False)
        access_token = config.get("tmdb_access_token", "") if use_v4 else None

        headers = {}

        # normalize incoming parameter to list of candidate queries
        queries = []
        if isinstance(query, (list, tuple)):
            queries = [q for q in query if q]
        else:
            queries = [query]

        def make_variants(q):
            import re
            variants = [q]
            if not q:
                return variants
            cleaned = re.sub(r"[_\.\-]+", " ", q)
            cleaned = re.sub(r"\(.*?\)|\[.*?\]", "", cleaned).strip()
            cleaned = re.sub(r"\b(19|20)\d{2}\b", "", cleaned).strip()
            cleaned = re.sub(r"\s+", " ", cleaned)
            if cleaned and cleaned not in variants:
                variants.append(cleaned)
            no_articles = re.sub(r"\b(the|a|an)\b", "", cleaned, flags=re.IGNORECASE).strip()
            no_articles = re.sub(r"\s+", " ", no_articles)
            if no_articles and no_articles not in variants:
                variants.append(no_articles)
            return variants

        params = {"query": "", "page": 1}
        if use_v4 and access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        else:
            params["api_key"] = key

        # Cache key should include media preference to avoid mixing movie/tv results
        cache_key_pref = f"search:tmdb:{media_preference}:{query}"
        cached_pref = cache_get(cache_key_pref)
        if cached_pref is not None:
            return cached_pref

        # iterate candidate queries and keep best overall match across all candidates
        overall_best = None
        overall_best_score = -1.0
        overall_media_type = None

        for candidate in queries:
            query_variants = make_variants(candidate)
            logging.debug("tmdb_search: candidate='%s' variants=%s", candidate, query_variants)

            results = []
            data = {}

            results = []
            media_type = None
            # Decide search order based on media_preference
            pref = (media_preference or 'auto').lower()
            # Helper to try an endpoint and return results
            def try_search(endpoint):
                nonlocal results
                for qv in query_variants:
                    params["query"] = qv
                    try:
                        resp = session.get(f"{TMDB_API_BASE}/{endpoint}", params=params, headers=headers, timeout=10)
                        resp.raise_for_status()
                        data = resp.json()
                        results = data.get("results") or []
                        if results:
                            logging.debug("tmdb_search: found %d %s results for variant '%s'", len(results), endpoint, qv)
                            return True
                    except Exception:
                        results = []
                return False

            if pref == 'movie':
                if try_search('search/movie'):
                    media_type = 'movie'
            elif pref == 'tv':
                if try_search('search/tv'):
                    media_type = 'tv'
            else:  # auto (existing behavior): try movie then tv
                if try_search('search/movie'):
                    media_type = 'movie'
                else:
                    if try_search('search/tv'):
                        media_type = 'tv'

            if not results:
                # try next candidate query in the provided list
                continue

            # Select best candidate: prefer exact match, otherwise use token-overlap and similarity
            import re
            qnorm = (candidate or "").strip().lower()
            # detect year if present in original candidate
            year_match_val = None
            m_year = re.search(r"\b(19|20)\d{2}\b", candidate or "")
            if m_year:
                year_match_val = m_year.group(0)

            # prefer TV search first if the candidate indicates seasons
            tv_hint = bool(re.search(r"season\b|s\d{1,2}\b|temporada\b", (candidate or "").lower()))

            best = None
            best_score = -1.0

            tokens = re.findall(r"\w+", qnorm)
            tokens = [t for t in tokens if t and len(t) > 1]

            for cand in results:
                # compute candidate title text
                title_candidates = [cand.get("title") or cand.get("name") or "", cand.get("original_title") or cand.get("original_name") or ""]
                cand_title = (title_candidates[0] or "").strip().lower()
                # exact match
                if cand_title == qnorm:
                    best = cand
                    best_score = 1.0
                    break

                # token overlap
                if tokens:
                    matched = sum(1 for t in tokens if t in cand_title)
                    token_ratio = matched / len(tokens)
                else:
                    token_ratio = 0.0

                # similarity
                sim = difflib.SequenceMatcher(None, qnorm, cand_title).ratio()

                # year match bonus
                year_bonus = 0.0
                try:
                    release = cand.get('release_date') or cand.get('first_air_date') or ''
                    if release and year_match_val and release.startswith(year_match_val):
                        year_bonus = 0.2
                except Exception:
                    year_bonus = 0.0

                score = 0.6 * token_ratio + 0.3 * sim + year_bonus
                # prefer tv if tv_hint and candidate media type is tv-like (has 'first_air_date' or media_type tv)
                if tv_hint and (cand.get('media_type') == 'tv' or cand.get('first_air_date')):
                    score += 0.05

                if score > best_score:
                    best_score = score
                    best = cand

            if best is None:
                best = results[0]

            logging.debug("tmdb_search: selected candidate id=%s title=%s score=%.3f", best.get('id'), best.get('title') or best.get('name'), best_score)

            # track best across candidates
            try:
                score_for_candidate = float(best_score)
            except Exception:
                score_for_candidate = 0.0
            if score_for_candidate > overall_best_score:
                overall_best_score = score_for_candidate
                overall_best = best
                overall_media_type = media_type

        # after trying all candidates, if we have an overall best, fetch details and return
        if overall_best is None:
            return None

        best = overall_best
        media_type = overall_media_type or "movie"
        tmdb_id = best.get("id")
        details = best
        try:
            if media_type == "movie":
                det = session.get(f"{TMDB_API_BASE}/movie/{tmdb_id}", params=(None if headers else {"api_key": key}), headers=headers if headers else None, timeout=10)
            else:
                det = session.get(f"{TMDB_API_BASE}/tv/{tmdb_id}", params=(None if headers else {"api_key": key}), headers=headers if headers else None, timeout=10)
            det.raise_for_status()
            details = det.json()
        except Exception:
            details = best

        poster = details.get("poster_path") or best.get("poster_path")
        poster_url = f"https://image.tmdb.org/t/p/original{poster}" if poster else ""

        normalized = {
            "provider": "tmdb",
            "provider_id": tmdb_id,
            "tmdb_id": tmdb_id,
            "media_type": media_type,
            "title": details.get("title") or details.get("name") or best.get("title") or best.get("name"),
            "synopsis": details.get("overview") or best.get("overview") or "",
            "genre_ids": [g.get("id") for g in (details.get("genres") or []) if isinstance(g, dict)],
            "genres": [g.get("name") for g in (details.get("genres") or []) if isinstance(g, dict)],
            "images": {"jpg": {"large_image_url": poster_url}} if poster_url else {},
        }
        logging.debug("tmdb_search: overall selected id=%s title=%s score=%.3f", normalized.get('tmdb_id'), normalized.get('title'), overall_best_score)
        # cache by preference-aware key
        cache_set(cache_key_pref, normalized)
        return normalized
    except Exception:
        return None


def tmdb_search_by_type(title, type_hint=None, allow_when_config_is_jikan=False):
    """Convenience wrapper: decide media_preference based on a textual type hint.
    type_hint: string like 'pelicula', 'serie', 'tv', 'movie' etc. If absent, uses 'auto'.
    Returns the normalized tmdb_search result or None.
    """
    try:
        pref = 'auto'
        if type_hint:
            th = str(type_hint).strip().lower()
            if any(k in th for k in ('serie', 'series', 'tv', 'season')):
                pref = 'tv'
            elif any(k in th for k in ('pelicula', 'movie', 'film')):
                pref = 'movie'
        return tmdb_search(title, media_preference=pref, allow_when_config_is_jikan=allow_when_config_is_jikan)
    except Exception:
        return None


def tmdb_get_episodes(tmdb_id):
    try:
        key = config.get("tmdb_api_key", "")
        if not key:
            return []
        cache_key = f"tmdb_episodes:{tmdb_id}:{key}"
        cached = cache_get(cache_key)
        if cached is not None:
            return cached

        # First, try to fetch TV details. If TMDB responds 404 it likely isn't a TV id.
        try:
            resp = session.get(f"{TMDB_API_BASE}/tv/{tmdb_id}", params={"api_key": key}, timeout=10)
        except Exception as e:
            logging.warning("tmdb_get_episodes: error fetching tv/%s : %s", tmdb_id, e)
            return []

        if resp.status_code == 404:
            logging.info("tmdb_get_episodes: tv/%s returned 404 (not a TV show)", tmdb_id)
            return []

        try:
            resp.raise_for_status()
        except Exception as e:
            logging.warning("tmdb_get_episodes: failed to fetch tv/%s : %s", tmdb_id, e)
            return []

        data = resp.json()
        episodes = []
        seasons = data.get("seasons", []) or []
        for s in seasons:
            season_number = s.get("season_number")
            if season_number == 0:
                continue
            try:
                resp_season = session.get(f"{TMDB_API_BASE}/tv/{tmdb_id}/season/{season_number}", params={"api_key": key}, timeout=10)
                if resp_season.status_code == 404:
                    logging.info("tmdb_get_episodes: season %s for tv/%s returned 404, skipping detailed fetch", season_number, tmdb_id)
                    count = s.get("episode_count") or 0
                    for i in range(1, count + 1):
                        episodes.append({"title": f"Ep {i}"})
                    continue
                resp_season.raise_for_status()
                season_data = resp_season.json()
                for ep in season_data.get("episodes", []) or []:
                    title = ep.get("name") or ep.get("overview") or f"Ep {ep.get('episode_number') or ''}"
                    episodes.append({"title": title})
            except Exception as e:
                logging.debug("tmdb_get_episodes: failed to fetch details for tv/%s season %s: %s", tmdb_id, season_number, e)
                count = s.get("episode_count") or 0
                for i in range(1, count + 1):
                    episodes.append({"title": f"Ep {i}"})

        cache_set(cache_key, episodes)
        return episodes
    except Exception as e:
        logging.warning("tmdb_get_episodes: unexpected error for %s: %s", tmdb_id, e)
        return []

# Note: the provider-facing function `obtener_episodios` is defined earlier in this
# module (near the top) and delegates to `provider_jikan` when available. A previous
# duplicate definition was removed to avoid shadowing and confusion.


def buscar_anime_por_titulo(titulo, folder_path=None, media=None, provider_override=None):
    """Search Jikan (or TMDB if configured) for the best anime match for the given title/folder name.
    If folder_path is provided, this function will look for a mal_id file inside the folder and prefer it.
    If `provider_override` is provided it will be used ('jikan' or 'tmdb'); otherwise `config['metadata_provider']` is used.
    Returns the result dict or None.
    """
    provider = provider_override or config.get("metadata_provider", "jikan")
    try:
        logging.debug("buscar_anime_por_titulo: titulo=%s provider_override=%s config_provider=%s folder=%s", titulo, provider_override, config.get('metadata_provider'), folder_path)
    except Exception:
        pass

    # 1) If a folder-level MAL id exists, prefer fetching from Jikan by id (highest priority)
    if folder_path and pj:
        try:
            mid = pj.find_local_mal_id(folder_path)
            if mid:
                try:
                    data = pj.fetch_anime_by_id(mid)
                    if data:
                        cache_set(f"search:jikan:{titulo}", data)
                        return data
                except Exception:
                    logging.debug("buscar_anime_por_titulo: pj.fetch_anime_by_id failed for %s", mid)
                    pass
        except Exception:
            pass

    # 2) If configured/provider override forces TMDB, try TMDB first
    tmdb_res = None
    cache_key_tmdb = None
    if provider == "tmdb":
        logging.debug("buscar_anime_por_titulo: using TMDB path for title=%s media=%s", titulo, media)
        media_pref = (media or 'auto')
        cache_key_tmdb = f"search:tmdb:{media_pref}:{titulo}"
        cached_tmdb = cache_get(cache_key_tmdb)
        if cached_tmdb:
            return cached_tmdb
        # delegate to provider module if available
        try:
            if pt and hasattr(pt, 'tmdb_search_by_type'):
                tmdb_res = pt.tmdb_search_by_type(titulo, type_hint=media, allow_when_config_is_jikan=True)
            else:
                tmdb_res = tmdb_search(titulo, media_preference=media_pref, allow_when_config_is_jikan=True)
        except Exception:
            tmdb_res = None
    if tmdb_res:
        try:
            tmdb_res["tmdb_id"] = tmdb_res.get("provider_id")
            try:
                genres_map = tmdb_get_genres()
                ids = tmdb_res.get("genre_ids") or []
                names = [genres_map.get(gid, str(gid)) for gid in ids]
                tmdb_res["genres"] = names
            except Exception:
                tmdb_res["genres"] = []
            if cache_key_tmdb:
                cache_set(cache_key_tmdb, tmdb_res)
            return tmdb_res
        except Exception:
            tmdb_res = None

    # If a folder_path is provided, check for an existing mal/id file and prefer it
    if folder_path:
        try:
            # possible filenames in order of preference
            candidates = [
                'id_mal.json', 'id_mal.txt',
                'mal_id.json', 'mal_id.txt',
                'mal.json', 'id.json'
            ]
            import json as _json
            found_mid = None
            found_source = None
            for fn in candidates:
                path = os.path.join(folder_path, fn)
                if not os.path.exists(path):
                    continue
                try:
                    if fn.endswith('.json'):
                        with open(path, 'r', encoding='utf-8') as fh:
                            jd = _json.load(fh)
                            # prefer explicit key 'id_mal'
                            mid = jd.get('id_mal') or jd.get('mal_id') or jd.get('id') or jd.get('mal')
                    else:
                        with open(path, 'r', encoding='utf-8') as fh:
                            mid = fh.read().strip()
                    if mid:
                        # normalize to numeric id if possible
                        try:
                            mid_int = int(mid)
                            found_mid = str(mid_int)
                        except Exception:
                            # try to extract digits
                            import re
                            m = re.search(r"(\d+)", str(mid))
                            if m:
                                found_mid = m.group(1)
                            else:
                                found_mid = str(mid).strip()
                        found_source = path
                        break
                except Exception:
                    continue

            if found_mid:
                try:
                    logging.debug("buscar_anime_por_titulo: found local id file %s -> id=%s", found_source, found_mid)
                    resp = session.get(f"{API_BASE}/anime/{found_mid}", timeout=10)
                    resp.raise_for_status()
                    data = resp.json()
                    if data and data.get('data'):
                        cache_set(f"search:jikan:{titulo}", data['data'])
                        return data['data']
                except Exception:
                    logging.debug("buscar_anime_por_titulo: failed fetching Jikan by id %s (file %s)", found_mid, found_source)
                    pass
        except Exception:
            pass

    # Normalize title into variants derived from folder name
    from src.core.utils import normalize_folder_name_for_search
    variants = normalize_folder_name_for_search(titulo)

    # Try cached result first
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

        for v in variants:
            if not v:
                continue

            # extract season number if present in the variant
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

            # detect roman numerals like II, III
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
                    # collect candidate title strings
                    cand_titles = []
                    try:
                        for t in [cand.get('title') or '', cand.get('title_english') or '', cand.get('title_japanese') or '']:
                            if t:
                                cand_titles.append(t.strip().lower())
                    except Exception:
                        cand_titles = [((cand.get('title') or '') or '').strip().lower()]

                    # include title synonyms
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

                    # season number bonus
                    if season_num:
                        season_found = False
                        for ct in cand_titles:
                            if f"season {season_num}" in ct or re.search(rf"\b{season_num}(st|nd|rd|th)?\b", ct):
                                # crude year-avoid check
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

                    # year bonus
                    try:
                        my = re.search(r"\b(19|20)\d{2}\b", v)
                        if my:
                            year_match_val = my.group(0)
                            aired = cand.get('year') or ''
                            if aired and str(aired).startswith(year_match_val):
                                score += 0.15
                    except Exception:
                        pass

                    # exact english title boost
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
        logging.warning("Error buscando anime '%s' (variants=%s): %s", titulo, variants, e)
        return None


def get_episodes_for_anime(anime):
    if not anime:
        return []
    if anime.get("provider") == "tmdb" or anime.get("tmdb_id"):
        tmdb_id = anime.get("tmdb_id") or anime.get("provider_id")
        if not tmdb_id:
            return []
        # If media_type is present and is not 'tv', avoid querying TV endpoints
        media_type = (anime.get("media_type") or anime.get("type") or "").lower()
        if media_type and media_type != 'tv':
            logging.debug("get_episodes_for_anime: tmdb item %s has media_type=%s, skipping tv episodes fetch", tmdb_id, media_type)
            return []
        cache_key = f"episodes:tmdb:{tmdb_id}"
        cached = cache_get(cache_key)
        if cached is not None:
            return cached
        # delegate to provider module if available
        try:
            if pt and hasattr(pt, 'tmdb_get_episodes'):
                eps = pt.tmdb_get_episodes(tmdb_id)
            else:
                eps = tmdb_get_episodes(tmdb_id)
        except Exception:
            eps = []
        cache_set(cache_key, eps)
        return eps
    if anime.get("mal_id"):
        try:
            if pj and hasattr(pj, 'obtener_episodios'):
                return pj.obtener_episodios(anime.get("mal_id"))
        except Exception:
            pass
        return obtener_episodios(anime.get("mal_id"))
    return []


def buscar_anime_candidates(titulo, limit_per_variant=8, overall_limit=20):
    """Return a list of candidate anime result dicts from Jikan for the given folder name/title.
    This exposes more raw results so callers (e.g., page_builder) can apply custom disambiguation logic.
    """
    # delegate to provider module if available
    try:
        if pj and hasattr(pj, 'buscar_anime_candidates'):
            return pj.buscar_anime_candidates(titulo, limit_per_variant=limit_per_variant, overall_limit=overall_limit)
    except Exception:
        pass
    from utils import normalize_folder_name_for_search
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
                results = data.get('data') or []
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
