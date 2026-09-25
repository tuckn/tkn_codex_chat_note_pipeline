"""Provider-neutral usage records; never store prompts, answers, or credentials."""

from __future__ import annotations

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
