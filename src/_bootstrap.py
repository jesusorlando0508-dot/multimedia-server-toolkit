"""Bootstrap helpers to ensure the project root is importable.

This module is safe to import from any entry-point and will make sure that the
repository root directory is present in ``sys.path`` regardless of where the
project folder is located.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SRC_DIR.parent


def ensure_project_root_on_path() -> None:
    """Insert the repository root into ``sys.path`` if it is missing."""
    root_str = str(PROJECT_ROOT)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)


def _install_rich_traceback() -> None:
    if os.environ.get("MM_SERVER_DISABLE_RICH_TRACEBACK"):
        return
    try:
        from rich.traceback import install

        install(show_locals=False, width=100)
    except Exception:
        # Prefer resilience over fanciness: if Rich is missing keep default behavior
        pass


# Execute on import so that simply importing this module bootstraps the path and tracebacks.
ensure_project_root_on_path()
_install_rich_traceback()
