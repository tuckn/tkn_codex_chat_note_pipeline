"""Read optional local chat names and derive honest display-name fallbacks."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from .chat_logs import clean_user_text


def display_title(text: str) -> str:
    cleaned = clean_user_text(text)
    if not cleaned or cleaned.startswith("<external_codex_apps_"):
        return ""
    for marker in ("## My request:", "## My request for Codex:"):
        if marker in cleaned:
            cleaned = cleaned.split(marker, 1)[1]
    return " ".join(cleaned.strip().split())[:100]


def conversation_title(title: str, requests: Iterable[str], thread_id: str = "") -> str:
    if name := display_title(title):
        return name
    for request in requests:
        if name := display_title(request):
            return name
    return "Codex conversation" + (" " + thread_id[:8] if thread_id else "")


def _add_title(titles: dict[str, str], line: str) -> None:
    try:
        record = json.loads(line)
    except ValueError:
        return
    if not isinstance(record, dict):
        return
    id, title = record.get("id"), record.get("thread_name")
    if isinstance(id, str) and isinstance(title, str) and (name := display_title(title)):
        titles[id] = name


def parse_chat_titles(lines: Iterable[str]) -> dict[str, str]:
    titles: dict[str, str] = {}
    for line in lines:
        _add_title(titles, line)
    return titles


def load_chat_titles(path: Path) -> dict[str, str]:
    """Session index names are optional display metadata, never semantic evidence."""
    titles: dict[str, str] = {}
    try:
        with path.open(encoding="utf-8-sig") as stream:
            for line in stream:
                _add_title(titles, line)
    except (OSError, UnicodeError):
        pass  # Optional local metadata can be absent, including on imported sources.
    return titles
