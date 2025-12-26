import os
import re
import json
import shutil
import threading
import time
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, scrolledtext, messagebox, simpledialog
from tkinter.ttk import Progressbar

# -------------------------
# Configuración general
# -------------------------
AUTO_MODE = False
DRY_RUN_DEFAULT = False
WAIT_SECONDS_DEFAULT = 30  # por defecto espera 30s después de cada carpeta
learning_file = "learning.json"
actions_db = "acciones.json"

video_exts = {'.mp4', '.mkv', '.avi', '.mov', '.flv', '.wmv', '.webm', '.m4v'}
protected_exts = {'.json', '.jpg', '.jpeg', '.png', '.gif'}
protected_dirs = {"Pages"}
forbidden_delete_exts = video_exts | protected_exts

# -------------------------
# Logger (puede ser reemplazado por GUI)
# -------------------------
_logger = lambda msg: print(msg)

def set_logger(fn):
    global _logger
    _logger = fn

# -------------------------
# Helpers de aprendizaje
# -------------------------
def ensure_learning():
    p = Path(learning_file)
    if not p.exists():
        p.write_text(json.dumps({"patterns": {}}, indent=2, ensure_ascii=False), encoding='utf-8')

def load_learning():
    ensure_learning()
    try:
        return json.load(open(learning_file, "r", encoding="utf-8"))
    except Exception:
        return {"patterns": {}}

def save_learning(data):
    json.dump(data, open(learning_file, "w", encoding="utf-8"), indent=2, ensure_ascii=False)

def learn_pattern(folder_name, sample_name):
    data = load_learning()
    key = str(folder_name)
    if key not in data.get("patterns", {}):
        data.setdefault("patterns", {})[key] = sample_name
        save_learning(data)
        _logger(f"🧠 Aprendido patrón para '{key}': '{sample_name}'")

# -------------------------
# Acciones / historial global
# -------------------------
acciones_globales = []

def registrar_accion(folder_name, acciones):
    entry = {"folder": folder_name, "acciones": acciones, "timestamp": time.time()}
    acciones_globales.append(entry)
    try:
        if Path(actions_db).exists():
            data = json.load(open(actions_db, "r", encoding="utf-8"))
        else:
            data = []
        data.append(entry)
        json.dump(data, open(actions_db, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    except Exception as e:
        _logger(f"❌ Error guardando acciones: {e}")


# -------------------------
# Limpiador de ruido y extracción
# -------------------------

def limpiar_ruido(nombre):
    base = Path(nombre).stem
    base = re.sub(r"\(.*?\)", "", base)
    base = re.sub(r"(?:[_\-\s]|)(?:v|ver|version)\s*\d+", "", base, flags=re.IGNORECASE)
    basura = ["final", "remaster", "uncut", "clean", "raw", "sub", "dub", "lat", "latam", "hd", "fullhd", "1080p", "720p"]
    for word in basura:
        base = re.sub(rf"\b{re.escape(word)}\b", "", base, flags=re.IGNORECASE)
    base = re.sub(r"[_\-]+", " ", base)
    base = re.sub(r"\s+", " ", base).strip()
    return base


def extraer_numero(nombre, folder_hint=None):
    base_raw = Path(nombre).stem
    base = limpiar_ruido(nombre)
    if folder_hint:
        data = load_learning()
        pat = data.get("patterns", {}).get(str(folder_hint))
        if pat:
            learned_nums = re.findall(r"(\d{1,4})", pat)
            if learned_nums:
                nums = re.findall(r"(\d{1,4})", base)
                if nums:
                    return int(nums[-1])

    m = re.search(r'_(\d{1,3})_', base_raw)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\D*$", base)
    if m:
        try:
            return int(m.group(1))
        except:
            pass
    m = re.search(r"(\d{1,4})", base)
    if m:
        try:
            return int(m.group(1))
        except:
            pass
    m = re.search(r'(?:S\d+E|S\d+ E\d+|(?:E|EP|Episode|Cap)\s*[:\-\.]?\s*(\d{1,4}))', base, re.IGNORECASE)
    if m:
        nums = re.findall(r"(\d{1,4})", m.group(0))
        if nums:
            return int(nums[-1])
    nums = re.findall(r"(\d{1,4})", base_raw)
    if nums:
        return int(nums[-1])
    return 9999


# -------------------------
# Especiales (ESP/OVA/SPECIAL) handling
# -------------------------
SPECIAL_PREFIXES = ("ESP", "OVA", "SPECIAL")

def detectar_especial(nombre):
    base = Path(nombre).stem.upper()
    for p in SPECIAL_PREFIXES:
        if re.search(rf'\b{p}\s*\d+', base):
            return p
    return None


def generar_moves_con_especiales(videos, root, decisiones_especiales):
    normales = []
    especiales = []
    for v in videos:
        if detectar_especial(v.name):
            especiales.append(v)
        else:
            normales.append(v)
    moves = []
    for i, v in enumerate(normales, 1):
        dst = root / f"{i:02d}{v.suffix.lower()}"
        moves.append((v, dst))
    offset = len(normales)
    extra = 0
    for v in especiales:
        decision = decisiones_especiales.get(v, "auto")
        if decision == "skip":
            continue
        if isinstance(decision, tuple) and decision[0] == "manual":
            dst = root / decision[1]
            moves.append((v, dst))
            extra += 1
            continue
        extra += 1
        dst = root / f"{offset + extra:02d}{v.suffix.lower()}"
        moves.append((v, dst))
    return moves


# -------------------------
# Núcleo: procesar carpeta
# -------------------------

def planear_acciones(folder: Path, folder_hint=None, decisiones_especiales=None):
    root = Path(folder)
    decisiones_especiales = decisiones_especiales or {}
    videos = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in video_exts]
    if not videos:
        return {"moves": [], "deletes": [], "exists_all": False}
    videos.sort(key=lambda p: extraer_numero(p.name, folder_hint))
    planned_moves = generar_moves_con_especiales(videos, root, decisiones_especiales)
    planned_deletes = []
    for item in root.iterdir():
        if item.name in protected_dirs:
            continue
        if item.is_dir():
            planned_deletes.append(item)
        elif item.is_file() and item.suffix.lower() not in forbidden_delete_exts:
            planned_deletes.append(item)
    exists_all = all(dst.exists() for (_, dst) in planned_moves)
    return {"moves": planned_moves, "deletes": planned_deletes, "exists_all": exists_all}


def aplicar_plan(planned, dry_run=False):
    moves_done = []
    deleted = []
    for src, dst in planned.get("moves", []):
        try:
            if dst.exists():
                _logger(f"⏩ Saltado (ya existe): {dst.name}")
                continue
            if dry_run:
                _logger(f"(dry-run) ✅ {src.name} -> {dst.name}")
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                _logger(f"✅ {src.name} → {dst.name}")
            moves_done.append((str(src), str(dst)))
        except Exception as e:
            _logger(f"❌ Error moviendo {src}: {e}")
    for item in planned.get("deletes", []):
        try:
            if dry_run:
                _logger(f"(dry-run) 🗑 {item.name}")
                deleted.append(str(item))
                continue
            if item.is_file():
                item.unlink()
                deleted.append(str(item))
                _logger(f"🗑 Archivo eliminado: {item.name}")
            else:
                shutil.rmtree(item)
                deleted.append(str(item))
                _logger(f"🗑 Carpeta eliminada: {item.name}/")
        except Exception as e:
            _logger(f"❌ Error eliminando {item}: {e}")
    return {"moves_done": moves_done, "deleted": deleted}


# -------------------------
# Undo manager
# -------------------------
undo_stacks = []

def registrar_undo_entry(folder, moves_done, deleted):
    undo_moves = [(new, old) for (old, new) in moves_done]
    undo_stacks.append({"folder": folder, "moves": undo_moves, "deleted": deleted, "time": time.time()})

def ejecutar_undo():
    if not undo_stacks:
        _logger("⚠ Nada para deshacer.")
        return
    entry = undo_stacks.pop()
    folder = entry.get("folder")
    _logger(f"\n⏪ Revirtiendo folder: {folder}")
    for new, old in reversed(entry.get("moves", [])):
        try:
            new_p = Path(new)
            old_p = Path(old)
            if not new_p.exists():
                _logger(f"⚠ {new_p.name} no existe; no se puede revertir este archivo.")
                continue
            old_p.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(new_p), str(old_p))
            _logger(f"⏪ {new_p.name} → {old_p.name}")
        except Exception as e:
            _logger(f"❌ Error revirtiendo {new}: {e}")
    _logger("✔ Revertido completado. (Los archivos eliminados no se restauran automáticamente)")


# -------------------------
# Procedural wrapper expected by GUI
# -------------------------
def procesar(folder, auto_confirm=False, dry_run=False):
    wait_time = WAIT_SECONDS_DEFAULT
    _logger("")
    _logger(f"📂 Carpeta seleccionada: {folder}")
    plan = planear_acciones(Path(folder), folder_hint=Path(folder).name)
    if not plan["moves"]:
        _logger("⚠ No se encontraron videos para renombrar. Saltando...")
        return {"moves": [], "deleted": [], "moved": 0, "deleted_files": 0, "deleted_folders": 0, 'dry_run': dry_run}
    if plan.get("exists_all"):
        _logger("⏩ Saltando: ya existen los videos en destino.")
        return {"moves": [], "deleted": [], 'moved': 0, 'deleted_files': 0, 'deleted_folders': 0, 'dry_run': dry_run}
    # preview - in GUI the logger is hooked and user can inspect output; if not auto_confirm, ask
    if not auto_confirm and not AUTO_MODE:
        if not messagebox.askyesno("Confirmar", "¿Deseas continuar con el proceso?"):
            _logger("❌ Operación cancelada.")
            return {}
    res = aplicar_plan(plan, dry_run=dry_run)
    registrar_undo_entry(Path(folder).name, res.get("moves_done", []), res.get("deleted", []))
    acciones = {"renombres": res.get("moves_done", []), "eliminados": res.get("deleted", [])}
    registrar_accion(Path(folder).name, acciones)
    # aprendizaje
    if res.get("moves_done"):
        first_old = Path(res.get("moves_done")[0][0]).name # type: ignore
        if "_" in first_old or re.search(r"\d+_\d+", first_old):
            learn_pattern(Path(folder).name, first_old)
    _logger("\n🎉 Proceso completado.")
    _logger(f"   ✅ Videos movidos/renombrados: {len(res.get('moves_done', []))}")
    _logger(f"   🗑 Items eliminados: {len(res.get('deleted', []))}")
    return {"moves": plan.get('moves', []), 'deleted': res.get('deleted', []), 'moved': len(res.get('moves_done', [])), 'deleted_files': 0, 'deleted_folders': 0, 'dry_run': dry_run}

