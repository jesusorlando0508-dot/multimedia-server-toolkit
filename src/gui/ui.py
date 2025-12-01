import os
import threading
import tkinter as tk
from tkinter import filedialog, simpledialog, messagebox, ttk
import time
import random
try:
    from natsort import natsorted
except Exception:
    # Best-effort fallback when natsort isn't installed.
    def natsorted(seq):
        try:
            return sorted(seq)
        except Exception:
            return list(seq)
import importlib.util
import tempfile
import tkinter.scrolledtext as scrolledtext

from src.core.app_state import ui_queue, gen_control
import logging
# Initialize UI logging early
try:
    from src.core.ui_logging import setup_ui_logging
    setup_ui_logging()
except Exception:
    # If logging setup fails, continue without UI logging
    pass
from src.core.config import config, load_config, save_config, save_secrets, save_env_key
from src.gui.config_gui import ensure_config_via_gui
from src.core.cache import CACHE_FILE, _ensure_cache
from src.core.utils import limpiar_nombre_archivo, buscar_imagen_local
from src.builder.page_builder import generar_en_hilo_con_tipo, generar_automatico_en_hilo
from src.builder import page_builder
from src.core.network import buscar_anime_por_titulo, tmdb_search
try:
    from src.core import renombrar
except Exception:
    renombrar = None
try:
    from src.translator.translation_cache import get_stats as translation_cache_get_stats
except Exception:
    translation_cache_get_stats = None

# UI elements will be module-level so other helpers can reference them if needed
root = None
label_titulo = None
label_sinopsis = None
etiqueta_imagen = None
label_cache_stats = None
auto_monitor_win = None
auto_monitor_items = {}


def _safe_config(widget, **kwargs):
    """Safely call `widget.config(...)` if widget is not None.

    This helper avoids optional-member access issues that static analyzers
    (Pylance) report when a widget may be `None` in some analysis paths.
    """
    try:
        if widget is not None:
            widget.config(**kwargs)
    except Exception:
        pass


def process_ui_queue(root=None):
    """Procesa mensajes de la cola para actualizar la UI desde el hilo principal."""
    while not ui_queue.empty():
        try:
            item = ui_queue.get(False)
        except Exception:
            break
        if not item:
            continue
        action = item[0]
        if action == "label_text":
            _, widget, text = item
            try:
                widget.config(text=text)
            except Exception:
                pass
            # hide main generation buttons while run is active (so user won't start another full run)
            try:
                bm = globals().get('boton_manual')
                ba = globals().get('boton_auto')
                if bm:
                    try:
                        bm.pack_forget()
                    except Exception:
                        bm.config(state='disabled')
                if ba:
                    try:
                        ba.pack_forget()
                    except Exception:
                        ba.config(state='disabled')
            except Exception:
                pass
        elif action == "translation_status":
            # payload: (action, text) or (action, widget, text)
            try:
                if len(item) >= 3 and hasattr(item[1], 'config'):
                    # backward-compatible: caller provided widget
                    _, widget, text = item
                    try:
                        widget.config(text=text)
                    except Exception:
                        pass
                else:
                    # find label_traduccion in module globals and update
                    try:
                        lbl = globals().get('label_traduccion')
                        # Safely access optional UI entries (they exist only when Ajustes dialog is open)
                        deepl_entry = globals().get('entry_deepl')
                        tmdb_entry = globals().get('entry_tmdb')
                        deepl_val = None
                        tmdb_val = None
                        if deepl_entry is not None:
                            try:
                                deepl_val = deepl_entry.get().strip()
                            except Exception:
                                deepl_val = None
                        if tmdb_entry is not None:
                            try:
                                tmdb_val = tmdb_entry.get().strip()
                            except Exception:
                                tmdb_val = None
                        # Persist DeepL (non-TMDB) to .secrets.json if provided
                        try:
                            if deepl_val:
                                config['deepl_api_key'] = deepl_val
                                save_secrets({'deepl_api_key': deepl_val})
                        except Exception:
                            pass
                        # Persist TMDB API key into .env (so provider code reads from os.environ)
                        try:
                            if tmdb_val:
                                save_env_key('TMDB_API_KEY', tmdb_val)
                            else:
                                # remove key if empty
                                save_env_key('TMDB_API_KEY', None)
                        except Exception:
                            pass
                        # Optionally update translation label if provided in payload
                        if lbl and len(item) >= 2 and isinstance(item[1], str):
                            try:
                                _safe_config(lbl, text=item[1])
                            except Exception:
                                pass
                    except Exception:
                        pass
            except Exception:
                # Defensive: ensure outer try has an except
                pass
        elif action == "show_error":
            _, title, msg = item
            try:
                messagebox.showerror(title, msg)
            except Exception:
                pass
        elif action == "translation_cache":
            # payload: (action, value) e.g. ("translation_cache", "hit", text_snip)
            try:
                _, ev, payload = item
            except Exception:
                ev = None
                payload = None
            try:
                process_ui_queue._cache_hits = getattr(process_ui_queue, '_cache_hits', 0)
                process_ui_queue._cache_misses = getattr(process_ui_queue, '_cache_misses', 0)
                process_ui_queue._cache_sets = getattr(process_ui_queue, '_cache_sets', 0)
                if ev == 'hit':
                    process_ui_queue._cache_hits += 1
                elif ev == 'miss':
                    process_ui_queue._cache_misses += 1
                elif ev == 'set':
                    process_ui_queue._cache_sets += 1
                elif ev == 'batch_set' and isinstance(payload, int):
                    process_ui_queue._cache_sets = getattr(process_ui_queue, '_cache_sets', 0) + int(payload)
                # update label if present
                if 'label_cache_stats' in globals() and globals().get('label_cache_stats') is not None:
                    try:
                        entries = None
                        if translation_cache_get_stats is not None:
                            try:
                                stats = translation_cache_get_stats()
                                entries = stats.get('entries')
                            except Exception:
                                entries = None
                        hits = getattr(process_ui_queue, '_cache_hits', 0)
                        misses = getattr(process_ui_queue, '_cache_misses', 0)
                        sets = getattr(process_ui_queue, '_cache_sets', 0)
                        if entries is None:
                            globals()['label_cache_stats'].config(text=f"Cache: hits={hits} misses={misses} sets={sets}")
                        else:
                            globals()['label_cache_stats'].config(text=f"Cache: entries={entries} | hits={hits} misses={misses}")
                    except Exception:
                        pass
            except Exception:
                pass
        elif action == "translator_progress":
            # payloads: ("cache_hit", 1) or ("cache_summary", {total,hits,misses})
            try:
                _, kind, payload = item
            except Exception:
                kind = None
                payload = None
            try:
                if kind == 'cache_hit':
                    process_ui_queue._cache_hits = getattr(process_ui_queue, '_cache_hits', 0) + 1
                elif kind == 'cache_summary' and isinstance(payload, dict):
                    process_ui_queue._cache_hits = int(payload.get('hits', getattr(process_ui_queue, '_cache_hits', 0)))
                    process_ui_queue._cache_misses = int(payload.get('misses', getattr(process_ui_queue, '_cache_misses', 0)))
                # update label if exists
                if 'label_cache_stats' in globals() and globals().get('label_cache_stats') is not None:
                    try:
                        hits = getattr(process_ui_queue, '_cache_hits', 0)
                        misses = getattr(process_ui_queue, '_cache_misses', 0)
                        globals()['label_cache_stats'].config(text=f"Cache: hits={hits} misses={misses}")
                    except Exception:
                        pass
            except Exception:
                pass
        elif action == "auto_folder_update":
            # payload: (action, folder_path, percent, status)
            try:
                _, folder, percent, status = item
            except Exception:
                folder = None
                percent = None
                status = None
            try:
                imap = globals().get('auto_monitor_items') or {}
                entry = imap.get(folder)
                if entry:
                    lbl, pbar = entry
                    try:
                        if isinstance(percent, int):
                            pbar['value'] = percent
                        _safe_config(lbl, text=f"{os.path.basename(folder)} — {status} {'' if percent is None else f'({percent}%)'}") #type: ignore
                    except Exception:
                        pass
            except Exception:
                pass
        elif action == "progress":
            _, widget, value = item
            try:
                widget['value'] = value
            except Exception:
                pass
        elif action == "debug_log":
            # backward-compatible: older code can send 'debug_log' -> put in Processes tab
            _, msg = item
            try:
                import datetime
                ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                line = msg
                if not msg or not msg[:4].isdigit():
                    line = f"{ts} {msg}"
                if hasattr(process_ui_queue, "debug_process_widget") and process_ui_queue.debug_process_widget:
                    w = process_ui_queue.debug_process_widget
                    w.configure(state='normal')
                    w.insert('end', line + "\n")
                    if getattr(process_ui_queue, "debug_autoscroll", True):
                        w.see('end')
                    w.configure(state='disabled')
                    # update counters
                    try:
                        process_ui_queue._proc_count = getattr(process_ui_queue, '_proc_count', 0) + 1
                        process_ui_queue._last_activity = ts
                    except Exception:
                        pass
            except Exception:
                pass
        elif action == "debug_error":
            _, msg = item
            try:
                import datetime
                ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                line = msg
                if not msg or not msg[:4].isdigit():
                    line = f"{ts} {msg}"
                if hasattr(process_ui_queue, "debug_error_widget") and process_ui_queue.debug_error_widget:
                    w = process_ui_queue.debug_error_widget
                    w.configure(state='normal')
                    w.insert('end', line + "\n")
                    if getattr(process_ui_queue, "debug_autoscroll", True):
                        w.see('end')
                    w.configure(state='disabled')
                    # update counters
                    try:
                        process_ui_queue._err_count = getattr(process_ui_queue, '_err_count', 0) + 1
                        process_ui_queue._last_activity = ts
                    except Exception:
                        pass
            except Exception:
                pass
        elif action == "label_image":
            # payload: (action, widget, image_obj)
            try:
                _, widget, img = item
                try:
                    # set image and keep a reference to avoid GC
                    widget.config(image=img)
                    try:
                        setattr(widget, 'image', img)
                    except Exception:
                        # fallback: attach to globals under a unique name
                        globals().setdefault('_ui_images', []).append(img)
                except Exception:
                    pass
            except Exception:
                pass
        elif action == "debug_process":
            _, msg = item
            try:
                import datetime
                ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                line = msg
                if not msg or not msg[:4].isdigit():
                    line = f"{ts} {msg}"
                if hasattr(process_ui_queue, "debug_process_widget") and process_ui_queue.debug_process_widget:
                    w = process_ui_queue.debug_process_widget
                    w.configure(state='normal')
                    w.insert('end', line + "\n")
                    if getattr(process_ui_queue, "debug_autoscroll", True):
                        w.see('end')
                    w.configure(state='disabled')
                    # update counters
                    try:
                        process_ui_queue._proc_count = getattr(process_ui_queue, '_proc_count', 0) + 1
                        process_ui_queue._last_activity = ts
                    except Exception:
                        pass
            except Exception:
                pass
        elif action == "request_input":
            # payload: {"title": str, "prompt": str, "type": "string", "response_queue": Queue}
            try:
                _, payload = item
                title = payload.get('title') if isinstance(payload, dict) else None
                prompt = payload.get('prompt') if isinstance(payload, dict) else None
                resp_q = payload.get('response_queue') if isinstance(payload, dict) else None
                # Only handle string prompts for now
                if resp_q is not None:
                    try:
                        # show dialog in main thread; use root as parent if available
                        if root:
                            res = simpledialog.askstring(title or 'Input', prompt or '', parent=root)
                        else:
                            res = simpledialog.askstring(title or 'Input', prompt or '')
                        try:
                            resp_q.put(res)
                        except Exception:
                            pass
                    except Exception:
                        try:
                            resp_q.put(None)
                        except Exception:
                            pass
            except Exception:
                pass
        elif action == "generation_finished":
            try:
                # allow UI to remove control buttons
                if hasattr(process_ui_queue, "on_generation_finished") and process_ui_queue.on_generation_finished:
                    try:
                        process_ui_queue.on_generation_finished()
                    except Exception:
                        pass
            except Exception:
                pass
    if root:
        try:
            root.after(200, process_ui_queue, root)
        except Exception:
            pass


def open_debug_panel():
    """Open a small password-protected debug panel that shows debug_log messages."""
    panel = tk.Toplevel(root)
    panel.title("Debug Panel")
    panel.geometry("800x600")

    # Status frame with metrics and search
    status_frame = tk.Frame(panel)
    status_frame.pack(fill='x', padx=6, pady=(6,0))
    lbl_errors_count = tk.Label(status_frame, text="Errors: 0")
    lbl_errors_count.pack(side='left', padx=(0,8))
    lbl_procs_count = tk.Label(status_frame, text="Processes: 0")
    lbl_procs_count.pack(side='left', padx=(0,8))
    lbl_last = tk.Label(status_frame, text="Última actividad: -")
    lbl_last.pack(side='left', padx=(0,8))

    search_frame = tk.Frame(panel)
    search_frame.pack(fill='x', padx=6, pady=(4,6))
    tk.Label(search_frame, text='Buscar:').pack(side='left')
    search_var = tk.StringVar()
    search_entry = tk.Entry(search_frame, textvariable=search_var, width=40)
    search_entry.pack(side='left', padx=(4,6))
    def do_search():
        query = search_var.get().strip()
        if not query:
            return
        # determine active tab
        idx = notebook.index('current')
        target = err_txt if idx == 0 else proc_txt
        # clear previous highlights
        try:
            target.tag_remove('hl', '1.0', 'end')
        except Exception:
            pass
        start = '1.0'
        while True:
            pos = target.search(query, start, stopindex='end', nocase=True)
            if not pos:
                break
            end = f"{pos}+{len(query)}c"
            try:
                target.tag_add('hl', pos, end)
            except Exception:
                pass
            start = end
        try:
            target.tag_config('hl', background='yellow')
        except Exception:
            pass

    tk.Button(search_frame, text='Buscar', command=do_search).pack(side='left', padx=(2,6))
    def clear_highlights():
        try:
            err_txt.tag_remove('hl', '1.0', 'end')
            proc_txt.tag_remove('hl', '1.0', 'end')
        except Exception:
            pass
    tk.Button(search_frame, text='Clear Highlights', command=clear_highlights).pack(side='left')

    # Notebook with two tabs: Errors and Processes
    notebook = ttk.Notebook(panel)
    notebook.pack(expand=True, fill='both', padx=6, pady=6)

    tab_errors = tk.Frame(notebook)
    tab_processes = tk.Frame(notebook)
    notebook.add(tab_errors, text='Errors')
    notebook.add(tab_processes, text='Processes')

    # Errors text widget
    err_txt = tk.Text(tab_errors, wrap='word', state='disabled')
    err_txt.pack(expand=True, fill='both', padx=6, pady=(6,0))
    err_controls = tk.Frame(tab_errors)
    err_controls.pack(fill='x', padx=6, pady=(0,6))
    def on_clear_errors():
        err_txt.configure(state='normal')
        err_txt.delete('1.0', 'end')
        err_txt.configure(state='disabled')
    def on_export_errors():
        try:
            logdir = config.get('debug_log_dir') or os.getcwd()
            os.makedirs(logdir, exist_ok=True)
            fname = os.path.join(logdir, f"errors_{int(time.time())}.log")
            with open(fname, 'w', encoding='utf-8') as ef:
                ef.write(err_txt.get('1.0', 'end'))
            messagebox.showinfo('Export', f'Errors exported to: {fname}')
        except Exception as e:
            messagebox.showwarning('Export', f'No se pudo exportar errors: {e}')

    tk.Button(err_controls, text='Export Errors', command=on_export_errors).pack(side='right')
    tk.Button(err_controls, text='Clear Errors', command=on_clear_errors).pack(side='right')

    # Processes text widget
    proc_txt = tk.Text(tab_processes, wrap='word', state='disabled')
    proc_txt.pack(expand=True, fill='both', padx=6, pady=(6,0))
    proc_controls = tk.Frame(tab_processes)
    proc_controls.pack(fill='x', padx=6, pady=(0,6))
    autoscroll_var = tk.BooleanVar(value=True)
    def on_autoscroll_change():
        process_ui_queue.debug_autoscroll = autoscroll_var.get()
    tk.Checkbutton(proc_controls, text='Autoscroll', variable=autoscroll_var, command=on_autoscroll_change).pack(side='left')
    def on_clear_processes():
        proc_txt.configure(state='normal')
        proc_txt.delete('1.0', 'end')
        proc_txt.configure(state='disabled')
    def on_export_processes():
        try:
            logdir = config.get('debug_log_dir') or os.getcwd()
            os.makedirs(logdir, exist_ok=True)
            fname = os.path.join(logdir, f"processes_{int(time.time())}.log")
            with open(fname, 'w', encoding='utf-8') as pf:
                pf.write(proc_txt.get('1.0', 'end'))
            messagebox.showinfo('Export', f'Processes exported to: {fname}')
        except Exception as e:
            messagebox.showwarning('Export', f'No se pudo exportar processes: {e}')

    tk.Button(proc_controls, text='Export Processes', command=on_export_processes).pack(side='right')
    tk.Button(proc_controls, text='Clear Processes', command=on_clear_processes).pack(side='right')

    # expose the text widgets to process_ui_queue so messages are appended
    process_ui_queue.debug_error_widget = err_txt
    process_ui_queue.debug_process_widget = proc_txt
    process_ui_queue.debug_autoscroll = autoscroll_var.get()

    # Update status labels periodically
    def update_status():
        try:
            errc = getattr(process_ui_queue, '_err_count', 0)
            proc = getattr(process_ui_queue, '_proc_count', 0)
            last = getattr(process_ui_queue, '_last_activity', '-')
            lbl_errors_count.config(text=f"Errors: {errc}")
            lbl_procs_count.config(text=f"Processes: {proc}")
            lbl_last.config(text=f"Última actividad: {last}")
        except Exception:
            pass
        try:
            panel.after(1000, update_status)
        except Exception:
            pass

    update_status()

    # Cleanup when closing panel
    def _on_close_panel():
        try:
            process_ui_queue.debug_error_widget = None
            process_ui_queue.debug_process_widget = None
            process_ui_queue.debug_autoscroll = True
        except Exception:
            pass
        try:
            panel.destroy()
        except Exception:
            pass

    panel.protocol('WM_DELETE_WINDOW', _on_close_panel)


def main():
    global root, label_titulo, label_sinopsis, etiqueta_imagen
    root = tk.Tk()
    root.title("Generador de Páginas")
    root.geometry("600x700")
    # Load application icon if available (prefer .ico on Windows, .png otherwise)
    try:
        icon_path = None
        # allow user to override via config
        try:
            icon_path = config.get('app_icon_path')
        except Exception:
            icon_path = None
        if not icon_path:
            # try common filenames
            base = os.path.dirname(__file__)
            if os.name == 'nt':
                cand = os.path.join(base, 'icon.ico')
            else:
                cand = os.path.join(base, 'icon.png')
            if os.path.exists(cand):
                icon_path = cand
        if icon_path and os.path.exists(icon_path):
            if icon_path.lower().endswith('.ico') and os.name == 'nt':
                try:
                    root.iconbitmap(icon_path)
                except Exception:
                    pass
            else:
                try:
                    img = tk.PhotoImage(file=icon_path)
                    root.iconphoto(True, img)
                    # keep reference to avoid GC; use setattr to avoid static attribute warnings
                    try:
                        setattr(root, '_icon_image', img)
                    except Exception:
                        # fallback: keep a module-level reference
                        try:
                            globals()['_icon_image_ref'] = img
                        except Exception:
                            pass
                except Exception:
                    pass
    except Exception:
        pass

    # Menu bar with access to configuration GUI
    def open_config_and_apply():
        try:
            new_cfg = ensure_config_via_gui(config)
            if new_cfg and isinstance(new_cfg, dict):
                # update runtime config dict in-place so other modules see changes
                try:
                    config.clear()
                    config.update(new_cfg)
                except Exception:
                    pass
                try:
                    save_config(config)
                except Exception:
                    pass
                try:
                    _safe_config(label_backend, text=f"Traductor: {config.get('translator_backend', 'local')}")
                except Exception:
                    pass
                try:
                    _safe_config(label_provider, text=f"Metadata: {config.get('metadata_provider', 'jikan')}")
                except Exception:
                    pass
        except Exception:
            pass

    menubar = tk.Menu(root)
    file_menu = tk.Menu(menubar, tearoff=0)
    # Use 'Ajustes' as the default label in the Archivo menu
    file_menu.add_command(label='Ajustes', command=open_config_and_apply)
    menubar.add_cascade(label='Configuracion', menu=file_menu)
    try:
        root.config(menu=menubar)
    except Exception:
        pass

    frame = tk.Frame(root, padx=10, pady=10)
    frame.pack(expand=True, fill="both")

    barra_progreso = ttk.Progressbar(frame, orient="horizontal", length=500, mode="determinate")
    barra_progreso.pack(pady=10)

    label_estado = tk.Label(frame, text="Esperando...", font=("Arial", 10))
    label_estado.pack(pady=5)

    # Translation status shown under progress bar (e.g., "Traduciendo capítulos...")
    label_traduccion = tk.Label(frame, text="", font=("Arial", 9), fg="#333333")
    label_traduccion.pack(pady=(0,6))

    # Preview
    frame_preview = tk.Frame(frame)
    frame_preview.pack(pady=5)
    label_titulo = tk.Label(frame_preview, text="", font=("Arial", 12, "bold"))
    label_titulo.pack()
    label_sinopsis = tk.Label(frame_preview, text="", font=("Arial", 10), wraplength=400, justify="left")
    label_sinopsis.pack()
    etiqueta_imagen = tk.Label(frame_preview)
    etiqueta_imagen.pack(pady=5)

    # Meta label
    label_meta = tk.Label(frame_preview, text="", font=("Arial", 9), fg="#666666")
    label_meta.pack(pady=2)

    def reset_ui_state():
        """Reset UI to initial idle state (clear progress, labels, image)."""
        try:
            barra_progreso['value'] = 0
        except Exception:
            pass
        try:
            _safe_config(label_estado, text='Esperando...')
        except Exception:
            pass
        try:
            _safe_config(label_titulo, text='')
        except Exception:
            pass
        try:
            _safe_config(label_sinopsis, text='')
        except Exception:
            pass
        try:
            _safe_config(label_meta, text='')
        except Exception:
            pass
        try:
            if etiqueta_imagen is not None:
                _safe_config(etiqueta_imagen, image='')
                if hasattr(etiqueta_imagen, 'image'):
                    delattr(etiqueta_imagen, 'image')
        except Exception:
            pass

    def abrir_ajustes():
        ventana_ajustes = tk.Toplevel(root)
        ventana_ajustes.title("Ajustes")
        ventana_ajustes.geometry("600x700")

        # Create a scrollable area so long settings forms can be scrolled vertically
        canvas = tk.Canvas(ventana_ajustes, borderwidth=0, highlightthickness=0)
        vsb = tk.Scrollbar(ventana_ajustes, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)

        inner = tk.Frame(canvas)
        canvas.create_window((0, 0), window=inner, anchor='nw')

        def _on_frame_configure(event):
            canvas.configure(scrollregion=canvas.bbox('all'))

        inner.bind('<Configure>', _on_frame_configure)

        # mousewheel support
        def _on_mousewheel(event):
            # Defensive: avoid calling methods on a destroyed widget.
            try:
                # For Windows / Mac (event.delta present)
                if getattr(event, 'delta', 0):
                    canvas.yview_scroll(int(-1*(event.delta/120)), 'units')
                else:
                    # For Linux (event.num 4 = up, 5 = down)
                    if getattr(event, 'num', None) == 4:
                        canvas.yview_scroll(-1, 'units')
                    elif getattr(event, 'num', None) == 5:
                        canvas.yview_scroll(1, 'units')
            except tk.TclError:
                # Widget was likely destroyed; ignore the event.
                return

        # Bind mousewheel handlers when the pointer is over the scrollable canvas
        def _bind_mousewheel(event):
            canvas.bind('<MouseWheel>', _on_mousewheel)
            canvas.bind('<Button-4>', _on_mousewheel)
            canvas.bind('<Button-5>', _on_mousewheel)

        def _unbind_mousewheel(event):
            try:
                canvas.unbind('<MouseWheel>')
                canvas.unbind('<Button-4>')
                canvas.unbind('<Button-5>')
            except Exception:
                pass

        canvas.bind('<Enter>', _bind_mousewheel)
        canvas.bind('<Leave>', _unbind_mousewheel)

        tk.Label(inner, text="Media root directory:").pack(anchor='w', padx=10, pady=(10,0))
        entry_media_root = tk.Entry(inner, width=60)
        entry_media_root.insert(0, config.get('media_root_dir', ''))
        entry_media_root.pack(padx=10, pady=2)
        def choose_media_root():
            d = filedialog.askdirectory(title='Selecciona la carpeta raíz de media (contiene subfolders de anime/películas)')
            if d:
                entry_media_root.delete(0, 'end')
                entry_media_root.insert(0, os.path.abspath(d))
        tk.Button(inner, text='Seleccionar media root', command=choose_media_root).pack(padx=10, pady=(0,4))

        tk.Label(inner, text="Pages output dir:").pack(anchor='w', padx=10, pady=(8,0))
        entry_pages = tk.Entry(inner, width=60)
        entry_pages.insert(0, config.get('pages_output_dir', ''))
        entry_pages.pack(padx=10, pady=2)
        def choose_pages_dir():
            d = filedialog.askdirectory(title='Selecciona la carpeta de salida para las páginas (pages)')
            if d:
                entry_pages.delete(0, 'end')
                entry_pages.insert(0, os.path.abspath(d))
        tk.Button(inner, text='Seleccionar pages', command=choose_pages_dir).pack(padx=10, pady=(0,4))

        tk.Label(inner, text="JSON link prefix:").pack(anchor='w', padx=10, pady=(8,0))
        entry_jsonprefix = tk.Entry(inner, width=60)
        entry_jsonprefix.insert(0, config.get('json_link_prefix', ''))
        entry_jsonprefix.pack(padx=10, pady=2)

        tk.Label(inner, text="Translator backend (local/deepl):").pack(anchor='w', padx=10, pady=(8,0))
        entry_backend = tk.Entry(inner, width=20)
        entry_backend.insert(0, config.get('translator_backend', 'local'))
        entry_backend.pack(padx=10, pady=2)

        tk.Label(inner, text="DeepL API Key (optional):").pack(anchor='w', padx=10, pady=(8,0))
        entry_deepl = tk.Entry(inner, width=40, show='*')
        entry_deepl.insert(0, config.get('deepl_api_key', ''))
        entry_deepl.pack(padx=10, pady=2)

        tk.Label(inner, text="Metadata provider:").pack(anchor='w', padx=10, pady=(8,0))
        provider_var = tk.StringVar(value=config.get('metadata_provider', 'jikan'))
        entry_provider = ttk.Combobox(inner, textvariable=provider_var, values=['jikan', 'tmdb'], state='readonly', width=20)
        entry_provider.pack(padx=10, pady=2)

        tk.Label(inner, text="TMDB API Key (optional):").pack(anchor='w', padx=10, pady=(8,0))
        entry_tmdb = tk.Entry(inner, width=40, show='*')
        # prefer the environment variable so UI reflects the runtime environment
        entry_tmdb.insert(0, os.environ.get('TMDB_API_KEY', ''))
        entry_tmdb.pack(padx=10, pady=2)

        # Additional selectable paths: template, tmdb_overrides, tmdb_gen, extractor, cache dir
        tk.Label(inner, text="Template HTML path:").pack(anchor='w', padx=10, pady=(8,0))
        entry_template = tk.Entry(inner, width=60)
        entry_template.insert(0, config.get('template_path', os.path.join(os.path.dirname(__file__), 'template.html')))
        entry_template.pack(padx=10, pady=2)
        def choose_template_path():
            p = filedialog.askopenfilename(title='Selecciona template.html', filetypes=[('HTML','*.html'),('All','*.*')])
            if p:
                entry_template.delete(0, 'end')
                entry_template.insert(0, p)
        tk.Button(inner, text='Seleccionar template', command=choose_template_path).pack(padx=10, pady=(0,4))

        tk.Label(inner, text="TMDB overrides JSON (tmdb_overrides.json):").pack(anchor='w', padx=10, pady=(8,0))
        entry_overrides = tk.Entry(inner, width=60)
        entry_overrides.insert(0, config.get('tmdb_overrides_path', os.path.join(os.path.dirname(__file__), 'tmdb_overrides.json')))
        entry_overrides.pack(padx=10, pady=2)
        def choose_overrides_path():
            p = filedialog.askopenfilename(title='Selecciona tmdb_overrides.json', filetypes=[('JSON','*.json'),('All','*.*')])
            if p:
                entry_overrides.delete(0, 'end')
                entry_overrides.insert(0, p)
        tk.Button(inner, text='Seleccionar overrides', command=choose_overrides_path).pack(padx=10, pady=(0,4))

        tk.Label(inner, text="TMDB genres mapping (tmdb_gen.json):").pack(anchor='w', padx=10, pady=(8,0))
        entry_tmdb_gen = tk.Entry(inner, width=60)
        entry_tmdb_gen.insert(0, config.get('tmdb_gen_path', os.path.join(os.path.dirname(__file__), 'tmdb_gen.json')))
        entry_tmdb_gen.pack(padx=10, pady=2)
        def choose_tmdb_gen_path():
            p = filedialog.askopenfilename(title='Selecciona tmdb_gen.json', filetypes=[('JSON','*.json'),('All','*.*')])
            if p:
                entry_tmdb_gen.delete(0, 'end')
                entry_tmdb_gen.insert(0, p)
        tk.Button(inner, text='Seleccionar tmdb_gen', command=choose_tmdb_gen_path).pack(padx=10, pady=(0,4))

        tk.Label(inner, text="Cache directory (where .cache is stored):").pack(anchor='w', padx=10, pady=(8,0))
        entry_cache_dir = tk.Entry(inner, width=60)
        entry_cache_dir.insert(0, config.get('cache_dir', os.path.join(os.path.dirname(__file__), '.cache')))
        entry_cache_dir.pack(padx=10, pady=2)
        def choose_cache_dir():
            d = filedialog.askdirectory(title='Selecciona carpeta de cache')
            if d:
                entry_cache_dir.delete(0, 'end')
                entry_cache_dir.insert(0, d)
        tk.Button(inner, text='Seleccionar cache dir', command=choose_cache_dir).pack(padx=10, pady=(0,4))

        def aplicar_ajustes():
            media_root = entry_media_root.get().strip()
            if media_root and not os.path.isabs(media_root):
                media_root = os.path.abspath(media_root)
            pages_dir = entry_pages.get().strip()
            if pages_dir and not os.path.isabs(pages_dir):
                pages_dir = os.path.abspath(pages_dir)
            config['media_root_dir'] = media_root
            config['pages_output_dir'] = pages_dir
            config['json_link_prefix'] = entry_jsonprefix.get().strip()
            config['translator_backend'] = entry_backend.get().strip()
            # Do not store sensitive API keys in config.json. Save them to .secrets.json instead.
            secret_updates = {
                'deepl_api_key': entry_deepl.get().strip(),
                'tmdb_api_key': entry_tmdb.get().strip(),
                'tmdb_access_token': config.get('tmdb_access_token', ''),
            }
            # Merge secrets into runtime config so they are available immediately
            try:
                for k, v in secret_updates.items():
                    if v is not None:
                        config[k] = v
                # persist secrets to protected file
                try:
                    save_secrets(secret_updates)
                except Exception:
                    pass
            except Exception:
                pass
            config['metadata_provider'] = entry_provider.get().strip()
            # tmdb_api_key is saved as a secret (handled above during aplicar_ajustes)
            # save optional paths
            try:
                config['template_path'] = entry_template.get().strip()
            except Exception:
                pass
            try:
                config['tmdb_overrides_path'] = entry_overrides.get().strip()
            except Exception:
                pass
            try:
                config['tmdb_gen_path'] = entry_tmdb_gen.get().strip()
            except Exception:
                pass
            try:
                config['cache_dir'] = entry_cache_dir.get().strip()
            except Exception:
                pass
            messagebox.showinfo('Ajustes', 'Ajustes aplicados para la sesión.')

            try:
                _safe_config(label_backend, text=f"Traductor: {config.get('translator_backend', 'local')}")
            except Exception:
                pass
            try:
                _safe_config(label_provider, text=f"Metadata: {config.get('metadata_provider', 'jikan')}")
            except Exception:
                pass

        def guardar_ajustes():
            aplicar_ajustes()
            try:
                os.makedirs(config['pages_output_dir'], exist_ok=True)
            except Exception as e:
                messagebox.showwarning('Ajustes', f'No se pudo crear la carpeta de salida: {e}')
            # Save non-sensitive settings to config.json; secrets are already persisted separately
            try:
                save_config(config)
            except Exception:
                pass
            messagebox.showinfo('Ajustes', 'Ajustes guardados en config.json')

        def limpiar_cache():
            try:
                if os.path.exists(CACHE_FILE):
                    os.remove(CACHE_FILE)
                _ensure_cache()
                messagebox.showinfo('Cache', 'Cache de metadata limpiada.')
            except Exception as e:
                messagebox.showwarning('Cache', f'No se pudo limpiar la cache: {e}')

    # The Ajustes action is available from the 'Archivo' menu. Remove the separate button to simplify the UI.

    # Dev / Debug button (no password required).
    def abrir_dev_mode():
        open_debug_panel()

    tk.Button(frame, text="Dev Mode", command=abrir_dev_mode, font=("Arial", 10)).pack(pady=2)

    # Shortcut buttons: Renombrar and Extractor (open lightweight GUIs)
    def abrir_renombrar_wrapper():
        # Toplevel that uses renombrar.procesar and set_logger to show output
        win = tk.Toplevel(root)
        win.title('Renombrar (Preview / Aplicar)')
        win.geometry('700x600')

        log_box = scrolledtext.ScrolledText(win, width=90, height=20, state='normal')
        log_box.pack(padx=8, pady=8, fill='both', expand=True)

        folder_var = tk.StringVar(value='')
        def choose_folder():
            f = filedialog.askdirectory(title='Selecciona carpeta a renombrar')
            if f:
                folder_var.set(f)
        frm = tk.Frame(win)
        frm.pack(fill='x', padx=8, pady=(0,6))
        tk.Button(frm, text='Seleccionar carpeta', command=choose_folder).pack(side='left')
        tk.Label(frm, textvariable=folder_var).pack(side='left', padx=6)

        auto_confirm_var = tk.BooleanVar(value=True)
        dry_run_var = tk.BooleanVar(value=True)
        tk.Checkbutton(frm, text='Auto confirm (no ask)', variable=auto_confirm_var).pack(side='left', padx=6)
        tk.Checkbutton(frm, text='Dry-run', variable=dry_run_var).pack(side='left', padx=6)

        def gui_log(msg):
            try:
                log_box.insert('end', str(msg) + "\n")
                log_box.see('end')
            except Exception:
                pass

        def run_preview():
            folder = folder_var.get()
            if not folder:
                messagebox.showwarning('Renombrar', 'Selecciona una carpeta primero')
                return
            # hook logger
            try:
                if renombrar:
                    renombrar.set_logger(gui_log)
            except Exception:
                pass
            log_box.delete('1.0', 'end')
            # Ensure renombrar module provides `procesar` before calling
            proc = getattr(renombrar, 'procesar', None)
            if proc:
                threading.Thread(target=lambda: proc(folder, auto_confirm=auto_confirm_var.get(), dry_run=dry_run_var.get()), daemon=True).start()
            else:
                messagebox.showerror('Renombrar', 'El módulo renombrar no está disponible o no implementa procesar().')

        def run_apply():
            if not renombrar:
                messagebox.showerror('Renombrar', 'El módulo renombrar no está disponible.')
                return
            folder = folder_var.get()
            if not folder:
                messagebox.showwarning('Renombrar', 'Selecciona una carpeta primero')
                return
            if dry_run_var.get():
                if not messagebox.askyesno('Confirmar', 'Está activo Dry-run. Cambiar a modo aplicar?'):
                    return
            if not messagebox.askyesno('Confirmar', '¿Seguro que deseas aplicar los cambios en la carpeta seleccionada?'):
                return
            try:
                renombrar.set_logger(gui_log)
            except Exception:
                pass
            proc = getattr(renombrar, 'procesar', None)
            if proc:
                threading.Thread(target=lambda: proc(folder, auto_confirm=True, dry_run=False), daemon=True).start()
            else:
                messagebox.showerror('Renombrar', 'El módulo renombrar no está disponible o no implementa procesar().')

        btns = tk.Frame(win)
        btns.pack(fill='x', padx=8, pady=(0,8))
        tk.Button(btns, text='Preview (dry-run)', command=run_preview).pack(side='left', padx=6)
        tk.Button(btns, text='Aplicar (run)', command=run_apply).pack(side='left', padx=6)


    def abrir_extractor_wrapper():
        # Toplevel that calls extractor_html2.2.extract_folder via path import
        win = tk.Toplevel(root)
        win.title('Extractor HTML -> JSON')
        win.geometry('700x600')

        frm = tk.Frame(win)
        frm.pack(fill='x', padx=8, pady=8)
        base_var = tk.StringVar(value=config.get('pages_output_dir', os.path.join(os.getcwd(), 'pages')))
        out_var = tk.StringVar(value='')
        tk.Label(frm, text='Base HTML folder:').grid(row=0, column=0, sticky='w')
        tk.Entry(frm, textvariable=base_var, width=60).grid(row=0, column=1, padx=6)
        def choose_base():
            d = filedialog.askdirectory(title='Selecciona carpeta pages')
            if d:
                base_var.set(d)
        tk.Button(frm, text='Seleccionar', command=choose_base).grid(row=0, column=2, padx=4)

        tk.Label(frm, text='Output JSON (opcional):').grid(row=1, column=0, sticky='w')
        tk.Entry(frm, textvariable=out_var, width=60).grid(row=1, column=1, padx=6)
        def choose_out():
            f = filedialog.asksaveasfilename(title='Guardar JSON como', defaultextension='.json', filetypes=[('JSON','*.json')])
            if f:
                out_var.set(f)
        tk.Button(frm, text='Seleccionar', command=choose_out).grid(row=1, column=2, padx=4)

        preview_box = scrolledtext.ScrolledText(win, width=90, height=12, state='normal')
        preview_box.pack(padx=8, pady=(0,8), fill='both', expand=True)

        def import_extractor():
            # load module by path to avoid name issues
            try:
                path = os.path.join(os.path.dirname(__file__), 'extractor_html2.2.py')
                spec = importlib.util.spec_from_file_location('extractor_for_ui', path)
                if spec is None or spec.loader is None:
                    raise ImportError(f'No se pudo cargar el extractor desde {path} (spec loader faltante)')
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                return mod
            except Exception as e:
                messagebox.showerror('Extractor', f'Error importando extractor: {e}')
                return None

        def run_preview():
            mod = import_extractor()
            if not mod:
                return
            base = base_var.get()
            # use temp file so we don't overwrite user's JSON
            tf = tempfile.NamedTemporaryFile(delete=False, suffix='.json')
            tf.close()

            def append_preview(msg: str):
                try:
                    preview_box.configure(state='normal')
                    preview_box.insert('end', msg + "\n")
                    preview_box.see('end')
                    preview_box.configure(state='disabled')
                except Exception:
                    pass

            # pass a log_callback that safely appends to the preview_box from any thread
            def log_cb(msg: str):
                try:
                    preview_box.after(0, append_preview, msg)
                except Exception:
                    pass

            preview_box.delete('1.0', 'end')
            threading.Thread(target=lambda: mod.extract_folder(base_html_folder=base, output_json_path=tf.name, json_link_prefix=config.get('json_link_prefix'), ui_queue=ui_queue, log_callback=log_cb), daemon=True).start()

        def run_apply():
            mod = import_extractor()
            if not mod:
                return
            base = base_var.get()
            outp = out_var.get() or os.path.join(base, 'anime1_info.json')
            if not messagebox.askyesno('Confirmar', f'Escribir JSON en: {outp}\n¿Continuar?'):
                return

            def append_preview(msg: str):
                try:
                    preview_box.configure(state='normal')
                    preview_box.insert('end', msg + "\n")
                    preview_box.see('end')
                    preview_box.configure(state='disabled')
                except Exception:
                    pass

            def log_cb(msg: str):
                try:
                    preview_box.after(0, append_preview, msg)
                except Exception:
                    pass

            preview_box.delete('1.0', 'end')
            # run in background so UI remains responsive
            threading.Thread(target=lambda: mod.extract_folder(base_html_folder=base, output_json_path=outp, json_link_prefix=config.get('json_link_prefix'), ui_queue=ui_queue, log_callback=log_cb), daemon=True).start()

        btns = tk.Frame(win)
        btns.pack(fill='x', padx=8, pady=(0,8))
        tk.Button(btns, text='Preview (no sobrescribir)', command=run_preview).pack(side='left', padx=6)
        tk.Button(btns, text='Ejecutar (escribir JSON)', command=run_apply).pack(side='left', padx=6)

    # Move shortcut actions into the 'Archivo' menu to keep the main UI clean.
    try:
        file_menu.add_separator()
        file_menu.add_command(label='Renombrar', command=lambda: abrir_renombrar_wrapper())
        file_menu.add_command(label='Extractor', command=lambda: abrir_extractor_wrapper())
        file_menu.add_command(label='Dev Mode', command=lambda: abrir_dev_mode())
    except Exception:
        # If menu isn't available for some reason, fall back to inline buttons
        try:
            tk.Button(frame, text="Renombrar", command=abrir_renombrar_wrapper, font=("Arial", 10)).pack(pady=2)
            tk.Button(frame, text="Extractor", command=abrir_extractor_wrapper, font=("Arial", 10)).pack(pady=2)
            tk.Button(frame, text="Dev Mode", command=abrir_dev_mode, font=("Arial", 10)).pack(pady=2)
        except Exception:
            pass

    # Iniciar procesador de cola UI
    root.after(200, process_ui_queue, root)

    # prepare hooks used by process_ui_queue for debug and generation finish
    process_ui_queue.debug_error_widget = None
    process_ui_queue.debug_process_widget = None
    process_ui_queue.debug_autoscroll = True
    process_ui_queue.on_generation_finished = None

    # Persistent generation control buttons (always visible)
    control_frame_persistent = tk.Frame(frame)
    control_frame_persistent.pack(pady=6)

    btn_pause = tk.Button(control_frame_persistent, text="Pausar", state='disabled')
    btn_resume = tk.Button(control_frame_persistent, text="Reanudar", state='disabled')
    btn_stop = tk.Button(control_frame_persistent, text="Detener", state='disabled')

    def _on_pause():
        try:
            gen_control.pause()
            btn_pause.config(state='disabled')
            btn_resume.config(state='normal')
            ui_queue.put(("label_text", label_estado, "⏸️ Pausado por el usuario."))
        except Exception:
            pass

    def _on_resume():
        try:
            gen_control.resume()
            btn_pause.config(state='normal')
            btn_resume.config(state='disabled')
            ui_queue.put(("label_text", label_estado, "▶️ Reanudado."))
        except Exception:
            pass

    def _on_stop():
        try:
            gen_control.stop()
            btn_pause.config(state='disabled')
            btn_resume.config(state='disabled')
            btn_stop.config(state='disabled')
            ui_queue.put(("label_text", label_estado, "⛔ Solicitado detener el proceso..."))
            # reset UI to idle immediately
            try:
                reset_ui_state()
            except Exception:
                pass
        except Exception:
            pass

    btn_pause.config(command=_on_pause)
    btn_resume.config(command=_on_resume)
    btn_stop.config(command=_on_stop)

    btn_pause.pack(side='left', padx=6)
    btn_resume.pack(side='left', padx=6)
    btn_stop.pack(side='left', padx=6)

    # Mostrar backend de traducción actual
    label_backend = tk.Label(frame, text=f"Traductor: {config.get('translator_backend', 'local')}")
    label_backend.pack(pady=2)
    # Mostrar proveedor de metadata actual
    label_provider = tk.Label(frame, text=f"Metadata: {config.get('metadata_provider', 'jikan')}")
    label_provider.pack(pady=2)

    # Cache stats (hits/misses/entries) — updated via ui_queue events
    try:
        label_cache_stats = tk.Label(frame, text="Cache: entries=? hits=0 misses=0", font=("Arial", 9), fg="#444444")
        label_cache_stats.pack(pady=2)
    except Exception:
        label_cache_stats = None

    # Dropdown en la ventana principal para seleccionar proveedor (jikan/tmdb)
    provider_frame = tk.Frame(frame)
    provider_frame.pack(pady=2)
    tk.Label(provider_frame, text="Proveedor:").pack(side='left', padx=(0,6))
    provider_var_main = tk.StringVar(value=config.get('metadata_provider', 'jikan'))
    provider_select = ttk.Combobox(provider_frame, textvariable=provider_var_main, values=['jikan', 'tmdb'], state='readonly', width=10)
    provider_select.pack(side='left')

    # Status label for TMDB key validation
    label_tmdb_status = tk.Label(provider_frame, text="", font=("Arial", 9))
    label_tmdb_status.pack(side='left', padx=(8,0))

    def validate_tmdb_async(label_status):
        # Run validation in background to avoid freezing UI
        def worker():
            try:
                from src.core.network import tmdb_get_genres
                genres = tmdb_get_genres()
                if genres and isinstance(genres, dict) and len(genres) > 0:
                    ui_queue.put(("label_text", label_status, "TMDB OK ✅"))
                else:
                    ui_queue.put(("label_text", label_status, "❌ TMDB inválida"))
            except Exception:
                ui_queue.put(("label_text", label_status, "❌ TMDB inválida"))
        threading.Thread(target=worker, daemon=True).start()

    def on_provider_change(event=None):
        sel = provider_var_main.get()
        config['metadata_provider'] = sel
        try:
            _safe_config(label_provider, text=f"Metadata: {sel}")
        except Exception:
            pass
        if sel == 'tmdb':
            ui_queue.put(("label_text", label_tmdb_status, "Validando TMDB..."))
            validate_tmdb_async(label_tmdb_status)
        else:
            ui_queue.put(("label_text", label_tmdb_status, ""))

    provider_select.bind('<<ComboboxSelected>>', on_provider_change)
    # If initial provider is tmdb, trigger validation at startup
    if provider_var_main.get() == 'tmdb':
        ui_queue.put(("label_text", label_tmdb_status, "Validando TMDB..."))
        validate_tmdb_async(label_tmdb_status)

    # Content type selector: determines whether generation targets anime (Jikan), pelicula (TMDB movie) or serie (TMDB tv)
    tipo_frame = tk.Frame(frame)
    tipo_frame.pack(pady=4)
    tk.Label(tipo_frame, text="Tipo a generar:").pack(side='left', padx=(0,6))
    tipo_var = tk.StringVar(value='anime')
    tipo_select = ttk.Combobox(tipo_frame, textvariable=tipo_var, values=['anime', 'pelicula', 'serie'], state='readonly', width=12)
    tipo_select.pack(side='left')

    # Botones
    def iniciar_manual():
        titulo_busqueda = simpledialog.askstring("Buscar Anime", "Ingresa el título del anime:")
        if not titulo_busqueda: return
        carpeta_anime = filedialog.askdirectory(title="Selecciona la carpeta donde están los videos (.mp4)")
        if not carpeta_anime: return
        # Determine content type selection (anime -> Jikan, pelicula/serie -> TMDB)
        tipo = tipo_var.get()
        # fetch metadata according to selection
        try:
            if tipo == 'anime':
                # Respect user's selection: when user selects 'anime' run Jikan-only lookup
                try:
                    anime_obj = buscar_anime_por_titulo(titulo_busqueda, provider_override='jikan')
                except TypeError:
                    anime_obj = buscar_anime_por_titulo(titulo_busqueda)
            elif tipo == 'pelicula':
                anime_obj = tmdb_search(titulo_busqueda, media_preference='movie', allow_when_config_is_jikan=True)
            else:  # serie
                anime_obj = tmdb_search(titulo_busqueda, media_preference='tv', allow_when_config_is_jikan=True)
        except Exception:
            anime_obj = None

        threading.Thread(
            target=generar_en_hilo_con_tipo,
            args=(
                barra_progreso,
                label_estado,
                titulo_busqueda,
                carpeta_anime,
                anime_obj,
                "Japonés",
                label_meta,
                False,
                label_titulo,
                label_sinopsis,
                etiqueta_imagen,
                ui_queue,
                root,
            ),
            daemon=True,
        ).start()

    def iniciar_automatico():
        carpeta_principal = filedialog.askdirectory(title="Selecciona la carpeta principal con subcarpetas de anime")
        if not carpeta_principal: return
        # Determine which media_root_dir to use: manual setting has priority.
        try:
            manual_media = str(config.get('media_root_dir') or '').strip()
        except Exception:
            manual_media = ''

        if manual_media:
            used = manual_media
            ui_queue.put(("label_text", label_estado, f"[MANUAL] media_root_dir usado: {used}"))
            logging.info("[MANUAL] media_root_dir usado: %s", used)
        else:
            # No manual setting — use the folder the user just selected as the media root
            try:
                config['media_root_dir'] = os.path.abspath(carpeta_principal)
                ui_queue.put(("label_text", label_estado, f"[AUTO] media_root_dir establecido: {config['media_root_dir']}"))
                logging.info("[AUTO] media_root_dir establecido: %s", config['media_root_dir'])
                try:
                    # Reconstrucción automática de índices deshabilitada.
                    # Antes se llamaba a `build_and_write_media_indexes(..., write=True)` aquí,
                    # lo que reescribía `anime.json` y `movies.json`. Para evitar sobrescribir
                    # los índices del usuario, esa llamada se ha removido.
                    logging.info('Reconstrucción automática de índices deshabilitada (no se escribirá anime.json/movies.json)')
                    ui_queue.put(("label_text", label_estado, "Índices de media: reconstrucción deshabilitada."))
                except Exception:
                    # No-op: mantener comportamiento robusto si logging falla
                    pass
            except Exception as e:
                logging.debug('Setting media_root_dir failed: %s', e)
        subcarpetas = [os.path.join(carpeta_principal, d) for d in natsorted(os.listdir(carpeta_principal)) if os.path.isdir(os.path.join(carpeta_principal, d))]
        if not subcarpetas:
            messagebox.showwarning("⚠️ Aviso", "No se encontraron subcarpetas en la carpeta seleccionada.")
            return
        # Mostrar ventana para seleccionar qué subcarpetas procesar
        ventana = tk.Toplevel(root)
        ventana.title("Seleccionar subcarpetas a procesar")
        ventana.geometry("600x700")

        tk.Label(ventana, text="Selecciona las carpetas a procesar:", font=("Arial", 10, "bold")).pack(pady=(6,0))
        search_frame = tk.Frame(ventana)
        search_frame.pack(fill='x', padx=10, pady=(0,4))
        tk.Label(search_frame, text="Buscar:").pack(side='left')
        search_var = tk.StringVar()
        tk.Entry(search_frame, textvariable=search_var, width=30).pack(side='left', padx=6, fill='x', expand=True)
        tk.Button(search_frame, text="Limpiar", command=lambda: search_var.set("")).pack(side='right')

        listbox_subcarpetas = tk.Listbox(ventana, selectmode=tk.MULTIPLE, width=80, height=20)
        listbox_subcarpetas.pack(padx=10, pady=5, fill="both", expand=True)

        displayed_subcarpetas = list(subcarpetas)
        # Keep a persistent set of selected folder paths even when filtering
        globals()['auto_selected_folders'] = globals().get('auto_selected_folders', set())
        # reentrancy guard for programmatic selection restoration
        restoring_selection = {'flag': False}

        def refresh_listbox(*_):
            nonlocal displayed_subcarpetas
            term = search_var.get().strip().lower()
            # preserve a copy of currently visible items so we can map listbox indexes
            old_displayed = list(displayed_subcarpetas)
            # remember current selection in listbox to merge with global set
            try:
                current_idxs = listbox_subcarpetas.curselection()
                for i in current_idxs:
                    try:
                        # map index to absolute folder using the old visible list
                        if 0 <= i < len(old_displayed):
                            globals()['auto_selected_folders'].add(old_displayed[i])
                    except Exception:
                        pass
            except Exception:
                pass
            # rebuild displayed list and UI entries
            listbox_subcarpetas.delete(0, tk.END)
            displayed_subcarpetas = []
            for sc in subcarpetas:
                name = os.path.basename(sc)
                if term and term not in name.lower():
                    continue
                displayed_subcarpetas.append(sc)
                listbox_subcarpetas.insert(tk.END, name)
            # restore selection for visible items if they were previously selected
            try:
                restoring_selection['flag'] = True
                for idx, sc in enumerate(displayed_subcarpetas):
                    if sc in globals().get('auto_selected_folders', set()):
                        try:
                            listbox_subcarpetas.selection_set(idx)
                        except Exception:
                            pass
            except Exception:
                pass
            finally:
                try:
                    restoring_selection['flag'] = False
                except Exception:
                    pass

        # Update selection set when user changes selection
        def on_listbox_select(event=None):
            try:
                # if we're restoring selection programmatically, ignore this event
                if restoring_selection.get('flag'):
                    return
                sel = listbox_subcarpetas.curselection()
                # add visible selected items to global set
                for i in sel:
                    try:
                        folder = displayed_subcarpetas[int(i)]
                        globals()['auto_selected_folders'].add(folder)
                    except Exception:
                        pass
                # remove from global set any visible items that were unselected by user
                # (we only remove items that are visible and not selected)
                visible_set = set(displayed_subcarpetas)
                visible_selected = set()
                for i in sel:
                    try:
                        visible_selected.add(displayed_subcarpetas[int(i)])
                    except Exception:
                        pass
                for v in visible_set - visible_selected:
                    try:
                        if v in globals().get('auto_selected_folders', set()):
                            globals()['auto_selected_folders'].discard(v)
                    except Exception:
                        pass
            except Exception:
                pass

        listbox_subcarpetas.bind('<<ListboxSelect>>', on_listbox_select)
        search_var.trace_add('write', refresh_listbox)
        refresh_listbox()

        idioma_seleccionado = tk.StringVar(value="Japonés")
        tk.Label(ventana, text="Selecciona idioma de los animes:", font=("Arial", 10)).pack(pady=(6,0))
        idioma_menu = ttk.Combobox(ventana, textvariable=idioma_seleccionado, values=["Japonés", "Español Latino", "Chino"], state="readonly")
        idioma_menu.pack(pady=4)
        # Ensure changing language does not clear selections; only store choice
        def on_idioma_change(evt=None):
            try:
                globals()['auto_selected_language'] = idioma_seleccionado.get()
            except Exception:
                pass
        idioma_menu.bind('<<ComboboxSelected>>', on_idioma_change)

        # Options: renamer and extractor
        opts_frame = tk.Frame(ventana)
        opts_frame.pack(fill='x', padx=8, pady=(6,4))
        rename_var = tk.BooleanVar(value=False)
        rename_apply_var = tk.BooleanVar(value=False)
        rename_dry_var = tk.BooleanVar(value=True)
        extractor_var = tk.BooleanVar(value=False)
        tk.Checkbutton(opts_frame, text='Renombrar (activar)', variable=rename_var).pack(side='left', padx=4)
        tk.Checkbutton(opts_frame, text='Aplicar cambios', variable=rename_apply_var).pack(side='left', padx=4)
        tk.Checkbutton(opts_frame, text='Dry-run (no aplicar)', variable=rename_dry_var).pack(side='left', padx=4)
        tk.Checkbutton(opts_frame, text='Ejecutar extractor al final', variable=extractor_var).pack(side='left', padx=8)

        # Preview area: detached window so it doesn't block the selection dialog
        preview_win = None
        preview_txt = None

        def open_preview_window():
            nonlocal preview_win, preview_txt
            try:
                # if already exists, just show and lift
                if preview_win and preview_win.winfo_exists():
                    preview_win.deiconify()
                    preview_win.lift()
                    return
            except Exception:
                preview_win = None
                preview_txt = None
            preview_win = tk.Toplevel(root)
            preview_win.title('Preview de acciones')
            preview_win.geometry('600x400')
            preview_txt = scrolledtext.ScrolledText(preview_win, height=20, wrap='word', state='normal')
            preview_txt.pack(fill='both', expand=True, padx=8, pady=8)

            def _on_close():
                try:
                    if preview_win is not None:
                        preview_win.withdraw()
                except Exception:
                    pass
            preview_win.protocol('WM_DELETE_WINDOW', _on_close)

        def mostrar_preview():
            nonlocal preview_win, preview_txt
            # ensure preview window exists
            try:
                if preview_txt is None or not (preview_win and preview_win.winfo_exists()):
                    open_preview_window()
            except Exception:
                open_preview_window()

            # Ensure preview_txt was created successfully by open_preview_window
            if preview_txt is None:
                return
            try:
                preview_txt.configure(state='normal')
                preview_txt.delete('1.0', 'end')
            except Exception:
                return

            indices = listbox_subcarpetas.curselection()
            if not indices:
                preview_txt.insert('end', 'Selecciona al menos una carpeta para previsualizar.')
                preview_txt.configure(state='disabled')
                return
            carpetas_sel = [displayed_subcarpetas[i] for i in indices]
            for c in carpetas_sel:
                preview_txt.insert('end', f"Carpeta: {os.path.basename(c)}\n")
                if rename_var.get():
                    proc = getattr(renombrar, 'procesar', None)
                    if proc:
                        try:
                            summary = proc(c, auto_confirm=True, dry_run=True)
                            moves = len(summary.get('moves', []))
                            deletes = len(summary.get('deletes', []))
                            preview_txt.insert('end', f"  - Renombrar (dry-run): {moves} movimientos, {deletes} eliminaciones\n")
                        except Exception as e:
                            preview_txt.insert('end', f"  - Error preview renombrar: {e}\n")
                    else:
                        preview_txt.insert('end', "  - Renombrar: módulo no disponible\n")
                else:
                    preview_txt.insert('end', "  - Renombrar: no activado\n")
                preview_txt.insert('end', '\n')
            if extractor_var.get():
                preview_txt.insert('end', "Extractor: se ejecutará al final del proceso automático.\n")
            else:
                preview_txt.insert('end', "Extractor: no se ejecutará.\n")
            preview_txt.configure(state='disabled')

        tk.Button(ventana, text='Abrir Preview', command=lambda: threading.Thread(target=mostrar_preview, daemon=True).start()).pack(pady=(0,6))

        def procesar_seleccionadas():
            indices = listbox_subcarpetas.curselection()
            if not indices:
                messagebox.showwarning("Atención", "Selecciona al menos una carpeta.")
                return
            carpetas_seleccionadas = [displayed_subcarpetas[i] for i in indices]
            ventana.destroy()
            # Open Auto Run Monitor window
            try:
                win = tk.Toplevel(root)
                win.title('Auto Run Monitor')
                win.geometry('700x400')
                frm = tk.Frame(win)
                frm.pack(fill='both', expand=True, padx=8, pady=8)
                canvas = tk.Canvas(frm)
                vsb = tk.Scrollbar(frm, orient='vertical', command=canvas.yview)
                inner = tk.Frame(canvas)
                inner.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
                canvas.create_window((0,0), window=inner, anchor='nw')
                canvas.configure(yscrollcommand=vsb.set)
                canvas.pack(side='left', fill='both', expand=True)
                vsb.pack(side='right', fill='y')
                # create per-folder rows
                globals()['auto_monitor_items'] = globals().get('auto_monitor_items', {})
                for c in carpetas_seleccionadas:
                    row = tk.Frame(inner)
                    row.pack(fill='x', pady=2)
                    lbl = tk.Label(row, text=os.path.basename(c), anchor='w')
                    lbl.pack(side='left', fill='x', expand=True)
                    p = ttk.Progressbar(row, orient='horizontal', length=150, mode='determinate')
                    p.pack(side='right')
                    globals()['auto_monitor_items'][c] = (lbl, p)
                # close button
                def close_monitor():
                    try:
                        globals()['auto_monitor_items'] = {}
                        win.destroy()
                    except Exception:
                        pass
                # Add folder during run button
                def add_folder_during_run():
                    try:
                        new_folder = filedialog.askdirectory(title='Selecciona carpeta a agregar al run')
                        if not new_folder:
                            return
                        # create UI row for the new folder
                        try:
                            row = tk.Frame(inner)
                            row.pack(fill='x', pady=2)
                            lbl = tk.Label(row, text=os.path.basename(new_folder), anchor='w')
                            lbl.pack(side='left', fill='x', expand=True)
                            p = ttk.Progressbar(row, orient='horizontal', length=150, mode='determinate')
                            p.pack(side='right')
                            globals()['auto_monitor_items'][new_folder] = (lbl, p)
                        except Exception:
                            pass

                        def _run_added_folder(folder_path=new_folder):
                            try:
                                if ui_queue:
                                    ui_queue.put(("auto_folder_update", folder_path, 0, "queued"))
                                # simple lookup attempt; best-effort
                                try:
                                    anime_obj = buscar_anime_por_titulo(os.path.basename(folder_path), folder_path=folder_path)
                                except TypeError:
                                    try:
                                        anime_obj = buscar_anime_por_titulo(os.path.basename(folder_path))
                                    except Exception:
                                        anime_obj = None
                                except Exception:
                                    anime_obj = None
                                if ui_queue:
                                    ui_queue.put(("auto_folder_update", folder_path, 0, "started"))
                                # run generation for this single folder in background
                                try:
                                    page_builder.generar_en_hilo_con_tipo(
                                        barra_progreso,
                                        label_estado,
                                        os.path.basename(folder_path),
                                        folder_path,
                                        anime_obj,
                                        globals().get('auto_selected_language', 'Japonés'),
                                        label_meta,
                                        True,
                                        label_titulo,
                                        label_sinopsis,
                                        etiqueta_imagen,
                                        ui_queue,
                                        root,
                                        False,
                                    )
                                finally:
                                    if ui_queue:
                                        ui_queue.put(("auto_folder_update", folder_path, 100, "done"))
                            except Exception as e:
                                if ui_queue:
                                    ui_queue.put(("debug_error", f"Error running added folder {folder_path}: {e}"))

                        threading.Thread(target=_run_added_folder, daemon=True).start()
                    except Exception:
                        pass

                btns_sub = tk.Frame(win)
                btns_sub.pack(fill='x', padx=8, pady=(0,6))
                tk.Button(btns_sub, text='Agregar carpeta', command=add_folder_during_run).pack(side='left', padx=6)
                tk.Button(btns_sub, text='Cerrar monitor', command=close_monitor).pack(side='right', padx=6)
                globals()['auto_monitor_win'] = win
            except Exception:
                pass
            # enable persistent control buttons for this run
            try:
                btn_pause.config(state='normal')
                btn_resume.config(state='disabled')
                btn_stop.config(state='normal')
            except Exception:
                pass

            # When generation finishes, remove the controls and reset gen_control
            def _cleanup_controls():
                try:
                    # disable persistent buttons until next run
                    btn_pause.config(state='disabled')
                    btn_resume.config(state='disabled')
                    btn_stop.config(state='disabled')
                except Exception:
                    pass
                # reset control flags for next run
                try:
                    gen_control.pause_event.clear()
                    gen_control.stop_event.clear()
                except Exception:
                    pass
                # reset UI state when generation fully finished
                try:
                    reset_ui_state()
                except Exception:
                    pass
                # restore main generation buttons
                try:
                    bm = globals().get('boton_manual')
                    ba = globals().get('boton_auto')
                    if bm:
                        try:
                            bm.pack(pady=5)
                        except Exception:
                            bm.config(state='normal')
                    if ba:
                        try:
                            ba.pack(pady=5)
                        except Exception:
                            ba.config(state='normal')
                except Exception:
                    pass
                process_ui_queue.on_generation_finished = None

            process_ui_queue.on_generation_finished = _cleanup_controls

            # build run options dict
            run_options = {
                'rename': rename_var.get(),
                'rename_apply': rename_apply_var.get(),
                'rename_dry': rename_dry_var.get(),
                'extractor': extractor_var.get()
            }
            threading.Thread(
                target=generar_automatico_en_hilo,
                args=(
                    barra_progreso,
                    label_estado,
                    carpetas_seleccionadas,
                    idioma_seleccionado,
                    label_meta,
                    label_titulo,
                    label_sinopsis,
                    etiqueta_imagen,
                    ui_queue,
                    root,
                    run_options,
                ),
                daemon=True,
            ).start()

        def seleccionar_todo():
            listbox_subcarpetas.select_set(0, tk.END)

        btns_frame = tk.Frame(ventana)
        btns_frame.pack(pady=8)
        tk.Button(btns_frame, text="Seleccionar Todas", command=seleccionar_todo).pack(side="left", padx=6)
        tk.Button(btns_frame, text="Procesar Seleccionadas", command=procesar_seleccionadas).pack(side="right", padx=6)

    boton_manual = tk.Button(frame, text="Generar Página HTML (Manual)", font=("Arial", 12), command=iniciar_manual)
    boton_manual.pack(pady=5)
    boton_auto = tk.Button(frame, text="Generar Páginas HTML (Automático)", font=("Arial", 12), command=iniciar_automatico)
    boton_auto.pack(pady=5)

    root.mainloop()
