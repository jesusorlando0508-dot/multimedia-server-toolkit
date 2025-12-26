#!/usr/bin/env python3
"""Refactored Tkinter GUI to edit and persist the project's configuration.

Goals:
- Break form into logical section frames so grid doesn't reflow unpredictably.
- Use columnconfigure to allow column 1 to expand and keep buttons stable.
- Use safer mousewheel bindings (Windows, macOS, Linux X11) and avoid bind_all.
- Keep the same external module calls (config, translator, translator_setup, page_builder).
- Preserve features: verify translators, model setup, save secrets/env, background scans.
"""
from __future__ import annotations
import os
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from typing import Dict, Any
from pathlib import Path
import threading
import time
import importlib
import importlib.util

# external project modules (kept as in original so behavior doesn't change)
import src.translator.translator
import src.translator.translator_setup
from src.core.cache import CACHE_FILE, _ensure_cache
from src.core.config import load_config, save_config, save_secrets, save_env_key, CONFIG_PATH, SECRETS_PATH


def _ensure_json_file(path: str) -> None:
    try:
        if not path:
            return
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text("[]", encoding="utf-8")
    except Exception:
        # keep silent: best-effort helper
        pass


def _browse_dir(entry: ttk.Entry) -> None:
    d = filedialog.askdirectory()
    if d:
        entry.delete(0, tk.END)
        entry.insert(0, os.path.abspath(d))


def _browse_file(entry: ttk.Entry, filetypes=(("All files", "*.*"),)) -> None:
    p = filedialog.askopenfilename(filetypes=filetypes)
    if p:
        entry.delete(0, tk.END)
        entry.insert(0, os.path.abspath(p))


def _browse_json_file(entry: ttk.Entry, default_name: str = "data.json") -> None:
    current = entry.get().strip() if entry else ""
    initialdir = os.path.dirname(current) if current else os.getcwd()
    if not os.path.isdir(initialdir):
        initialdir = os.getcwd()
    initialfile = os.path.basename(current) if current else default_name
    p = filedialog.asksaveasfilename(
        title="Selecciona o crea un archivo JSON",
        defaultextension=".json",
        initialdir=initialdir,
        initialfile=initialfile,
        filetypes=(("JSON", "*.json"), ("All files", "*.*")),
    )
    if p:
        entry.delete(0, tk.END)
        entry.insert(0, os.path.abspath(p))


def _safe_parent_root() -> tk.Tk | None:
    """Return an existing Tk root if usable, else None."""
    root = getattr(tk, "_default_root", None)
    if isinstance(root, tk.Tk):
        return root
    return None


def ensure_config_via_gui(existing_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Show a modal dialog to collect configuration.

    The refactor preserves the original external behavior while making the layout
    robust: sections are grouped into Frames and the middle column is stretchable.
    """
    parent = _safe_parent_root()

    created_root = False
    if parent is None:
        win = tk.Tk()
        created_root = True
    else:
        win = tk.Toplevel(parent)
        win.transient(parent)
        try:
            win.grab_set()
        except Exception:
            pass

    win.title("Ajustes")
    style = ttk.Style(win)
    try:
        style.theme_use("clam")
    except Exception:
        pass

    style.configure("Section.TLabelframe", padding=12)
    style.configure("Section.TLabelframe.Label", font=("Segoe UI", 10, "bold"))

    try:
        win.minsize(720, 620)
        win.resizable(True, True)
    except Exception:
        pass

    desired_w, desired_h = 900, 780
    try:
        win.update_idletasks()
        screen_w = max(1, win.winfo_screenwidth())
        screen_h = max(1, win.winfo_screenheight())
        pos_x = max(0, (screen_w - desired_w) // 2)
        pos_y = max(0, (screen_h - desired_h) // 3)
        win.geometry(f"{desired_w}x{desired_h}+{pos_x}+{pos_y}")
    except Exception:
        win.geometry("900x780")

    # ------------------ Scrollable container ------------------
    container = ttk.Frame(win)
    container.pack(fill=tk.BOTH, expand=True)

    canvas = tk.Canvas(container)
    vsb = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=vsb.set)
    vsb.pack(side=tk.RIGHT, fill=tk.Y)
    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    frm = ttk.Frame(canvas, padding=12)
    canvas_window = canvas.create_window((0, 0), window=frm, anchor="nw")

    def _on_frame_configure(event=None):
        try:
            frm.update_idletasks()
            canvas.configure(scrollregion=canvas.bbox("all"))
        except Exception:
            pass

    frm.bind("<Configure>", _on_frame_configure)

    # Safe mousewheel handlers (Windows/macOS/Linux X11)
    def _on_mousewheel(event):
        try:
            if event.num == 4:
                canvas.yview_scroll(-1, "units")
            elif event.num == 5:
                canvas.yview_scroll(1, "units")
            else:
                # Windows and macOS use delta
                delta = getattr(event, "delta", 0)
                canvas.yview_scroll(int(-1 * (delta / 120)), "units")
        except Exception:
            pass

    # bind to the canvas and window (avoid global bind_all)
    canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _on_mousewheel))
    canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
    # X11 mouse wheel events
    canvas.bind_all("<Button-4>", _on_mousewheel)
    canvas.bind_all("<Button-5>", _on_mousewheel)

    # ------------------ Layout policy: 3 columns, middle expands ------------------
    frm.grid_columnconfigure(0, weight=0)
    frm.grid_columnconfigure(1, weight=1)
    frm.grid_columnconfigure(2, weight=0)

    # ------------------ Fields ------------------
    fields = [
        ("pages_output_dir", "Carpeta de páginas (filesystem)"),
        ("media_root_dir", "Carpeta raíz de media (anime + películas)"),
        ("anime_json_path", "Ruta del JSON agregado de anime (filesystem)"),
        ("movies_json_path", "Ruta del JSON agregado de películas (filesystem)"),
        ("json_link_prefix", "Prefijo web para enlaces JSON (ej: /pages/)")
    ]

    entries: Dict[str, ttk.Entry] = {}

    def _get_entry_value(key: str, default: str = "") -> str:
        ent = entries.get(key)
        if ent is None:
            return default
        try:
            return ent.get().strip()
        except Exception:
            return default

    row_index = 0
    for key, label in fields:
        ttk.Label(frm, text=label).grid(row=row_index, column=0, sticky=tk.W, pady=6)
        ent = ttk.Entry(frm, width=60)
        ent.grid(row=row_index, column=1, sticky="we", padx=6)
        val = existing_cfg.get(key) or existing_cfg.get(key.lower()) or ""
        ent.insert(0, val)
        entries[key] = ent

        browse_btn = None
        if key in ("pages_output_dir", "media_root_dir"):
            browse_btn = ttk.Button(frm, text="Explorar...", command=lambda e=ent: _browse_dir(e))
        elif key in ("anime_json_path", "movies_json_path"):
            default_name = "anime.json" if key == "anime_json_path" else "movies.json"
            browse_btn = ttk.Button(frm, text="Archivo...", command=lambda e=ent, d=default_name: _browse_json_file(e, d))
        elif "dir" in key or "path" in key:
            browse_btn = ttk.Button(frm, text="Explorar...", command=lambda e=ent: _browse_dir(e))

        if browse_btn:
            browse_btn.grid(row=row_index, column=2, padx=6, sticky="w")
        row_index += 1

    # ------------------ Translator / advanced section ------------------
    # Use a frame for the advanced block so its internal grid doesn't affect other sections
    adv_frame = ttk.LabelFrame(frm, text="Opciones avanzadas", style="Section.TLabelframe")
    adv_frame.grid(row=row_index, column=0, columnspan=3, sticky="ew", pady=(12, 6))
    adv_frame.grid_columnconfigure(0, weight=0)
    adv_frame.grid_columnconfigure(1, weight=1)
    adv_frame.grid_columnconfigure(2, weight=0)

    adv_r = 0
    preload_var = tk.BooleanVar(value=bool(existing_cfg.get("preload_translator_on_start", True)))

    ttk.Label(adv_frame, text="Translator backend:").grid(row=adv_r, column=0, sticky=tk.W, pady=6)
    backend_var = tk.StringVar(value=existing_cfg.get("translator_backend", "local"))
    ent_backend = ttk.Combobox(adv_frame, textvariable=backend_var, values=["auto", "local", "deepl", "m2m100", "aventiq", "argos"], state="readonly", width=20)
    ent_backend.grid(row=adv_r, column=1, sticky="w", padx=6)
    entries["translator_backend"] = ent_backend
    lbl_mar_status = ttk.Label(adv_frame, text="Marian: ?")
    lbl_mar_status.grid(row=adv_r, column=2, sticky="w", padx=6)

    adv_r += 1
    ttk.Label(adv_frame, text="DeepL API Key (opcional):").grid(row=adv_r, column=0, sticky=tk.W, pady=6)
    ent_deepl = ttk.Entry(adv_frame, width=60, show="*")
    ent_deepl.grid(row=adv_r, column=1, sticky="we", padx=6)
    entries["deepl_api_key"] = ent_deepl
    lbl_m2m_status = ttk.Label(adv_frame, text="M2M100: ?")
    lbl_m2m_status.grid(row=adv_r, column=2, sticky="w", padx=6)

    adv_r += 1
    lbl_aventiq_status = ttk.Label(adv_frame, text="AventIQ: ?")
    lbl_aventiq_status.grid(row=adv_r, column=2, sticky="w", padx=6)
    adv_r += 1
    lbl_argos_status = ttk.Label(adv_frame, text="Argos: ?")
    lbl_argos_status.grid(row=adv_r, column=2, sticky="w", padx=6)
    try:
        lbl_argos_experimental = ttk.Label(adv_frame, text="Experimental: puede traducir parcialmente algunos textos", foreground="orange")
        lbl_argos_experimental.grid(row=adv_r, column=1, sticky="w", padx=6)
    except Exception:
        pass

    adv_r += 1
    ttk.Label(adv_frame, text="Proveedor de metadata (jikan/tmdb):").grid(row=adv_r, column=0, sticky=tk.W, pady=6)
    ent_provider = ttk.Entry(adv_frame, width=60)
    ent_provider.grid(row=adv_r, column=1, sticky="we", padx=6)
    ent_provider.insert(0, existing_cfg.get("metadata_provider", "jikan"))
    entries["metadata_provider"] = ent_provider

    adv_r += 1
    ttk.Label(adv_frame, text="TMDB API Key:").grid(row=adv_r, column=0, sticky=tk.W, pady=6)
    ent_tmdb = ttk.Entry(adv_frame, width=60, show="*")
    ent_tmdb.grid(row=adv_r, column=1, sticky="we", padx=6)
    entries["tmdb_api_key"] = ent_tmdb

    adv_r += 1
    # Translator tuning
    ttk.Label(adv_frame, text="Translator device (cpu/cuda):").grid(row=adv_r, column=0, sticky=tk.W, pady=6)
    ent_trans_dev = ttk.Entry(adv_frame, width=20)
    ent_trans_dev.grid(row=adv_r, column=1, sticky="w", padx=6)
    ent_trans_dev.insert(0, existing_cfg.get("translator_device", "cpu"))
    entries["translator_device"] = ent_trans_dev

    adv_r += 1
    ttk.Label(adv_frame, text="Translator batch size:").grid(row=adv_r, column=0, sticky=tk.W, pady=6)
    ent_trans_bs = ttk.Entry(adv_frame, width=20)
    ent_trans_bs.grid(row=adv_r, column=1, sticky="w", padx=6)
    ent_trans_bs.insert(0, str(existing_cfg.get("translator_batch_size", 16)))
    entries["translator_batch_size"] = ent_trans_bs

    adv_r += 1
    ttk.Label(adv_frame, text="Translator cache size:").grid(row=adv_r, column=0, sticky=tk.W, pady=6)
    ent_trans_cache = ttk.Entry(adv_frame, width=20)
    ent_trans_cache.grid(row=adv_r, column=1, sticky="w", padx=6)
    ent_trans_cache.insert(0, str(existing_cfg.get("translator_cache_size", 1024)))
    entries["translator_cache_size"] = ent_trans_cache

    adv_r += 1
    ttk.Label(adv_frame, text="Pre-cargar traductor al iniciar:").grid(row=adv_r, column=0, sticky=tk.W, pady=6)
    chk_preload = ttk.Checkbutton(adv_frame, variable=preload_var)
    chk_preload.grid(row=adv_r, column=1, sticky="w", padx=6)

    # Argos Translate (experimental) options
    adv_r += 1
    ttk.Separator(adv_frame, orient='horizontal').grid(row=adv_r, column=0, columnspan=3, sticky='ew', pady=(8,8))
    adv_r += 1
    ttk.Label(adv_frame, text="Argos Translate - Carpeta modelos:").grid(row=adv_r, column=0, sticky=tk.W, pady=6)
    ent_argos_models = ttk.Entry(adv_frame, width=60)
    ent_argos_models.grid(row=adv_r, column=1, sticky="we", padx=6)
    ent_argos_models.insert(0, existing_cfg.get("argos_models_dir", "argos_models"))
    entries["argos_models_dir"] = ent_argos_models
    ttk.Button(adv_frame, text="Explorar...", command=lambda e=ent_argos_models: _browse_dir(e)).grid(row=adv_r, column=2, padx=6, sticky="w")

    adv_r += 1
    argos_auto_var = tk.BooleanVar(value=bool(existing_cfg.get("argos_auto_install_models", False)))
    ttk.Label(adv_frame, text="Instalar modelos Argos automáticamente:").grid(row=adv_r, column=0, sticky=tk.W, pady=6)
    chk_argos_auto = ttk.Checkbutton(adv_frame, variable=argos_auto_var)
    chk_argos_auto.grid(row=adv_r, column=1, sticky="w", padx=6)
    # Install now button + progress
    btn_install_argos = ttk.Button(adv_frame, text="Instalar/Actualizar modelo Argos ahora")
    btn_install_argos.grid(row=adv_r, column=2, padx=6, sticky="w")
    adv_r += 1
    argos_progress = ttk.Progressbar(adv_frame, mode="indeterminate", length=180)
    argos_progress.grid(row=adv_r, column=1, sticky="w", padx=6)

    # system summary
    adv_r += 1
    sys_summary = existing_cfg.get("system_resources") or {}
    summary_text = (
        f"CPU: {sys_summary.get('cpu_count', '?')} | RAM: {sys_summary.get('total_ram_gb', '?')} GB | "
        f"GPU: {'Sí' if sys_summary.get('has_gpu') else 'No'} | Perfil: {sys_summary.get('translation_profile', 'unknown')}"
    )
    ttk.Label(adv_frame, text="Recursos detectados:").grid(row=adv_r, column=0, sticky=tk.W, pady=6)
    ttk.Label(adv_frame, text=summary_text).grid(row=adv_r, column=1, columnspan=2, sticky=tk.W)

    # status & progress
    adv_r += 1
    status_var = tk.StringVar(value="Listo")
    status_label = ttk.Label(adv_frame, textvariable=status_var)
    status_label.grid(row=adv_r, column=0, sticky=tk.W, pady=4)
    progress = ttk.Progressbar(adv_frame, mode="indeterminate", length=160)
    progress.grid(row=adv_r, column=1, sticky="w", pady=4)

    def _set_status(msg: str, busy: bool = False) -> None:
        def _update():
            status_var.set(msg)
            if busy:
                try:
                    progress.start(12)
                except Exception:
                    pass
            else:
                try:
                    progress.stop()
                except Exception:
                    pass

        status_label.after(0, _update)

    resource_probe_flag = {"requested": False}

    def _request_resource_probe() -> None:
        resource_probe_flag["requested"] = True
        _set_status("Se volverán a analizar los recursos al guardar.", False)

    ttk.Button(adv_frame, text="Recalcular recursos", command=_request_resource_probe).grid(row=adv_r, column=2, pady=4, padx=6, sticky="w")

    # translator checks
    def _update_backend_options(available_backends):
        try:
            ent_backend["values"] = available_backends
        except Exception:
            pass

    def verify_translators() -> None:
        def _worker():
            try:
                _set_status("Verificando traductores...", True)
                try:
                    mar_forbidden = getattr(src.translator.translator, "_MODEL_LOADING_FORBIDDEN", False)
                    mar_unavail = getattr(src.translator.translator, "_MODEL_UNAVAILABLE", False)
                except Exception:
                    mar_forbidden = True
                    mar_unavail = True
                mar_status = "Unknown"
                if mar_forbidden:
                    mar_status = "Unavailable (deps missing)"
                elif mar_unavail:
                    mar_status = "Unavailable (load failed)"
                else:
                    mar_status = "Available (on-demand)"
                try:
                    lbl_mar_status.config(text=f"Marian: {mar_status}")
                except Exception:
                    pass

                # M2M and AventIQ checks
                m2m_path = _get_entry_value("m2m_model_path", existing_cfg.get("m2m_model_path", "") or "")
                m2m_ok = False
                m2m_note = ""
                if m2m_path:
                    p = os.path.expanduser(str(m2m_path))
                    if os.path.exists(p):
                        m2m_ok = True
                        m2m_note = f"Local ({p})"
                if not m2m_ok:
                    try:
                        importlib.import_module("transformers")
                        m2m_ok = True
                        m2m_note = "Available (will download)"
                    except Exception:
                        m2m_ok = False
                        m2m_note = "Unavailable (transformers missing)"
                try:
                    lbl_m2m_status.config(text=f"M2M100: {m2m_note}")
                except Exception:
                    pass

                aventiq_path = _get_entry_value("aventiq_model_path", existing_cfg.get("aventiq_model_path", "") or "")
                aventiq_ok = False
                aventiq_note = ""
                if aventiq_path:
                    ap = os.path.expanduser(str(aventiq_path))
                    if os.path.exists(ap):
                        aventiq_ok = True
                        aventiq_note = f"Local ({ap})"
                if not aventiq_ok:
                    try:
                        importlib.import_module("transformers")
                        aventiq_ok = True
                        aventiq_note = "Disponible (requiere HuggingFace)"
                    except Exception:
                        aventiq_ok = False
                        aventiq_note = "No disponible (transformers faltante)"
                try:
                    lbl_aventiq_status.config(text=f"AventIQ: {aventiq_note}")
                except Exception:
                    pass

                # Argos check
                try:
                    try:
                        spec = importlib.util.find_spec('argostranslate')
                        if spec is None:
                            argos_note = 'No instalado (pip install argostranslate)'
                        else:
                            import argostranslate.translate as at
                            langs = []
                            try:
                                langs = at.get_installed_languages() or []
                            except Exception:
                                langs = []
                            target = existing_cfg.get('translator_target_lang', 'es') or 'es'
                            has_pair = any(getattr(l, 'code', None) == 'en' for l in langs) and any(getattr(l, 'code', None) == target for l in langs)
                            if has_pair:
                                argos_note = 'Disponible (en->' + str(target) + ')'
                            else:
                                if bool(existing_cfg.get('argos_auto_install_models', False)):
                                    argos_note = 'Disponible (instalará modelo en background)'
                                else:
                                    argos_note = 'Instalado pero sin modelo en->' + str(target)
                    except Exception:
                        argos_note = 'Error comprobando Argos'
                except Exception:
                    argos_note = 'Error'
                try:
                    lbl_argos_status.config(text=f"Argos: {argos_note}")
                except Exception:
                    pass

                avail = ["deepl"]
                if not mar_forbidden:
                    avail.insert(0, "local")
                if m2m_ok:
                    avail.append("m2m100")
                if aventiq_ok:
                    avail.append("aventiq")
                try:
                    # add Argos to available backends when package present
                    argos_spec = importlib.util.find_spec('argostranslate')
                    if argos_spec is not None:
                        avail.append('argos')
                except Exception:
                    pass
                try:
                    ent_backend.after(0, _update_backend_options, avail)
                except Exception:
                    _update_backend_options(avail)
            except Exception:
                try:
                    lbl_mar_status.config(text=f"Marian: error")
                except Exception:
                    pass
                try:
                    lbl_m2m_status.config(text=f"M2M100: error")
                except Exception:
                    pass
            finally:
                _set_status("Verificación completada.", False)

        threading.Thread(target=_worker, daemon=True).start()

    def _do_translation_test() -> None:
        def _worker():
            try:
                _set_status("Ejecutando prueba de traducción...", True)
                sample = "Hello world. This is a short test for translation."
                start = time.time()
                tr = src.translator.translator.get_translator()
                res = tr.translate(sample)
                elapsed = time.time() - start
                message = f"Resultado: {res}\nTiempo: {elapsed:.2f}s\nBackend usado: {type(tr).__name__}"
                try:
                    messagebox.showinfo("Prueba de traducción", message)
                except Exception:
                    pass
            except Exception as e:
                try:
                    messagebox.showerror("Prueba de traducción", f"Error durante la prueba: {e}")
                except Exception:
                    pass
            finally:
                _set_status("Prueba finalizada.", False)

        threading.Thread(target=_worker, daemon=True).start()

    # Argos install action
    def _install_argos_now() -> None:
        def _worker():
            try:
                _set_status("Instalando modelo Argos...", True)

                from src.translator.argos import ArgosTranslator
                at = ArgosTranslator()
                ui_q = None
                try:
                    from src.core.app_state import ui_queue as _ui_q
                    ui_q = _ui_q
                except Exception:
                    ui_q = None

                # determine target language
                target = existing_cfg.get('translator_target_lang') or 'es'
                try:
                    res = at.install_package_for_target(from_code='en', to_code=target, ui_queue=ui_q, max_attempts=3)
                except Exception as inst_err:
                    res = {"success": False, "error": str(inst_err)}

                if res.get('success'):
                    msg = f"Argos: instalado {getattr(res.get('package'), 'name', '')} en {res.get('elapsed'):.1f}s"
                    try:
                        lbl_argos_status.config(text=f"Argos: Disponible (en->{target})")
                    except Exception:
                        pass
                    try:
                        messagebox.showinfo('Argos', msg)
                    except Exception:
                        pass
                else:
                    err = res.get('error') or 'Desconocido'
                    try:
                        lbl_argos_status.config(text=f"Argos: Error: {err}")
                    except Exception:
                        pass
                    try:
                        messagebox.showerror('Argos', f'Instalación falló: {err}')
                    except Exception:
                        pass

            except Exception as e:
                try:
                    messagebox.showerror('Argos', f'Error: {e}')
                except Exception:
                    pass
            finally:
                _set_status('Listo.', False)
                try:
                    argos_progress.stop()
                except Exception:
                    pass

        # start progress and run worker in background
        try:
            try:
                argos_progress.start(10)
            except Exception:
                pass
            threading.Thread(target=_worker, daemon=True).start()
        except Exception:
            try:
                messagebox.showerror('Argos', 'No se pudo iniciar la instalación en background')
            except Exception:
                pass

    try:
        btn_install_argos.config(command=_install_argos_now)
    except Exception:
        pass

    row_index += 1

    # ------------------ Template & misc paths ------------------
    misc_frame = ttk.Frame(frm)
    misc_frame.grid(row=row_index, column=0, columnspan=3, sticky="ew", pady=(8, 6))
    misc_frame.grid_columnconfigure(0, weight=0)
    misc_frame.grid_columnconfigure(1, weight=1)
    misc_frame.grid_columnconfigure(2, weight=0)

    r = 0
    ttk.Label(misc_frame, text="ruta del template.html:").grid(row=r, column=0, sticky=tk.W, pady=6)
    ent_template = ttk.Entry(misc_frame, width=60)
    ent_template.grid(row=r, column=1, sticky="we", padx=6)
    ent_template.insert(0, existing_cfg.get("template_path", os.path.join(os.path.dirname(__file__), "template.html")))
    entries["template_path"] = ent_template
    btn_tmpl = ttk.Button(misc_frame, text="Explorar...", command=lambda e=ent_template: _browse_file(e))
    btn_tmpl.grid(row=r, column=2, padx=6, sticky="w")

    r += 1
    ttk.Label(misc_frame, text="tmdb_overrides.json:").grid(row=r, column=0, sticky=tk.W, pady=6)
    ent_overrides = ttk.Entry(misc_frame, width=60)
    ent_overrides.grid(row=r, column=1, sticky="we", padx=6)
    ent_overrides.insert(0, existing_cfg.get("tmdb_overrides_path", os.path.join(os.path.dirname(__file__), "tmdb_overrides.json")))
    entries["tmdb_overrides_path"] = ent_overrides
    btn_over = ttk.Button(misc_frame, text="Explorar...", command=lambda e=ent_overrides: _browse_file(e, filetypes=(("JSON files", "*.json"), ("All files", "*.*"))))
    btn_over.grid(row=r, column=2, padx=6, sticky="w")

    r += 1
    ttk.Label(misc_frame, text="TMDB genres JSON:").grid(row=r, column=0, sticky=tk.W, pady=6)
    ent_tmdb_gen = ttk.Entry(misc_frame, width=60)
    ent_tmdb_gen.grid(row=r, column=1, sticky="we", padx=6)
    ent_tmdb_gen.insert(0, existing_cfg.get("tmdb_gen_path", os.path.join(os.path.dirname(__file__), "tmdb_gen.json")))
    entries["tmdb_gen_path"] = ent_tmdb_gen
    btn_tg = ttk.Button(misc_frame, text="Explorar...", command=lambda e=ent_tmdb_gen: _browse_file(e, filetypes=(("JSON files", "*.json"), ("All files", "*.*"))))
    btn_tg.grid(row=r, column=2, padx=6, sticky="w")

    r += 1
    ttk.Label(misc_frame, text="ruta del extractor:").grid(row=r, column=0, sticky=tk.W, pady=6)
    ent_extractor = ttk.Entry(misc_frame, width=60)
    ent_extractor.grid(row=r, column=1, sticky="we", padx=6)
    ent_extractor.insert(0, existing_cfg.get("extractor_path", os.path.join(os.path.dirname(__file__), "extractor_html2.2.py")))
    entries["extractor_path"] = ent_extractor
    btn_ex = ttk.Button(misc_frame, text="Explorar...", command=lambda e=ent_extractor: _browse_file(e, filetypes=(("Python files", "*.py"), ("All files", "*.*"))))
    btn_ex.grid(row=r, column=2, padx=6, sticky="w")

    r += 1
    ttk.Label(misc_frame, text="Cache directory:").grid(row=r, column=0, sticky=tk.W, pady=6)
    ent_cache = ttk.Entry(misc_frame, width=60)
    ent_cache.grid(row=r, column=1, sticky="we", padx=6)
    ent_cache.insert(0, existing_cfg.get("cache_dir", os.path.join(os.path.dirname(__file__), ".cache")))
    entries["cache_dir"] = ent_cache
    btn_cache = ttk.Button(misc_frame, text="Explorar...", command=lambda e=ent_cache: _browse_dir(e))
    btn_cache.grid(row=r, column=2, padx=6, sticky="w")

    row_index += 1

    # ------------------ Model management section (grouped) ------------------
    mframe = ttk.LabelFrame(frm, text="Modelos locales y descargas", style="Section.TLabelframe")
    mframe.grid(row=row_index, column=0, columnspan=3, sticky="ew", pady=(12, 6))
    mframe.grid_columnconfigure(0, weight=0)
    mframe.grid_columnconfigure(1, weight=1)
    mframe.grid_columnconfigure(2, weight=0)

    mr = 0
    ttk.Label(mframe, text="Carpeta base para modelos descargados:").grid(row=mr, column=0, sticky=tk.W, pady=4)
    ent_models_dir = ttk.Entry(mframe, width=60)
    ent_models_dir.grid(row=mr, column=1, sticky="we", padx=6)
    ent_models_dir.insert(0, existing_cfg.get("translator_models_dir", os.path.join(os.path.dirname(__file__), "models")))
    entries["translator_models_dir"] = ent_models_dir
    ttk.Button(mframe, text="Explorar...", command=lambda e=ent_models_dir: _browse_dir(e)).grid(row=mr, column=2, padx=6, sticky="w")

    mr += 1
    auto_download_var = tk.BooleanVar(value=bool(existing_cfg.get("translator_auto_download_models", True)))
    ttk.Checkbutton(mframe, text="Descargar automáticamente si faltan modelos", variable=auto_download_var).grid(row=mr, column=0, columnspan=2, sticky=tk.W)

    mr += 1
    ttk.Label(mframe, text="Marian repo o carpeta remota:").grid(row=mr, column=0, sticky=tk.W, pady=4)
    ent_marian_repo = ttk.Entry(mframe, width=60)
    ent_marian_repo.grid(row=mr, column=1, sticky="we", padx=6)
    ent_marian_repo.insert(0, existing_cfg.get("local_marian_model_name", "Helsinki-NLP/opus-mt-en-es"))
    entries["local_marian_model_name"] = ent_marian_repo

    mr += 1
    ttk.Label(mframe, text="Marian carpeta local (opcional):").grid(row=mr, column=0, sticky=tk.W, pady=4)
    ent_marian_path = ttk.Entry(mframe, width=60)
    ent_marian_path.grid(row=mr, column=1, sticky="we", padx=6)
    ent_marian_path.insert(0, existing_cfg.get("local_marian_model_path", ""))
    entries["local_marian_model_path"] = ent_marian_path
    ttk.Button(mframe, text="Explorar...", command=lambda e=ent_marian_path: _browse_dir(e)).grid(row=mr, column=2, padx=6, sticky="w")

    mr += 1
    ttk.Label(mframe, text="M2M100 carpeta local (opcional):").grid(row=mr, column=0, sticky=tk.W, pady=4)
    ent_m2m = ttk.Entry(mframe, width=60)
    ent_m2m.grid(row=mr, column=1, sticky="we", padx=6)
    ent_m2m.insert(0, existing_cfg.get("m2m_model_path", ""))
    entries["m2m_model_path"] = ent_m2m
    ttk.Button(mframe, text="Explorar...", command=lambda e=ent_m2m: _browse_dir(e)).grid(row=mr, column=2, padx=6, sticky="w")

    mr += 1
    ttk.Label(mframe, text="M2M100 repo remoto:").grid(row=mr, column=0, sticky=tk.W, pady=4)
    ent_m2m_name = ttk.Entry(mframe, width=60)
    ent_m2m_name.grid(row=mr, column=1, sticky="we", padx=6)
    ent_m2m_name.insert(0, existing_cfg.get("m2m_model_name", "facebook/m2m100_418M"))
    entries["m2m_model_name"] = ent_m2m_name

    mr += 1
    ttk.Label(mframe, text="AventIQ repo remoto:").grid(row=mr, column=0, sticky=tk.W, pady=4)
    ent_aventiq_repo = ttk.Entry(mframe, width=60)
    ent_aventiq_repo.grid(row=mr, column=1, sticky="we", padx=6)
    ent_aventiq_repo.insert(0, existing_cfg.get("aventiq_model_name", "AventIQ-AI/English-To-Spanish"))
    entries["aventiq_model_name"] = ent_aventiq_repo

    mr += 1
    ttk.Label(mframe, text="AventIQ carpeta local (opcional):").grid(row=mr, column=0, sticky=tk.W, pady=4)
    ent_aventiq_path = ttk.Entry(mframe, width=60)
    ent_aventiq_path.grid(row=mr, column=1, sticky="we", padx=6)
    ent_aventiq_path.insert(0, existing_cfg.get("aventiq_model_path", ""))
    entries["aventiq_model_path"] = ent_aventiq_path
    ttk.Button(mframe, text="Explorar...", command=lambda e=ent_aventiq_path: _browse_dir(e)).grid(row=mr, column=2, padx=6, sticky="w")

    mr += 1
    model_status_var = tk.StringVar(value="—")

    def _apply_model_entries_from_cfg(cfg: Dict[str, Any]):
        for key in ("local_marian_model_path", "local_marian_model_name", "m2m_model_path", "m2m_model_name", "aventiq_model_path", "aventiq_model_name", "translator_models_dir"):
            val = cfg.get(key, "")
            ent = entries.get(key)
            if ent is None:
                continue
            try:
                ent.delete(0, tk.END)
                ent.insert(0, val or "")
            except Exception:
                pass
        try:
            auto_download_var.set(bool(cfg.get("translator_auto_download_models", True)))
        except Exception:
            pass

    def _update_model_status_label(cfg: Dict[str, Any] | None = None) -> None:
        try:
            data = cfg or load_config() or {}
            summaries = src.translator.translator_setup.verify_configured_models(data)
            text = "\n".join(summaries) if summaries else "Sin información disponible."
        except Exception as e:
            text = f"No se pudo verificar: {e}"
        model_status_var.set(text)

    def _run_model_setup() -> None:
        try:
            cfg = load_config() or {}
            src.translator.translator_setup.prompt_translator_model_setup(cfg, force=True, parent=win)
            refreshed = load_config() or {}
            _apply_model_entries_from_cfg(refreshed)
            _update_model_status_label(refreshed)
            messagebox.showinfo("Modelos", "Actualización de modelos completada.")
        except Exception as e:
            messagebox.showerror("Modelos", f"Error al configurar modelos: {e}")

    def _refresh_model_status_only() -> None:
        _update_model_status_label()

    ttk.Button(mframe, text="Descargar/seleccionar modelos…", command=_run_model_setup).grid(row=mr, column=0, columnspan=2, sticky=tk.W, pady=(4, 2))
    ttk.Button(mframe, text="Actualizar estado modelos", command=_refresh_model_status_only).grid(row=mr, column=2, sticky=tk.W, pady=(4, 2))

    mr += 1
    ttk.Label(mframe, text="Estado de modelos:").grid(row=mr, column=0, sticky=tk.W)
    lbl_model_status = ttk.Label(mframe, textvariable=model_status_var, justify=tk.LEFT)
    lbl_model_status.grid(row=mr, column=1, columnspan=2, sticky=tk.W)

    _update_model_status_label(existing_cfg)

    # ------------------ translator controls ------------------
    control_row = row_index + 1
    verify_btn = ttk.Button(frm, text="Verificar traductores", command=verify_translators)
    verify_btn.grid(row=control_row, column=0, pady=8)
    test_btn = ttk.Button(frm, text="Probar traducción (A)", command=_do_translation_test)
    test_btn.grid(row=control_row, column=1, pady=8, sticky="w")
    # Add a visible cache-clear button to advanced section
    def _clear_caches() -> None:
        try:
            confirm = messagebox.askyesno("Limpiar cachés", "¿Desea eliminar la caché de metadata y la caché de traducciones persistente? Esto no afectará los archivos de datos (JSON).")
            if not confirm:
                return
            # clear metadata cache
            try:
                if os.path.exists(CACHE_FILE):
                    os.remove(CACHE_FILE)
                _ensure_cache()
            except Exception as e:
                messagebox.showwarning("Limpiar cachés", f"No se pudo limpiar la cache de metadata: {e}")
                return
            # clear translation persistent cache if available
            try:
                from src.translator import translation_cache as _translation_cache
                try:
                    summary = _translation_cache.clear()
                    messagebox.showinfo("Limpiar cachés", f"Cachés limpiadas. Traducciones eliminadas: {summary.get('entries', 0)} entradas.")
                except Exception:
                    messagebox.showinfo("Limpiar cachés", "Caché de metadata limpiada. No se pudo limpiar la caché de traducciones persistente.")
            except Exception:
                # translation cache module not available
                messagebox.showinfo("Limpiar cachés", "Caché de metadata limpiada.")
        except Exception as e:
            try:
                messagebox.showerror("Limpiar cachés", f"Error al limpiar caches: {e}")
            except Exception:
                pass

    clear_btn = ttk.Button(frm, text="Limpiar cachés", command=_clear_caches)
    clear_btn.grid(row=control_row, column=2, pady=8, sticky="e")

    # ------------------ Save/Cancel ------------------
    def on_save() -> None:
        new_cfg = existing_cfg.copy()
        for k in entries:
            try:
                v = entries[k].get().strip()
            except Exception:
                v = ""
            if v:
                new_cfg[k] = v
        # Normalize media_root_dir
        try:
            if "media_root_dir" in new_cfg and new_cfg["media_root_dir"]:
                new_cfg["media_root_dir"] = os.path.abspath(new_cfg["media_root_dir"])
        except Exception:
            pass

        try:
            mroot = new_cfg.get("media_root_dir")
            if mroot and not os.path.exists(mroot):
                create = messagebox.askyesno("Crear carpeta", f"La carpeta {mroot} no existe. ¿Crear?")
                if create:
                    try:
                        os.makedirs(mroot, exist_ok=True)
                    except Exception as e:
                        messagebox.showerror("Error", f"No se pudo crear {mroot}: {e}")
        except Exception:
            pass

        try:
            new_cfg["preload_translator_on_start"] = bool(preload_var.get())
        except Exception:
            pass
        try:
            new_cfg["argos_auto_install_models"] = bool(argos_auto_var.get())
        except Exception:
            pass
        try:
            new_cfg["translator_auto_download_models"] = bool(auto_download_var.get())
        except Exception:
            pass

        try:
            _ensure_json_file(new_cfg.get("anime_json_path"))  # type: ignore
            _ensure_json_file(new_cfg.get("movies_json_path"))  # type: ignore
        except Exception:
            pass

        if resource_probe_flag.get("requested"):
            new_cfg["resource_probe_done"] = False

        try:
            new_cfg["config_initialized"] = True
        except Exception:
            pass

        # numeric casts
        try:
            if "translator_batch_size" in new_cfg:
                new_cfg["translator_batch_size"] = int(new_cfg["translator_batch_size"])
        except Exception:
            pass
        try:
            if "translator_cache_size" in new_cfg:
                new_cfg["translator_cache_size"] = int(new_cfg["translator_cache_size"])
        except Exception:
            pass

        try:
            save_config(new_cfg)
            try:
                deepl_val = entries.get("deepl_api_key") and entries["deepl_api_key"].get().strip()
                if deepl_val:
                    save_secrets({"deepl_api_key": deepl_val})
            except Exception:
                pass
            try:
                tmdb_val = entries.get("tmdb_api_key") and entries["tmdb_api_key"].get().strip()
                if tmdb_val:
                    save_env_key("TMDB_API_KEY", tmdb_val)
                else:
                    save_env_key("TMDB_API_KEY", None)
            except Exception:
                pass

            # If media root changed, do a background scan (non-blocking)
            try:
                if new_cfg.get("media_root_dir") and new_cfg.get("media_root_dir") != existing_cfg.get("media_root_dir"):
                    def _bg_build():
                        try:
                            from src.builder.page_builder import scan_media_root
                            scan_media_root(new_cfg.get("media_root_dir"))  # type: ignore
                        except Exception:
                            pass
                    threading.Thread(target=_bg_build, daemon=True).start()
            except Exception:
                pass

            messagebox.showinfo("Configuración", "Configuración guardada correctamente.")

            tracked_keys = [
                "translator_backend", "translator_batch_size", "translator_device",
                "media_root_dir", "pages_output_dir", "json_link_prefix",
                "m2m_model_path", "m2m_model_name", "local_marian_model_path", "local_marian_model_name",
                "aventiq_model_path", "aventiq_model_name", "translator_models_dir"
                    , "argos_models_dir", "argos_auto_install_models"
            ]
            changes = []
            for k in tracked_keys:
                old = existing_cfg.get(k)
                new = new_cfg.get(k)
                if old != new:
                    changes.append(f"{k}: {old or '—'} -> {new or '—'}")
            if changes:
                try:
                    messagebox.showinfo("Resumen de cambios", "\n".join(changes))
                except Exception:
                    pass

            _set_status("Listo. Cambios guardados.", False)
            try:
                win.destroy()
            except Exception:
                pass
        except Exception as e:
            messagebox.showerror("Error", f"No se pudo guardar la configuración: {e}")

    def on_cancel() -> None:
        try:
            win.destroy()
        except Exception:
            pass

    # Buttons row at the end
    btn_frame = ttk.Frame(frm)
    btn_frame.grid(row=row_index + 2, column=0, columnspan=3, pady=14, sticky="e")
    ttk.Button(btn_frame, text="Guardar", command=on_save).pack(side=tk.LEFT, padx=6)
    ttk.Button(btn_frame, text="Cancelar", command=on_cancel).pack(side=tk.LEFT, padx=6)

    # Ensure initial layout calculations and set scrollregion
    win.update_idletasks()
    _on_frame_configure()

    # Start modal loop
    if created_root:
        try:
            win.mainloop()
        except Exception:
            pass
    else:
        try:
            win.wait_window()
        except Exception:
            pass

    return load_config()


if __name__ == "__main__":
    cfg = load_config() or {}
    ensure_config_via_gui(cfg)
