"""Copyable, one-line rendering of resolved configuration reports."""

from __future__ import annotations

import json
from typing import Any


def _single_line(value: str) -> str:
    """Escape control characters while preserving path separators and Unicode."""
    escapes = {"\r": r"\r", "\n": r"\n", "\t": r"\t"}
    return "".join(
        escapes.get(char, f"\\u{ord(char):04x}")
        if ord(char) < 32 or 127 <= ord(char) <= 159 or char in "\u2028\u2029"
        else char
        for char in value
    )


def config_lines(value: Any, prefix: str = "") -> list[str]:
    """Flatten mappings and arrays into key=value lines in report order."""
    if isinstance(value, dict):
        if not value:
            return [f"{prefix}={{}}"]
        return [
            line
            for key, item in value.items()
            for line in config_lines(item, f"{prefix}.{_single_line(key)}" if prefix else _single_line(key))
        ]
    if isinstance(value, list):
        if not value:
            return [f"{prefix}=[]"]
        return [
            line
            for index, item in enumerate(value)
            for line in config_lines(item, f"{prefix}[{index}]")
        ]
    display = _single_line(value) if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return [f"{prefix}={display}"]
