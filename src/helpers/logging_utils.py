"""Console and logging helpers for the friendly CLI runner."""
from __future__ import annotations

import json
import logging
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from logging import Handler
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from rich import box
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.theme import Theme
from rich.text import Text

try:  # Optional dependency, reported as N/A when missing
    import psutil  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    psutil = None

try:
    import requests
except Exception:  # pragma: no cover - optional dependency
    requests = None

TRACE_LEVEL = 5
if not hasattr(logging, "TRACE"):
    logging.addLevelName(TRACE_LEVEL, "TRACE")

    def trace(self, message, *args, **kwargs):  # type: ignore[no-redef]
        if self.isEnabledFor(TRACE_LEVEL):
            self._log(TRACE_LEVEL, message, args, **kwargs)

    setattr(logging.Logger, "trace", trace)  # type: ignore[attr-defined]


THEME = Theme(
    {
        "runner.info": "bold cyan",
        "runner.subtle": "dim",
        "runner.success": "bold green",
        "runner.warning": "bold yellow",
        "runner.error": "bold red",
    }
)


@dataclass
class RunnerContext:
    console: Console
    silent: bool = False


def get_console() -> Console:
    return Console(theme=THEME, highlight=False, emoji=True)


def resolve_log_dir(preferred: str | None = None) -> Path:
    path = Path(preferred or os.environ.get("MULTIMEDIA_LOG_DIR") or Path.cwd() / "logs")
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_log_level(*, debug: bool, trace: bool, silent: bool, default_level: str | int | None = None) -> int:
    if trace:
        return TRACE_LEVEL
    if debug:
        return logging.DEBUG
    if silent:
        return logging.WARNING
    if isinstance(default_level, str):
        return getattr(logging, default_level.upper(), logging.INFO)
    if isinstance(default_level, int):
        return default_level
    return logging.INFO


def _preserve_handlers(root: logging.Logger) -> list[Handler]:
    preserved = []
    for handler in list(root.handlers):
        if getattr(handler, "_preserve", False):
            preserved.append(handler)
        else:
            root.removeHandler(handler)
    return preserved


def configure_rich_logging(
    *,
    level: int,
    log_dir: Path,
    silent: bool = False,
    console: Console | None = None,
    rich_tracebacks: bool = True,
) -> logging.Logger:
    root = logging.getLogger()
    preserved = _preserve_handlers(root)
    root.handlers.clear()
    root.setLevel(level)

    for handler in preserved:
        root.addHandler(handler)

    if not silent:
        rich_console = console or get_console()
        rich_handler = RichHandler(
            console=rich_console,
            rich_tracebacks=rich_tracebacks,
            show_path=False,
            log_time_format="%H:%M:%S",
            level=level,
        )
        rich_handler.setLevel(level)
        root.addHandler(rich_handler)

    log_dir.mkdir(parents=True, exist_ok=True)
    daily_file = log_dir / f"{datetime.now():%Y-%m-%d}.log"
    file_handler = logging.FileHandler(daily_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    file_handler._preserve = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)

    return root


def friendly_banner(context: RunnerContext, title: str, subtitle: str | None = None, emoji: str = "🐾") -> None:
    if context.silent:
        return
    panel = Panel.fit(
        Text(f"{emoji}  {title}", style="runner.info"),
        subtitle=subtitle,
        border_style="bright_magenta",
    )
    context.console.print(panel)


def friendly_footer(context: RunnerContext, elapsed: float, summary: str) -> None:
    if context.silent:
        return
    panel = Panel.fit(
        Text(f"{summary}\n⏱️  {elapsed:.2f}s", style="runner.success"),
        title="Hasta pronto",
        border_style="bright_green",
    )
    context.console.print(panel)


def collect_system_metrics() -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "available_ram_gb": None,
        "cpu_percent": None,
        "cpu_count": os.cpu_count(),
    }
    if psutil is None:
        return metrics
    try:
        vm = psutil.virtual_memory()
        metrics["available_ram_gb"] = round(getattr(vm, "available", 0) / (1024 ** 3), 2)
    except Exception:
        metrics["available_ram_gb"] = None
    try:
        metrics["cpu_percent"] = psutil.cpu_percent(interval=0.2)
    except Exception:
        metrics["cpu_percent"] = None
    return metrics


def measure_provider_latencies(targets: dict[str, str]) -> dict[str, float | None]:
    results: dict[str, float | None] = {k: None for k in targets}
    if requests is None:
        return results
    for name, url in targets.items():
        def _probe() -> float:
            start = time.perf_counter()
            resp = requests.get(url, timeout=3)
            resp.raise_for_status()
            return round((time.perf_counter() - start) * 1000, 1)

        try:
            latency = retry_with_backoff(_probe, description=f"latencia {name}")
            results[name] = latency
        except Exception as exc:  # pragma: no cover - telemetry only
            logging.getLogger(__name__).debug("Latency probe for %s failed: %s", name, exc)
            results[name] = None
    return results


def render_dashboard(
    context: RunnerContext,
    *,
    phase: str,
    metrics: dict[str, Any],
    latencies: dict[str, float | None],
    new_count: int,
    elapsed: float | None = None,
) -> None:
    if context.silent:
        return
    table = Table(title=f"{phase} del recorrido", box=box.ROUNDED, expand=False)
    table.add_column("Métrica", style="runner.subtle")
    table.add_column("Valor", style="runner.info")

    ram = metrics.get("available_ram_gb")
    cpu = metrics.get("cpu_percent")
    table.add_row("RAM disponible", f"{ram:.2f} GB" if isinstance(ram, (float, int)) and ram is not None else "N/A")
    table.add_row("CPU en uso", f"{cpu:.1f}%" if isinstance(cpu, (float, int)) and cpu is not None else "N/A")
    table.add_row("Nuevas carpetas", f"{new_count}")

    for name, latency in latencies.items():
        table.add_row(f"Latencia {name}", format_latency(latency))

    if elapsed is not None:
        table.add_row("Tiempo total", f"{elapsed:.2f}s")

    context.console.print(table)


def format_latency(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1f} ms"


def render_new_folders_table(context: RunnerContext, new_folders: Sequence[str]) -> None:
    if context.silent or not new_folders:
        return
    table = Table(title="Carpetas nuevas detectadas 🐕", box=box.SIMPLE_HEAVY)
    table.add_column("#", justify="right", style="runner.subtle")
    table.add_column("Carpeta", style="runner.info")
    for idx, folder in enumerate(new_folders, start=1):
        table.add_row(str(idx), folder)
    context.console.print(table)


def load_state(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh) or {}
    except Exception:
        logging.getLogger(__name__).debug("No se pudo leer %s", path, exc_info=True)
    return {"processed_folders": []}


def save_state(path: Path, processed_folders: Sequence[str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"processed_folders": sorted(set(processed_folders))}
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        tmp.replace(path)
    except Exception:
        logging.getLogger(__name__).warning("No se pudo guardar %s", path, exc_info=True)


def detect_new_folders(media_root: Path | str | None, known: Sequence[str]) -> tuple[list[str], list[str]]:
    root = Path(media_root) if media_root else Path()
    processed = set(known or [])
    if not media_root or not root.exists():
        return [], []
    seen: list[str] = []
    new_items: list[str] = []
    for folder in sorted([p for p in root.iterdir() if p.is_dir()]):
        seen.append(folder.name)
        if folder.name not in processed:
            new_items.append(folder.name)
    return new_items, seen


def validate_paths_with_feedback(
    context: RunnerContext,
    labeled_paths: Sequence[tuple[str, str | None]],
) -> tuple[bool, list[tuple[str, Path]]]:
    missing: list[tuple[str, Path]] = []
    total = len(labeled_paths)
    if total == 0:
        return True, missing
    with Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(bar_width=None),
        TimeElapsedColumn(),
        console=context.console,
        transient=True,
        disable=context.silent,
    ) as progress:
        task_id = progress.add_task("Revisando rutas", total=total)
        for label, raw in labeled_paths:
            path = Path(raw or "")
            if not raw or not path.exists():
                missing.append((label, path))
            progress.advance(task_id)
    if missing and not context.silent:
        table = Table(title="Rutas incompletas", box=box.SIMPLE, style="runner.warning")
        table.add_column("Recurso", style="runner.subtle")
        table.add_column("Ruta", style="runner.info")
        for label, path in missing:
            table.add_row(label, str(path) or "<no definida>")
        context.console.print(table)
    return len(missing) == 0, missing


def retry_with_backoff(
    func: Callable[[], Any],
    *,
    attempts: int = 3,
    base_delay: float = 0.6,
    max_delay: float = 4.0,
    description: str | None = None,
) -> Any:
    last_exc: Exception | None = None
    logger = logging.getLogger(__name__)
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except Exception as exc:  # pragma: no cover - retry helper
            last_exc = exc
            wait = min(max_delay, base_delay * (2 ** (attempt - 1)))
            wait += random.uniform(0, 0.15)
            logger.debug("Reintentando %s (%s/%s)", description or func.__name__, attempt, attempts)
            time.sleep(wait)
    raise last_exc if last_exc else RuntimeError("retry_with_backoff failed")


def timed(name: str | None = None, level: int = logging.DEBUG) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        label = name or func.__name__

        @wraps(func)
        def wrapper(*args, **kwargs):
            start = time.perf_counter()
            try:
                return func(*args, **kwargs)
            finally:
                elapsed = time.perf_counter() - start
                logger = logging.getLogger(func.__module__)
                if logger.isEnabledFor(level):
                    logger.log(level, "⏱️  %s tomó %.2fs", label, elapsed)

        return wrapper

    return decorator


def decide_provider_fallback(current: str | None, latencies: dict[str, float | None]) -> str | None:
    preferred = (current or "jikan").lower()
    jikan_latency = latencies.get("Jikan")
    tmdb_latency = latencies.get("TMDB")

    if preferred == "jikan" and jikan_latency is None and tmdb_latency is not None:
        return "tmdb"
    if preferred == "tmdb" and tmdb_latency is None and jikan_latency is not None:
        return "jikan"
    return None
