from __future__ import annotations
import os
import shutil
import time
from typing import Optional

import json

from config import CONFIG_PATH, SECRETS_PATH, load_config, save_config, save_secrets
import argparse
import stat
import shutil
import subprocess
from pathlib import Path


def abspath_expand(p: Optional[str]) -> Optional[str]:
    if p is None:
        return None
    p = p.strip()
    if not p:
        return None
    return os.path.abspath(os.path.expanduser(p))


def prompt_with_default(prompt: str, default: Optional[str]) -> str:
    if default:
        val = input(f"{prompt} [{default}]: ")
        return val.strip() or default
    else:
        val = input(f"{prompt}: ")
        return val.strip()


def backup_config(path: os.PathLike | str) -> None:
    p = str(path)
    if not os.path.exists(p):
        return
    stamp = time.strftime("%Y%m%d_%H%M%S")
    bak = f"{p}.bak.{stamp}"
    shutil.copy2(p, bak)
    print(f"Backup created: {bak}")


def ensure_dir(path: str) -> bool:
    if os.path.exists(path):
        return True
    try:
        os.makedirs(path, exist_ok=True)
        return True
    except Exception as e:
        print(f"No se pudo crear la carpeta {path}: {e}")
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Setup project config (interactive or --yes to apply current values)")
    parser.add_argument("--yes", "-y", action="store_true", help="Apply current configuration non-interactively")
    args = parser.parse_args()

    cfg = load_config() or {}

    if args.yes:
        # Non-interactive: just use current config values and persist them
        pages_dir = cfg.get("pages_output_dir")
        media_root = cfg.get("media_root_dir") or cfg.get("ASSETS_DIR")
        output_json = cfg.get("anime_json_path")
        json_prefix = cfg.get("json_link_prefix")
        interactive = False
    else:
        interactive = True
        print("Configuración actual (valores entre corchetes son por defecto; dejar vacío para mantener):")

        pages_dir = prompt_with_default("Ruta filesystem donde se generan las páginas (pages_output_dir)", cfg.get("pages_output_dir"))
        pages_dir = abspath_expand(pages_dir) or cfg.get("pages_output_dir")

        media_prompt_default = cfg.get("media_root_dir") or cfg.get("ASSETS_DIR") or cfg.get("assets_prefix")
        media_root = prompt_with_default("Ruta filesystem para media root (media_root_dir)", media_prompt_default)
        media_root = abspath_expand(media_root) or cfg.get("media_root_dir") or cfg.get("ASSETS_DIR")

        output_json = prompt_with_default("Ruta filesystem para el JSON agregado (anime_json_path)", cfg.get("anime_json_path"))
        output_json = abspath_expand(output_json) or cfg.get("anime_json_path")

        json_prefix = prompt_with_default("Prefijo web para enlaces JSON (json_link_prefix). Ej: /Vista/pages/ or /pages/", cfg.get("json_link_prefix"))
        json_prefix = json_prefix or cfg.get("json_link_prefix")

    # Normalize paths
    pages_dir = abspath_expand(pages_dir) or cfg.get("pages_output_dir")
    media_root = abspath_expand(media_root) or cfg.get("media_root_dir") or cfg.get("ASSETS_DIR")
    output_json = abspath_expand(output_json) or cfg.get("anime_json_path")

    # Confirm and possibly create directories when interactive
    if interactive:
        print("\nResumen de cambios propuestos:")
        print(f" pages_output_dir: {pages_dir}")
        print(f" media_root_dir: {media_root}")
        print(f" anime_json_path: {output_json}")
        print(f" json_link_prefix: {json_prefix}")

        ok = input("Aplicar y guardar esta configuración? (y/N): ").strip().lower() == "y"
        if not ok:
            print("Cancelado por el usuario. Ningún cambio aplicado.")
            return

        # Create directories if missing
        if pages_dir and not os.path.exists(pages_dir):
            create = input(f"La carpeta {pages_dir} no existe. ¿Crear? (y/N): ").strip().lower() == "y"
            if create:
                if not ensure_dir(pages_dir):
                    print("No se pudo crear la carpeta de páginas; abortando.")
                    return

    # Backup existing config.json
    try:
        backup_config(CONFIG_PATH)
    except Exception as e:
        print(f"Aviso: no se pudo crear backup de config: {e}")

    new_cfg = cfg.copy() if isinstance(cfg, dict) else {}
    if pages_dir:
        new_cfg["pages_output_dir"] = pages_dir
        new_cfg["BASE_PAGES_DIR"] = pages_dir
    if media_root:
        new_cfg["media_root_dir"] = media_root
    if output_json:
        new_cfg["anime_json_path"] = output_json
        new_cfg["OUTPUT_JSON_PATH"] = output_json
    if json_prefix:
        new_cfg["json_link_prefix"] = json_prefix
    # legacy assets_prefix intentionally not persisted (deprecated)

    # If config file exists and is not writable, temporarily make it writable
        def make_writable(path: os.PathLike | str):
            try:
                p = str(path)
                if os.path.exists(p):
                    os.chmod(p, 0o600)
            except Exception:
                pass

    def make_readonly_owner(path: os.PathLike | str):
        try:
            p = str(path)
            if os.path.exists(p):
                os.chmod(p, 0o400)
        except Exception:
            pass

    try:
        make_writable(CONFIG_PATH)
        saved = save_config(new_cfg)
    except Exception as e:
        print(f"Error al guardar config: {e}")
        saved = False

    # After saving, make config.json read-only for the owner to protect accidental edits
    try:
        make_readonly_owner(CONFIG_PATH)
    except Exception:
        pass

    # Ensure .secrets.json has restrictive perms (save_secrets already attempts this, but ensure here too)
    try:
        if os.path.exists(SECRETS_PATH):
            os.chmod(SECRETS_PATH, 0o600)
    except Exception:
        pass

    if saved is None:
        # older save_config might not return a value; assume success
        saved = True

    if saved:
        print("Configuración guardada con éxito. `config.json` marcado como no editable (solo lectura para el propietario).")
    else:
        print("No se pudo guardar la configuración. Revisa permisos y ruta de config.json.")

    # If repo-local config exists, offer to migrate it to user config dir if different
    try:
        repo_conf = os.path.join(os.path.dirname(__file__), "config.json")
        if os.path.exists(repo_conf) and os.path.abspath(repo_conf) != os.path.abspath(CONFIG_PATH):
            # If current CONFIG_PATH is inside user config dir, migrate
            migrate = input(f"Se detectó una configuración local en {repo_conf}. ¿Migrar al perfil de usuario ({CONFIG_PATH})? (y/N): ").strip().lower() == "y"
            if migrate:
                try:
                    backup_config(CONFIG_PATH)
                    shutil.copy2(repo_conf, CONFIG_PATH)
                    print(f"Migrado {repo_conf} -> {CONFIG_PATH}")
                    # attempt to remove repo-local config for safety
                    try:
                        os.remove(repo_conf)
                    except Exception:
                        pass
                    try:
                        os.chmod(CONFIG_PATH, 0o400)
                    except Exception:
                        pass
                except Exception as e:
                    print(f"No se pudo migrar la configuración: {e}")
    except Exception:
        pass

    # Offer to encrypt secrets with GPG if available and secrets file exists
    try:
        repo_secrets = os.path.join(os.path.dirname(__file__), ".secrets.json")
        secrets_path = SECRETS_PATH
        if os.path.exists(repo_secrets) or os.path.exists(secrets_path):
            gpg = shutil.which("gpg") or shutil.which("gpg2")
            if gpg:
                want = input("GPG detectado: ¿Quieres cifrar tus secretos con GPG (archivo .secrets.json.gpg)? (y/N): ").strip().lower() == "y"
                if want:
                    src = repo_secrets if os.path.exists(repo_secrets) else secrets_path
                    try:
                        out_gpg = str(secrets_path) + ".gpg"
                        # Use symmetric encryption; user will be prompted for a passphrase by gpg
                        subprocess.run([gpg, "--symmetric", "--cipher-algo", "AES256", "-o", out_gpg, src], check=True)
                        # Remove plaintext file
                        try:
                            os.remove(src)
                        except Exception:
                            pass
                        print(f"Secrets cifrados en {out_gpg}. Para usarlos deberás descifrarlos con gpg cuando haga falta.")
                    except Exception as e:
                        print(f"Fallo al cifrar secretos con gpg: {e}")
    except Exception:
        pass
    else:
        print("No se pudo guardar la configuración. Revisa permisos y ruta de config.json.")
    cfg = load_config() or {}
    print("Configuración actual (valores entre corchetes son por defecto; dejar vacío para mantener):")

    pages_dir = prompt_with_default("Ruta filesystem donde se generan las páginas (pages_output_dir)", cfg.get("pages_output_dir"))
    pages_dir = abspath_expand(pages_dir) or cfg.get("pages_output_dir")

    media_root = prompt_with_default("Ruta filesystem para media root (media_root_dir)", cfg.get("media_root_dir"))
    media_root = abspath_expand(media_root) or cfg.get("media_root_dir")

    output_json = prompt_with_default("Ruta filesystem para el JSON agregado (anime_json_path)", cfg.get("anime_json_path"))
    output_json = abspath_expand(output_json) or cfg.get("anime_json_path")

    json_prefix = prompt_with_default("Prefijo web para enlaces JSON (json_link_prefix). Ej: /Vista/pages/ or /pages/", cfg.get("json_link_prefix"))
    json_prefix = json_prefix or cfg.get("json_link_prefix")

    # Confirm and possibly create directories
    print("\nResumen de cambios propuestos:")
    print(f" pages_output_dir: {pages_dir}")
    print(f" media_root_dir: {media_root}")
    print(f" anime_json_path: {output_json}")
    print(f" json_link_prefix: {json_prefix}")

    ok = input("Aplicar y guardar esta configuración? (y/N): ").strip().lower() == "y"
    if not ok:
        print("Cancelado por el usuario. Ningún cambio aplicado.")
        return

    # Create directories if missing
    if pages_dir and not os.path.exists(pages_dir):
        create = input(f"La carpeta {pages_dir} no existe. ¿Crear? (y/N): ").strip().lower() == "y"
        if create:
            if not ensure_dir(pages_dir):
                print("No se pudo crear la carpeta de páginas; abortando.")
                return

    # Backup existing config.json
    try:
        backup_config(CONFIG_PATH)
    except Exception as e:
        print(f"Aviso: no se pudo crear backup de config: {e}")

    new_cfg = cfg.copy() if isinstance(cfg, dict) else {}
    if pages_dir:
        new_cfg["pages_output_dir"] = pages_dir
        new_cfg["BASE_PAGES_DIR"] = pages_dir
    if media_root:
        new_cfg["media_root_dir"] = media_root
    if output_json:
        new_cfg["anime_json_path"] = output_json
        new_cfg["OUTPUT_JSON_PATH"] = output_json
    if json_prefix:
        new_cfg["json_link_prefix"] = json_prefix

    saved = False
    try:
        saved = save_config(new_cfg)
    except Exception as e:
        print(f"Error al guardar config: {e}")

    if saved is None:
        # older save_config might not return a value; assume success
        saved = True

    if saved:
        print("Configuración guardada con éxito. Puedes editar 'config.json' manualmente si lo deseas.")
    else:
        print("No se pudo guardar la configuración. Revisa permisos y ruta de config.json.")


if __name__ == "__main__":
    main()
