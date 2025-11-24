import os
import re
import shutil
import tkinter as tk
from tkinter import filedialog, scrolledtext, messagebox

# ⚙️ Configuración
AUTO_MODE = False
video_exts = {'.mp4', '.mkv', '.avi', '.mov', '.flv', '.wmv', '.webm', '.m4v'}
protected_exts = {'.json', '.jpg', '.jpeg', '.png', '.gif'}
protected_dirs = {"Pages"}  # carpetas que no se deben borrar

def _noop_log(msg):
    # placeholder logger for non-GUI use
    try:
        print(msg)
    except Exception:
        pass

# default logger used when module functions are called programmatically
_logger = _noop_log

def set_logger(fn):
    """Replace internal logger function used by procesar (for UI or tests)."""
    global _logger
    _logger = fn

# ================================
def extraer_numero(nombre):
    base = os.path.splitext(nombre)[0]
    base_clean = base.replace('_', ' ')
    # 1) Common pattern: ' - 05 [720p]' or ' Name - 05 ' — number before a bracket or end
    m = re.search(r'[-\s]+(\d{1,3})(?=\s*(?:\[|$))', base_clean)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass
    # 2) Prefix patterns: E, EP, Ep, Cap, Episode, ep  (e.g. 'E05', 'EP 05', 'Episode 5')
    m = re.search(r'(?:E|EP|EPISODE|Episode|Cap\.?|C|ep)\s*[:\-\.]?\s*(\d{1,4})', base, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass
    # 3) Year-style prefix like '2020_03' or '202003_01'
    m = re.match(r'^(?:\d{4})[_\s-]?(\d{1,3})', base)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass
    # 4) Fallback: find all standalone numbers ignoring common non-episode tokens (resolutions etc.)
    numeros = re.findall(r'\b(\d{1,4})\b', base)
    candidatos = [int(n) for n in numeros if int(n) not in (720, 1080, 2160) and int(n) < 2000 and int(n) > 0]
    if candidatos:
        # Prefer the number closest to the end of filename (likely the episode index)
        # Map numbers to their last position in the base string
        pos_list = []
        for num in candidatos:
            s = str(num)
            pos = base.rfind(s)
            pos_list.append((pos, num))
        # sort by position descending and return the first (closest to end)
        pos_list.sort(reverse=True)
        return pos_list[0][1]
    # Nothing found: return a large number so it sorts to the end
    return 9999

# ================================
def seleccionar_carpeta():
    folder = filedialog.askdirectory(title="Selecciona la carpeta donde trabajar")
    if not folder:
        _logger("❌ No se seleccionó ninguna carpeta.")
        return
    procesar(folder)

# ================================
def procesar(folder, auto_confirm=False, dry_run=False):
    """Process a folder: move and rename video files into sequential names.

    If auto_confirm is True, no GUI confirmation dialogs are shown.
    This function is safe to call programmatically (it won't create a Tk root).
    """
    _logger(f"\n📂 Carpeta seleccionada: {folder}")

    # Buscar videos en raíz y subcarpetas
    videos = []
    for root_, dirs, files in os.walk(folder):
        for f in files:
            if os.path.splitext(f)[1].lower() in video_exts:
                videos.append(os.path.join(root_, f))

    # Ordenar por número de episodio
    videos.sort(key=lambda x: extraer_numero(os.path.basename(x)))

    # Archivos y carpetas en la raíz
    others = [f for f in os.listdir(folder) if f not in protected_dirs]

    # Vista previa en log
    planned_moves = []
    planned_deletes = []

    if videos:
        _logger("\n✅ Videos encontrados (se moverán y renombrarán en la carpeta seleccionada):")
        for i, file in enumerate(videos, 1):
            ext = os.path.splitext(file)[1]
            _logger(f"   {os.path.basename(file)} → {i:02d}{ext}")
            planned_moves.append((file, os.path.join(folder, f"{i:02d}{ext}")))
    else:
        _logger("\n⚠ No se encontraron videos.")

    # Archivos/carpetas que serían eliminados
    _logger("\n🗑 Archivos/carpetas que se eliminarán después de mover los videos (solo no protegidos):")
    for f in others:
        path = os.path.join(folder, f)
        if os.path.isfile(path) and os.path.splitext(f)[1].lower() not in video_exts | protected_exts:
            _logger(f"   {f}")
            planned_deletes.append(path)
        elif os.path.isdir(path) and f not in protected_dirs:
            _logger(f"   {f}")
            planned_deletes.append(path)

    # Confirmación (GUI) unless auto_confirm is True
    if not auto_confirm and not AUTO_MODE:
        if not messagebox.askyesno("Confirmar", "¿Deseas continuar con el proceso?"):
            _logger("❌ Operación cancelada.")
            return

    # ================================
    # Mover y renombrar videos
    count_videos = 0
    for i, old_path in enumerate(videos, 1):
        ext = os.path.splitext(old_path)[1]
        new_name = f"{i:02d}{ext}"
        new_path = os.path.join(folder, new_name)
        if os.path.exists(new_path):
            _logger(f"⚠ {new_name} ya existe en {folder}, saltando.")
        else:
            if dry_run:
                _logger(f"(dry-run) ✅ {os.path.basename(old_path)} -> {new_name}")
            else:
                shutil.move(old_path, new_path)
                _logger(f"✅ {os.path.basename(old_path)} → {new_name}")
            count_videos += 1

    # ================================
    # Eliminar archivos/carpetas no protegidos
    count_files = count_folders = 0
    for f in others:
        path = os.path.join(folder, f)
        if os.path.isfile(path):
            ext = os.path.splitext(f)[1].lower()
            if ext not in video_exts | protected_exts:
                if dry_run:
                    _logger(f"(dry-run) 🗑 Archivo eliminado: {f}")
                else:
                    os.remove(path)
                    _logger(f"🗑 Archivo eliminado: {f}")
                count_files += 1
        elif os.path.isdir(path):
            if f not in protected_dirs:
                if dry_run:
                    _logger(f"(dry-run) 🗑 Carpeta eliminada: {f}")
                else:
                    shutil.rmtree(path)
                    _logger(f"🗑 Carpeta eliminada: {f}")
                count_folders += 1

    # ================================
    _logger("\n🎉 Proceso completado.")
    _logger("📊 Resumen:")
    _logger(f"   ✅ Videos movidos/renombrados: {count_videos}")
    _logger(f"   🗑 Archivos eliminados: {count_files}")
    _logger(f"   🗑 Carpetas eliminadas: {count_folders}")
    _logger("\n---------------------------------------")

    return {
        'moves': planned_moves,
        'deletes': planned_deletes,
        'moved': count_videos,
        'deleted_files': count_files,
        'deleted_folders': count_folders,
        'dry_run': dry_run
    }

# ================================
def _run_gui():
    # create GUI only when invoked as script
    root = tk.Tk()
    root.title("Organizador de Videos")
    root.geometry("700x500")

    log_box = scrolledtext.ScrolledText(root, width=85, height=25, state="normal")
    log_box.pack(padx=10, pady=10)

    def gui_log(msg):
        log_box.insert(tk.END, msg + "\n")
        log_box.see(tk.END)
        root.update_idletasks()

    set_logger(gui_log)

    btn = tk.Button(root, text="Seleccionar carpeta y ejecutar", command=seleccionar_carpeta)
    btn.pack(pady=5)

    root.mainloop()


if __name__ == '__main__':
    _run_gui()
