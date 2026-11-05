"""Shared runtime settings and atomic file utilities; no note generation."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import uuid4

from .file_io import replace_file

DEFAULT_SOURCE_ID = "windows" if os.name == "nt" else "local"
DEFAULT_RUNTIME_MINUTES = 230
DEFAULT_MODEL_TIMEOUT_SECONDS = 1800


class PipelineError(RuntimeError):
    pass


@dataclass(frozen=True)
class InferenceConfig:
    provider: str = "codex"
    generation_profile: str | None = None
    codex_bin: str = "codex"
    claude_bin: str = "claude"
    copilot_bin: str = "copilot"
    ollama_base_url: str = "http://127.0.0.1:11434"
    model: str = "gpt-5.6-sol"
    reasoning_effort: str = "high"
    runtime_minutes: int = DEFAULT_RUNTIME_MINUTES
    model_timeout_seconds: int = DEFAULT_MODEL_TIMEOUT_SECONDS
    inference_options: dict[str, Any] = field(default_factory=dict)


def now_local() -> datetime:
    return datetime.now().astimezone()


def now_iso() -> str:
    return now_local().isoformat(timespec="seconds")


def parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise PipelineError(f"invalid ISO 8601 timestamp: {value}") from exc
    return parsed if parsed.tzinfo else parsed.astimezone()


def source_event_time(value: str) -> datetime | None:
    try:
        return parse_datetime(value) if value else None
    except PipelineError:
        return None


@lru_cache(maxsize=8192)
def path_variants(value: str | Path) -> tuple[str, ...]:
    path = Path(value).expanduser().absolute()
    values = [str(path)]
    try:
        values.append(str(path.resolve(strict=False)))
    except OSError:
        pass
    return tuple(dict.fromkeys(values))


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".tmp-{uuid4().hex[:12]}"
    try:
        temporary.write_bytes(content)
        replace_file(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
