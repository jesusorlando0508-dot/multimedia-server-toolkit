import json
import os
from pathlib import Path
from bs4 import BeautifulSoup


def extract_folder(base_html_folder, output_json_path=None, json_link_prefix=None, ui_queue=None, log_callback=None):
    """Extract info from HTML files under base_html_folder and write a summary JSON.

    Sends progress messages to `ui_queue` as ('debug_process', message) if provided or
    if `app_state.ui_queue` is available. Returns the dict that was written.
    """
    # try to use shared ui_queue if none provided
    if ui_queue is None:
        try:
            from src.core.app_state import ui_queue as _shared_q
            ui_queue = _shared_q
        except Exception:
            ui_queue = None

    def qput(msg: str):
        if ui_queue:
            try:
                ui_queue.put(("debug_process", msg))
                return
            except Exception:
                pass
        # fallback to print
        try:
            print(msg)
        except Exception:
            pass

    # Allow caller to pass values; otherwise read from config defaults so paths are centralized
    try:
        from src.core.config import config as _config
    except Exception:
        _config = {}
    base_html_folder = base_html_folder or _config.get('pages_output_dir') or os.path.join(os.path.dirname(__file__), 'pages')
    json_link_prefix = json_link_prefix or _config.get('json_link_prefix', '')
    if output_json_path is None:
        # Prefer an extractor-specific config key so extractor output doesn't overwrite
        # the main `anime.json` used by the site. Fall back to the older key for
        # backward compatibility.
        candidate = _config.get('extractor_anime_json_path') or _config.get('anime_json_path') or os.path.join(os.path.dirname(__file__), 'anime1_info.json')
        # If candidate is relative, make it absolute relative to the project dir
        if not os.path.isabs(candidate):
            output_json_path = os.path.abspath(os.path.join(os.path.dirname(__file__), candidate))
        else:
            output_json_path = candidate

    os.makedirs(base_html_folder, exist_ok=True)

    # Collect html files
    html_files = [f for f in os.listdir(base_html_folder) if f.lower().endswith('.html')]

    # Load existing output JSON (if any) and build lookup by link so we can update/overwrite
    animes_detalle = []
    existing_map = {}
    try:
        if os.path.exists(output_json_path):
            with open(output_json_path, 'r', encoding='utf-8') as _f:
                animes_detalle = json.load(_f) or []
            for e in animes_detalle:
                try:
                    existing_map[e.get('link')] = e
                except Exception:
                    pass
    except Exception:
        animes_detalle = []
        existing_map = {}

    # no translation here: extractor only reads raw fields from HTML
    html_count = 0

    qput(f"Extractor: {len(html_files)} archivos HTML encontrados en {base_html_folder}")

    total_files = len(html_files)
    for idx_file, file in enumerate(sorted(html_files), start=1):
        html_count += 1
        html_path = os.path.join(base_html_folder, file)

        try:
            with open(html_path, 'r', encoding='utf-8') as f:
                soup = BeautifulSoup(f, 'html.parser')
        except Exception as e:
            qput(f"Extractor: no se pudo leer {file}: {e}")
            continue

        # Title
        h1_tag = soup.select_one('header h1')
        title_pagina = h1_tag.text.strip() if h1_tag else os.path.splitext(file)[0]

        qput(f"Procesando [{idx_file}/{total_files}] {file} -> título: {title_pagina}")
        if log_callback:
            try:
                # also notify caller-specific logger
                log_callback(f"Procesando [{idx_file}/{total_files}] {file} -> título: {title_pagina}")
            except Exception:
                pass

        ul_episodes = soup.find('ul', id='videoListEs') or soup.find('ul', id='videoList', class_='episode-list')

        episodes = []
        if ul_episodes:
            # bs4 types may not expose 'find_all' to static checkers; ignore attribute check
            li_items = ul_episodes.find_all('li')  # type: ignore[attr-defined]
            for idx_ep, li in enumerate(li_items, start=1):
                ep_title = li.text.strip()
                ep_video = li.get('data-src')  # type: ignore[attr-defined]
                if ep_video:
                    episodes.append({'episodeNumber': idx_ep, 'title': ep_title, 'videoPath': ep_video})
                    # emit progress per episode
                    line = f"  - Ep {idx_ep}: {ep_title} -> {ep_video}"
                    qput(line)
                    if log_callback:
                        try:
                            log_callback(line)
                        except Exception:
                            pass

        # emit percent progress after processing each file
        try:
            pct = int((idx_file / total_files) * 100)
            qput(f"Extractor: Progreso {pct}% ({idx_file}/{total_files})")
            if log_callback:
                try:
                    log_callback(f"Extractor: Progreso {pct}% ({idx_file}/{total_files})")
                except Exception:
                    pass
        except Exception:
            pass

        total_episodes = len(episodes)

        rel_path = os.path.relpath(html_path, base_html_folder).replace('\\', '/')
        json_link = f"{json_link_prefix}{rel_path}"

        # Extract additional optional metadata (image, synopsis, genres)
        # image via og:image or first img
        image = None
        try:
            meta_img = soup.select_one('meta[property="og:image"]')
            if meta_img and meta_img.get('content'):
                image = meta_img.get('content')
            else:
                img_tag = soup.find('img')
                if img_tag and img_tag.get('src'):
                    image = img_tag.get('src')
        except Exception:
            image = None

        synopsis = None
        try:
            syn = soup.select_one('#synopsis') or soup.select_one('.synopsis') or soup.select_one('p.synopsis') or soup.select_one('div.synopsis')
            if syn:
                synopsis = syn.text.strip()
        except Exception:
            synopsis = None

        # collect genre-like tokens
        genres = []
        try:
            # find containers that likely hold genres/tags
            for sel in ('.genres', '.genre', '.tags', '.tags-list', '.categories'):
                cont = soup.select_one(sel)
                if cont:
                    for a in cont.find_all('a'):
                        t = (a.text or '').strip()
                        if t:
                            genres.append(t)
                    if genres:
                        break
            # fallback: any element with class containing 'genre' and children
            if not genres:
                for el in soup.find_all(class_=lambda c: c and 'genre' in c.lower()):
                    for a in el.find_all('a'):
                        t = (a.text or '').strip()
                        if t:
                            genres.append(t)
        except Exception:
            genres = []

        # Build page data; per-user request we will overwrite existing fields with translated values
        page_data = {'title': title_pagina, 'link': json_link, 'episodes': episodes, 'totalEpisodes': total_episodes}
        if image:
            page_data['image'] = image
        if synopsis:
            page_data['synopsis'] = synopsis
        if genres:
            page_data['genres'] = genres

        # Do not translate: keep raw text from HTML

        # Merge/overwrite into existing data following rules:
        # - If no existing entry -> add new
        # - If existing entry exists:
        #    * If HTML provides a field not present in existing -> set it
        #    * If HTML provides more episodes than existing -> overwrite episodes
        #    * If HTML field appears to be Spanish (heuristic) -> overwrite
        #    * Otherwise keep existing field
        def looks_spanish(s: str) -> bool:
            if not s or not isinstance(s, str):
                return False
            spanish_chars = set('áéíóúñÁÉÍÓÚÑ')
            if any((c in spanish_chars) for c in s):
                return True
            # common short words
            low = s.lower()
            for w in (' el ', ' la ', ' los ', ' las ', ' y ', ' episodio', 'episodio', 'temporada', 'capítulo'):
                if w in (' ' + low + ' '):
                    return True
            return False

        try:
            existing_entry = existing_map.get(json_link)
            if not existing_entry:
                animes_detalle.append(page_data)
                existing_map[json_link] = page_data
            else:
                # update fields selectively
                for k, v in page_data.items():
                    try:
                        if k not in existing_entry or not existing_entry.get(k):
                            # missing or empty -> set
                            existing_entry[k] = v
                            continue
                        # special handling for episodes: overwrite if HTML has more entries
                        if k == 'episodes' and isinstance(v, list) and isinstance(existing_entry.get('episodes'), list):
                            if len(v) > len(existing_entry.get('episodes', [])):
                                existing_entry['episodes'] = v
                            continue
                        # if the HTML-sourced value looks Spanish, overwrite
                        if isinstance(v, str) and looks_spanish(v):
                            existing_entry[k] = v
                            continue
                        # if HTML provides a list with more items than existing, overwrite
                        if isinstance(v, list) and len(v) > len(existing_entry.get(k, []) if isinstance(existing_entry.get(k), list) else []):
                            existing_entry[k] = v
                            continue
                        # otherwise keep existing
                    except Exception:
                        continue
        except Exception:
            animes_detalle.append(page_data)

    # write JSON
    try:
        # Normalize path directory and ensure folder exists
        try:
            os.makedirs(os.path.dirname(output_json_path) or '.', exist_ok=True)
        except Exception:
            pass
        # Replace or append entries in animes_detalle based on existing_map
        # If we loaded existing file initially, ensure we merge updates
        # Write final list (keep original order where possible)
        final_list = []
        # Start with existing entries, update from existing_map
        # If we loaded existing file initially, use it as base
        seen_links = set()
        for e in animes_detalle:
            ln = e.get('link')
            if ln and ln in existing_map:
                final_list.append(existing_map.get(ln))
                seen_links.add(ln)
            else:
                final_list.append(e)
                if ln:
                    seen_links.add(ln)
        # Add any new entries not in the original list
        for ln, ent in existing_map.items():
            if ln not in seen_links:
                final_list.append(ent)

        with open(output_json_path, 'w', encoding='utf-8') as f:
            json.dump(final_list, f, ensure_ascii=False, indent=2)
        qput(f"Extractor: JSON escrito en {output_json_path} ({len(final_list)} títulos)")
        if log_callback:
            try:
                log_callback(f"Extractor: JSON escrito en {output_json_path} ({len(final_list)} títulos)")
            except Exception:
                pass
        # no genre map persistence when extractor is in raw-extract mode
    except Exception as e:
        qput(f"Extractor: error escribiendo JSON: {e}")
        if log_callback:
            try:
                log_callback(f"Extractor: error escribiendo JSON: {e}")
            except Exception:
                pass

    return {'animesDetail': animes_detalle, 'count': len(animes_detalle), 'html_count': html_count}


if __name__ == '__main__':
    # simple CLI behaviour: use configured paths where available
    try:
        from src.core.config import config as _config
    except Exception:
        _config = {}
    base = _config.get('pages_output_dir') or os.path.join(os.path.dirname(__file__), 'pages')
    out_candidate = _config.get('extractor_anime_json_path') or _config.get('anime_json_path') or os.path.join(os.path.dirname(__file__), 'anime1_info.json')
    if not os.path.isabs(out_candidate):
        out = os.path.abspath(os.path.join(os.path.dirname(__file__), out_candidate))
    else:
        out = out_candidate
    res = extract_folder(base, out)
    print(f"Processed {res.get('html_count',0)} HTML files, extracted {res.get('count',0)} titles to {out}")
