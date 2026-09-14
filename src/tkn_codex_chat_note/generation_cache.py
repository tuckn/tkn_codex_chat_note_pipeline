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
        self.last_response_model: str | None = None

    def read(self, key: str) -> dict[str, Any] | None:
        self.last_response_model = None
        if not self.reuse:
            return None
        path = self.directory / f"{key}.json"
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            value = envelope["value"]
            model = envelope.get("responseModel")
            hashed = {"value": value, "responseModel": model} if envelope["version"] == 2 else value
            if (envelope["version"] in (1, 2) and envelope["key"] == key
                and isinstance(value, dict) and envelope["sha256"] == content_hash(hashed)):
                self.last_response_model = model
                return value
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    def write(self, key: str, value: dict[str, Any], *, response_model: str | None = None) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / f"{key}.json"
        temporary = self.directory / f".tmp-{uuid4().hex}"
        envelope = {"version": 1, "key": key, "sha256": content_hash(value), "value": value}
        if response_model is not None:
            envelope.update(version=2, responseModel=response_model,
                            sha256=content_hash({"value": value, "responseModel": response_model}))
        try:
            temporary.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
            replace_file(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
