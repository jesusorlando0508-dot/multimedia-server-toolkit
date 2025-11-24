import json
import os
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
    base_html_folder = base_html_folder or _config.get('pages_output_dir') or os.path.join(os.path.dirname(__file__), 'Vista', 'pages')
    json_link_prefix = json_link_prefix or _config.get('json_link_prefix', '')
    if output_json_path is None:
        # Prefer an extractor-specific config key so extractor output doesn't overwrite
        # the main `anime.json` used by the site. Fall back to the older key for
        # backward compatibility.
        candidate = _config.get('extractor_anime_json_path') or _config.get('anime_json_path') or os.path.join(os.path.dirname(__file__), 'anime_info.json')
        # If candidate is relative, make it absolute relative to the project dir
        if not os.path.isabs(candidate):
            output_json_path = os.path.abspath(os.path.join(os.path.dirname(__file__), candidate))
        else:
            output_json_path = candidate

    os.makedirs(base_html_folder, exist_ok=True)

    # Collect html files
    html_files = [f for f in os.listdir(base_html_folder) if f.lower().endswith('.html')]

    animes_detalle = []
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
            for idx, li in enumerate(li_items, start=1):
                ep_title = li.text.strip()
                # li may be a NavigableString/PageElement; guard .get access from static checker
                ep_video = li.get('data-src')  # type: ignore[attr-defined]
                if ep_video:
                    episodes.append({'episodeNumber': idx, 'title': ep_title, 'videoPath': ep_video})
                    # emit progress per episode
                    line = f"  - Ep {idx}: {ep_title} -> {ep_video}"
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

        animes_detalle.append({'title': title_pagina, 'link': json_link, 'episodes': episodes, 'totalEpisodes': total_episodes})

    # write JSON
    try:
        # Normalize path directory
        try:
            os.makedirs(os.path.dirname(output_json_path) or '.', exist_ok=True)
        except Exception:
            pass
        # For compatibility with the rest of the app, write a top-level list of entries
        # (page_builder expects a list of anime dicts). Each entry contains title, link and episodes.
        with open(output_json_path, 'w', encoding='utf-8') as f:
            json.dump(animes_detalle, f, ensure_ascii=False, indent=2)
        qput(f"Extractor: JSON escrito en {output_json_path} ({len(animes_detalle)} títulos)")
        if log_callback:
            try:
                log_callback(f"Extractor: JSON escrito en {output_json_path} ({len(animes_detalle)} títulos)")
            except Exception:
                pass
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
    base = _config.get('pages_output_dir') or os.path.join(os.path.dirname(__file__), 'Vista', 'pages')
    out_candidate = _config.get('extractor_anime_json_path') or _config.get('anime_json_path') or os.path.join(os.path.dirname(__file__), 'anime_info.json')
    if not os.path.isabs(out_candidate):
        out = os.path.abspath(os.path.join(os.path.dirname(__file__), out_candidate))
    else:
        out = out_candidate
    res = extract_folder(base, out)
    print(f"Processed {res.get('html_count',0)} HTML files, extracted {res.get('count',0)} titles to {out}")
