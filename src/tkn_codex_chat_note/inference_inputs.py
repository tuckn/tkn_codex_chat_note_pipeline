"""Conservative inference-only compaction; canonical evidence is never changed."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Sequence
from hashlib import sha256
from typing import Any

from .chat_logs import ChatEvent


def _output_body(text: str) -> str | None:
    match = re.search(r"(?:^|\n)(?:Final output|Output):\r?\n", text)
    return text[match.end():] if match else None


def _line_endings(text: str) -> str:
    return re.sub(r"\r+\n", "\n", text).strip("\r\n")


def _same_output(completed: str, response: str) -> bool:
    if _line_endings(completed) == _line_endings(response):
        return True
    # A debug-escaped completion must match a whole-string encoding of the
    # response. Never decode selected escapes in otherwise plain output/code.
    if any(char in completed for char in "\r\n\t\x1b"):
        return False
    def trim_endings(text: str) -> str:
        return re.sub(r"^(?:\\r|\\n)+|(?:\\r|\\n)+$", "", text)
    for text in (response, re.sub(r"\r+\n", "\r\n", response), _line_endings(response)):
        encoded = json.dumps(text, ensure_ascii=False)[1:-1].replace("\\u001b", "\\x1b")
        if trim_endings(completed) == trim_endings(encoded):
            return True
    return False


def compact_duplicate_outputs(events: Sequence[ChatEvent]) -> dict[str, str]:
    """Replace only an identical command output with its same-call source reference.

    Retain the completion event, status, command, timestamps, and all other fields.
    Ambiguous IDs, different turns/branches, user boundaries, or changed output
    keep both bodies. The referenced response always remains in the input set.
    """
    responses: dict[tuple[str, str, int, str], list[ChatEvent]] = defaultdict(list)
    completions: dict[tuple[str, str, int, str], list[tuple[ChatEvent, dict[str, Any]]]] = defaultdict(list)
    boundary = 0
    for event in events:
        if event.actor == "user":
            boundary += 1
        scope = (event.branch_id, event.turn_id, boundary)
        if event.kind == "tool_result" and event.name:
            responses[(*scope, event.name)].append(event)
        if event.name != "item_completed":
            continue
        try:
            payload = json.loads(event.text)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        item = payload.get("item")
        if (
            isinstance(item, dict) and item.get("type") in {"CommandExecution", "commandExecution"}
            and isinstance(item.get("id"), str) and item["id"]
            and isinstance(item.get("aggregated_output"), str)
        ):
            completions[(*scope, item["id"])].append((event, payload))
    replacements: dict[str, str] = {}
    for key, entries in completions.items():
        matches = responses.get(key, [])
        if len(entries) != 1 or len(matches) != 1:
            continue
        event, payload = entries[0]
        result = matches[0]
        body = _output_body(result.text)
        original = payload["item"]["aggregated_output"]
        if body is None or not _same_output(original, body):
            continue
        payload["item"]["aggregated_output"] = {
            "duplicateOfEventId": result.id,
            "characters": len(original),
            "sha256": sha256(original.encode("utf-8")).hexdigest(),
        }
        compacted = json.dumps(payload, ensure_ascii=False)
        if len(compacted) < len(event.text):
            replacements[event.id] = compacted
    return replacements
