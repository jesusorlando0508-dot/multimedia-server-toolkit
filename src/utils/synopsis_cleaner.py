"""Utilities to clean up synopsis text fetched from external providers."""
from __future__ import annotations

import re
from typing import Callable, Optional

# Precompile regexes for performance
EDITORIAL_PATTERNS = re.compile(r"\((?:source|written|provided)[^)]*\)|\[(?:source|written|provided)[^\]]*\]|\{(?:source|written|provided)[^}]*\}", re.IGNORECASE)
SECTION_PREFIXES = (
    "background:",
    "note:",
    "notes:",
    "for more information",
    "this entry is still incomplete",
    "visit the official site",
    "winner of",
    "award",
    "prize",
)
HTML_REPLACEMENTS = {
    "&nbsp;": " ",
    "&mdash;": "—",
    "&quot;": '"',
    "&#039;": "'",
}
EMPTY_PARENS = re.compile(r"\(\s*\)|\[\s*\]|\{\s*\}")
MULTISPACE = re.compile(r"\s+")

EmitFn = Optional[Callable[[str], None]]


def clean_synopsis(text: str | None, emit: EmitFn = None) -> str:
    if not text:
        return ""

    def _emit(message: str) -> None:
        if emit:
            try:
                emit(message)
            except Exception:
                pass

    _emit("Limpiando sinopsis…")
    original = text.strip()
    _emit(f"Sinopsis original {len(original)} chars")

    cleaned = EDITORIAL_PATTERNS.sub("", original)

    lines = cleaned.splitlines()
    filtered_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        lower = stripped.lower()
        if any(lower.startswith(prefix) for prefix in SECTION_PREFIXES):
            continue
        filtered_lines.append(stripped)
    cleaned = " ".join(filtered_lines)

    for html_entity, replacement in HTML_REPLACEMENTS.items():
        cleaned = cleaned.replace(html_entity, replacement)

    cleaned = EMPTY_PARENS.sub("", cleaned)
    cleaned = MULTISPACE.sub(" ", cleaned).strip()
    _emit(f"Sinopsis final {len(cleaned)} chars")
    return cleaned
