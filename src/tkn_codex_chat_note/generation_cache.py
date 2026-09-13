"""Content-addressed, disposable checkpoints for validated inference stages."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from .file_io import replace_file


def content_hash(value: Any) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


class GenerationCache:
    def __init__(self, root: Path, identity: dict[str, Any], *, reuse: bool = True) -> None:
        self.directory = root / "generation" / content_hash(identity)
        self.reuse = reuse

    def read(self, key: str) -> dict[str, Any] | None:
        if not self.reuse:
            return None
        path = self.directory / f"{key}.json"
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            value = envelope["value"]
            if (
                envelope["version"] == 1 and envelope["key"] == key
                and isinstance(value, dict) and envelope["sha256"] == content_hash(value)
            ):
                return value
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    def write(self, key: str, value: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / f"{key}.json"
        temporary = self.directory / f".tmp-{uuid4().hex}"
        envelope = {"version": 1, "key": key, "sha256": content_hash(value), "value": value}
        try:
            temporary.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
            replace_file(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
