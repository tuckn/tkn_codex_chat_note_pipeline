"""Provider-neutral usage records; never store prompts, answers, or credentials."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

TOKEN_FIELDS = ("inputTokens", "outputTokens", "cachedInputTokens", "reasoningTokens", "cacheWriteTokens")
Observer = Callable[[dict[str, Any]], None]


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


def new_record(provider: str, model: str, effort: str) -> dict[str, Any]:
    return {
        "usageId": str(uuid4()),
        "startedAt": timestamp(),
        "finishedAt": None,
        "provider": provider,
        "requestedModel": model,
        "model": None,
        "reasoningEffort": effort,
        "status": "started",
        "durationSeconds": None,
        "usageSource": "unavailable",
        "usageScope": "invocation",
        "usageComplete": False,
        **dict.fromkeys(TOKEN_FIELDS),
    }


def token_number(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def read_codex_usage(output: str | bytes | None, record: dict[str, Any]) -> None:
    """Only terminal per-turn usage is additive; never sum cumulative token events."""
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    turns: list[dict[str, Any]] = []
    started = 0
    failed = False
    for line in (output or "").splitlines():
        try:
            event = json.loads(line.lstrip("\ufeff"))
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "turn.started":
            started += 1
        elif kind in {"turn.failed", "error"}:
            failed = True
        elif kind == "turn.completed":
            usage = event.get("usage")
            turns.append(usage if isinstance(usage, dict) else {})
    if not turns:
        return
    complete = not failed and started <= len(turns)
    record.update(usageSource="codex.exec.turn.completed", usageScope="invocation-turns", completedTurnCount=len(turns))
    for target, source in zip(
        TOKEN_FIELDS,
        (
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "reasoning_output_tokens",
            "cache_write_input_tokens",
        ),
        strict=True,
    ):
        values = [token_number(turn.get(source)) for turn in turns]
        known = sum(value for value in values if value is not None)
        record[target] = known if complete and all(value is not None for value in values) else None
        record["known" + target[0].upper() + target[1:]] = known
    record["usageComplete"] = complete and all(record[key] is not None for key in TOKEN_FIELDS[:2])


class UsageJournal:
    """Atomically checkpoint each attempt before and after inference under source state."""

    def __init__(self, root: Path, context: dict[str, Any], observer: Observer | None = None) -> None:
        self.root = root
        self.context = context
        self.observer = observer
        self.records: dict[str, dict[str, Any]] = {}

    def __call__(self, event: dict[str, Any]) -> None:
        if event.get("type") in {"usage-start", "usage-complete", "api-request-start", "api-request-complete"}:
            from .session_notes import atomic_write_json

            record = {key: value for key, value in event.items() if key != "type"}
            record.update(self.context, schemaVersion=1)
            usage_id = str(record["usageId"])
            atomic_write_json(self.root / f"{usage_id}.json", record)
            self.records[usage_id] = record
        if self.observer:
            self.observer(event)

    def finish(self, status: str) -> None:
        from .session_notes import atomic_write_json

        for usage_id, record in self.records.items():
            record["noteStatus"] = status
            atomic_write_json(self.root / f"{usage_id}.json", record)
