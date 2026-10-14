"""Deterministic tool-log preparation; full events remain in canonical/Raw evidence."""
from __future__ import annotations

import json
import re
from collections import Counter
from hashlib import sha256
from typing import Any

from .media_inputs import describe_embedded_images

PREPARATION_VERSION = "tool-index-v1"
_SIGNAL = re.compile(
    r"(?im)(?:^\s*(?:traceback|error:|exception:|fatal:|Get-Content:|FileNotFoundError:|"
    r"PermissionError:|TimeoutError:)|"
    r"\b(?:validation_errors|error_count|failed_count)\s*[=:]\s*[1-9]\d*|"
    r"\b[1-9]\d* (?:failed|errors)\b|(?:timed out after|HTTP (?:429|5\d\d)))"
)


def _unwrap(value: Any, depth: int = 0) -> tuple[list[str], list[str]]:
    if depth > 8:
        return [str(value)], []
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return [value], []
        if decoded != value:
            return _unwrap(decoded, depth + 1)
        return [value], []
    if isinstance(value, list):
        parts, metadata = [], []
        for item in value:
            text, fields = _unwrap(item, depth + 1)
            parts.extend(text)
            metadata.extend(fields)
        return parts, metadata
    if isinstance(value, dict):
        metadata = [f"{k}={value[k]}" for k in ("exit_code", "wall_time_seconds", "session_id", "isError")
                    if k in value]
        parts = []
        # Decode known transport fields only; ordinary structured output is kept as text.
        for key in ("text", "output", "value", "content"):
            if key in value:
                text, fields = _unwrap(value[key], depth + 1)
                parts.extend(text)
                metadata.extend(fields)
        if not parts:
            parts = [json.dumps(value, ensure_ascii=False)]
        return parts, metadata
    return [str(value)], []


def tool_excerpt(event: dict[str, Any]) -> tuple[str, bool]:
    """Return a diagnostic excerpt and a possible-problem flag, not a failure verdict."""
    text = describe_embedded_images(event.get("text", "")).text
    parts, metadata = _unwrap(text)
    body = "\n".join(parts)
    matches = list(_SIGNAL.finditer(body))
    nonzero = any(re.fullmatch(r"exit_code=-?[1-9]\d*", v) or v == "isError=True" for v in metadata)
    signal = event["kind"] == "tool_result" and (bool(matches) or nonzero)
    if not signal:
        return "", False
    excerpts = [body[max(0, m.start()-100):m.end()+300] for m in matches[:3]]
    if not excerpts or nonzero:
        excerpts.insert(0, body[:450])
    excerpt = "\n...\n".join(dict.fromkeys(excerpts))[:1200]
    return ("Possible tool problem (keyword/transport observation; quoted code may match):\n"
            + "; ".join(metadata[:12]) + "\n" + excerpt
            + "\n[Bounded excerpt; verify cause, counts and recovery in Raw.]"), True


def routine_index(events: list[dict[str, Any]]) -> str:
    calls = [e for e in events if e["kind"] == "tool_call"]
    kinds = Counter(e["kind"] for e in events)
    names = Counter(e.get("name", "unknown") for e in calls)
    repeats = Counter(sha256(e.get("text", "").encode()).hexdigest() for e in calls)
    duplicated = {key: n for key, n in repeats.items() if n > 1}
    return ("Routine tool log index; full bodies omitted from model input, retained in canonical/Raw.\n"
            "Counts are log records, NOT independent executions or VLM calls. No success is inferred from omission.\n"
            + json.dumps({"records": dict(kinds), "callNames": dict(names),
                          "repeatedExactCallTextHashes": dict(list(duplicated.items())[:8]),
                          "additionalRepeatedGroups": max(0, len(duplicated)-8)}, ensure_ascii=False)
            + "\nExact call-text equality does not prove equal image bytes, purpose, or avoidable work. "
            "Request Raw for important results, reuse candidates or validation details.")
