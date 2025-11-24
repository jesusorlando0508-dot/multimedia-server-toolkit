import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import logging
import os
import json
from src.core.cache import cache_get, cache_set
from src.core.config import config
from src.core.utils import GENRE_MAP as DICCIONARIO_COMUN, map_tmdb_genres

TMDB_API_BASE = "https://api.themoviedb.org/3"


def create_session_with_retries(total_retries=3, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504)):
    session = requests.Session()
    retries = Retry(total=total_retries, backoff_factor=backoff_factor, status_forcelist=status_forcelist, allowed_methods=frozenset(['GET','POST']))
    adapter = HTTPAdapter(max_retries=retries)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session


session = create_session_with_retries()


def tmdb_get_genres(api_key=None):
    try:
        # prefer explicit api_key, then environment variable (from .env), then config value
        key = api_key or os.environ.get('TMDB_API_KEY', '') or config.get('tmdb_api_key', '')
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


def tmdb_search(query, media_preference='auto', api_key=None, access_token=None, allow_when_config_is_jikan=False):
    try:
        # prefer explicit api_key, then environment variable (from .env), then config value
        key = api_key or os.environ.get('TMDB_API_KEY', '') or config.get('tmdb_api_key', '')
        if not key and not access_token:
            return None

        headers = {}
        params = {"query": "", "page": 1}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        else:
            params["api_key"] = key

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

        cache_key_pref = f"search:tmdb:{media_preference}:{query}"
        cached_pref = cache_get(cache_key_pref)
        if cached_pref is not None:
            return cached_pref

        overall_best = None
        overall_best_score = -1.0
        overall_media_type = None

        queries = [query] if not isinstance(query, (list, tuple)) else [q for q in query if q]

        for candidate in queries:
            query_variants = make_variants(candidate)

            results = []
            media_type = None

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
                            return True
                    except Exception:
                        results = []
                return False

            pref = (media_preference or 'auto').lower()
            if pref == 'movie':
                if try_search('search/movie'):
                    media_type = 'movie'
            elif pref == 'tv':
                if try_search('search/tv'):
                    media_type = 'tv'
            else:
                if try_search('search/movie'):
                    media_type = 'movie'
                else:
                    if try_search('search/tv'):
                        media_type = 'tv'

            if not results:
                continue

            import difflib
            import re
            qnorm = (candidate or "").strip().lower()
            m_year = re.search(r"\b(19|20)\d{2}\b", candidate or "")
            year_match_val = m_year.group(0) if m_year else None
            tv_hint = bool(re.search(r"season\b|s\d{1,2}\b|temporada\b", (candidate or "").lower()))

            best = None
            best_score = -1.0

            tokens = re.findall(r"\w+", qnorm)
            tokens = [t for t in tokens if t and len(t) > 1]

            for cand in results:
                title_candidates = [cand.get("title") or cand.get("name") or "", cand.get("original_title") or cand.get("original_name") or ""]
                cand_title = (title_candidates[0] or "").strip().lower()
                if cand_title == qnorm:
                    best = cand
                    best_score = 1.0
                    break

                if tokens:
                    matched = sum(1 for t in tokens if t in cand_title)
                    token_ratio = matched / len(tokens)
                else:
                    token_ratio = 0.0

                sim = difflib.SequenceMatcher(None, qnorm, cand_title).ratio()

                year_bonus = 0.0
                try:
                    release = cand.get('release_date') or cand.get('first_air_date') or ''
                    if release and year_match_val and release.startswith(year_match_val):
                        year_bonus = 0.2
                except Exception:
                    year_bonus = 0.0

                score = 0.6 * token_ratio + 0.3 * sim + year_bonus
                if tv_hint and (cand.get('media_type') == 'tv' or cand.get('first_air_date')):
                    score += 0.05

                if score > best_score:
                    best_score = score
                    best = cand

            if best is None:
                best = results[0]

            # If we've found an exact-title match (score == 1.0) there may be
            # multiple results with the same normalized title (different years).
            # Prefer the most recent one based on `release_date` or `first_air_date`.
            try:
                if best_score == 1.0 and results:
                    exacts = [r for r in results if ((r.get('title') or r.get('name') or '').strip().lower() == qnorm)]
                    if len(exacts) > 1:
                        def parse_date(d):
                            try:
                                from datetime import datetime
                                return datetime.strptime(d, '%Y-%m-%d') if d else None
                            except Exception:
                                return None

                        best_date = None
                        best_candidate = None
                        for r in exacts:
                            rd = r.get('release_date') or r.get('first_air_date') or ''
                            pd = parse_date(rd)
                            if pd is None:
                                # treat missing dates as very old
                                continue
                            if best_date is None or pd > best_date:
                                best_date = pd
                                best_candidate = r
                        if best_candidate:
                            best = best_candidate
                            best_score = 1.0
            except Exception:
                pass

            try:
                score_for_candidate = float(best_score)
            except Exception:
                score_for_candidate = 0.0
            # Prefer higher score, but break ties using most recent release/air date
            replaced = False
            if score_for_candidate > overall_best_score:
                replaced = True
            elif score_for_candidate == overall_best_score and overall_best is not None:
                try:
                    from datetime import datetime
                    def parse(d):
                        try:
                            return datetime.strptime(d, '%Y-%m-%d') if d else None
                        except Exception:
                            return None
                    best_rd = best.get('release_date') or best.get('first_air_date') or ''
                    overall_rd = overall_best.get('release_date') or overall_best.get('first_air_date') or ''
                    pd_best = parse(best_rd)
                    pd_over = parse(overall_rd)
                    if pd_best and pd_over:
                        if pd_best > pd_over:
                            replaced = True
                    elif pd_best and not pd_over:
                        replaced = True
                except Exception:
                    replaced = False

            if replaced:
                overall_best_score = score_for_candidate
                overall_best = best
                overall_media_type = media_type

        if overall_best is None:
            return None

        best = overall_best
        media_type = overall_media_type or "movie"
        tmdb_id = best.get("id")
        details = best
        try:
            if media_type == "movie":
                det = session.get(f"{TMDB_API_BASE}/movie/{tmdb_id}", params={"api_key": key}, timeout=10)
            else:
                det = session.get(f"{TMDB_API_BASE}/tv/{tmdb_id}", params={"api_key": key}, timeout=10)
            det.raise_for_status()
            details = det.json()
        except Exception:
            details = best

        poster = details.get("poster_path") or best.get("poster_path")
        poster_url = f"https://image.tmdb.org/t/p/original{poster}" if poster else ""

        genre_ids = [g.get("id") for g in (details.get("genres") or []) if isinstance(g, dict)]
        mapped_genres = map_tmdb_genres(",".join(str(gid) for gid in genre_ids))
        normalized = {
            "provider": "tmdb",
            "provider_id": tmdb_id,
            "tmdb_id": tmdb_id,
            "media_type": media_type,
            "title": details.get("title") or details.get("name") or best.get("title") or best.get("name"),
            "synopsis": details.get("overview") or best.get("overview") or "",
            "genre_ids": genre_ids,
            "genres": mapped_genres,
            "images": {"jpg": {"large_image_url": poster_url}} if poster_url else {},
        }
        cache_set(cache_key_pref, normalized)
        return normalized
    except Exception:
        return None


def tmdb_search_by_type(title, type_hint=None, api_key=None, access_token=None, allow_when_config_is_jikan=False):
    try:
        pref = 'auto'
        if type_hint:
            th = str(type_hint).strip().lower()
            if any(k in th for k in ('serie', 'series', 'tv', 'season')):
                pref = 'tv'
            elif any(k in th for k in ('pelicula', 'movie', 'film')):
                pref = 'movie'
        return tmdb_search(title, media_preference=pref, api_key=api_key, access_token=access_token, allow_when_config_is_jikan=allow_when_config_is_jikan)
    except Exception:
        return None


def tmdb_get_episodes(tmdb_id, api_key=None):
    try:
        # prefer explicit api_key, then environment variable (from .env), then config value
        key = api_key or os.environ.get('TMDB_API_KEY', '') or config.get('tmdb_api_key', '')
        if not key:
            return []
        cache_key = f"tmdb_episodes:{tmdb_id}:{key}"
        cached = cache_get(cache_key)
        if cached is not None:
            return cached

        try:
            resp = session.get(f"{TMDB_API_BASE}/tv/{tmdb_id}", params={"api_key": key}, timeout=10)
        except Exception as e:
            logging.warning("provider_tmdb.tmdb_get_episodes: error fetching tv/%s : %s", tmdb_id, e)
            return []

        if resp.status_code == 404:
            return []

        try:
            resp.raise_for_status()
        except Exception as e:
            logging.warning("provider_tmdb.tmdb_get_episodes: failed to fetch tv/%s : %s", tmdb_id, e)
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
                    count = s.get("episode_count") or 0
                    for i in range(1, count + 1):
                        episodes.append({"title": f"Ep {i}"})
                    continue
                resp_season.raise_for_status()
                season_data = resp_season.json()
                for ep in season_data.get("episodes", []) or []:
                    title = ep.get("name") or ep.get("overview") or f"Ep {ep.get('episode_number') or ''}"
                    episodes.append({"title": title})
            except Exception:
                count = s.get("episode_count") or 0
                for i in range(1, count + 1):
                    episodes.append({"title": f"Ep {i}"})

        cache_set(cache_key, episodes)
        return episodes
    except Exception as e:
        logging.warning("provider_tmdb.tmdb_get_episodes: unexpected error for %s: %s", tmdb_id, e)
        return []
