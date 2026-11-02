"""Synthetic tkn-genai-chat-archiver archives (data contract 1.0.0) for read-only tests."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

import zstandard

MARKER = ".tkn-genai-chat-archive.json"


def write_marker(root: Path, source_id: str, **overrides: Any) -> None:
    root.mkdir(parents=True, exist_ok=True)
    document = {
        "schemaVersion": "1.0.0",
        "applicationId": "tkn-genai-chat-archiver",
        "rootKind": "chatLogArchive",
        "profile": "codex",
        "software": "Codex",
        "sourceId": source_id,
        "deviceId": "Example PC",
        "environment": "windows",
        "createdAt": "2026-11-02T03:00:00+09:00",
        "legacyStore": None,
        **overrides,
    }
    (root / MARKER).write_text(json.dumps(document), encoding="utf-8")


def later(days: int = 1) -> datetime:
    """A capture time after anything the old pipeline recorded during the test."""
    return datetime.now().astimezone().replace(microsecond=0) + timedelta(days=days)


def run_id_for(moment: datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%S%z")


@dataclass
class ArchiveRun:
    root: Path
    source_id: str
    run_id: str
    captured_at: str
    records: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def at(cls, root: Path, source_id: str, moment: datetime) -> ArchiveRun:
        return cls(root, source_id, run_id_for(moment), moment.isoformat())

    def version(
        self,
        relative_path: str,
        content: bytes,
        *,
        encoding: str = "identity",
        stored_path: str | None = None,
        captured_at: str | None = None,
        method: str = "copy",
        legacy: dict[str, Any] | None = None,
        write: bool = True,
        content_error: str | None = None,
    ) -> dict[str, Any]:
        stored = zstandard.ZstdCompressor().compress(content) if encoding == "zstd" else content
        stored_path = stored_path or f"snapshots/{self.run_id}/{relative_path}"
        if write:
            target = self.root.joinpath(*stored_path.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(stored)
        description: dict[str, Any] = {
            "encoding": encoding,
            "byteCount": len(content),
            "sha256": sha256(content).hexdigest(),
        }
        if content_error is not None:
            description = {"encoding": encoding, "error": content_error}
        record: dict[str, Any] = {
            "schemaVersion": "1.0.0",
            "recordType": "version",
            "recordId": str(uuid4()),
            "runId": self.run_id,
            "profile": "codex",
            "software": "Codex",
            "sourceId": self.source_id,
            "deviceId": "Example PC",
            "environment": "windows",
            "relativePath": relative_path,
            "storedPath": stored_path,
            "captureMethod": method,
            "capturedAt": captured_at or self.captured_at,
            "byteCount": len(stored),
            "sha256": sha256(stored).hexdigest(),
            "content": description,
            "previous": None,
            "pipeline": {"name": "tkn-genai-chat-archiver", "version": "0.1.0"},
            "result": "imported" if method == "legacy-import" else "stored",
        }
        if legacy is not None:
            record["legacy"] = legacy
        self.records.append(record)
        return record

    def missing(self, relative_path: str) -> None:
        self.records.append({
            "schemaVersion": "1.0.0",
            "recordType": "sourceMissing",
            "runId": self.run_id,
            "relativePath": relative_path,
            "lastRecordId": "",
            "observedAt": self.captured_at,
        })

    def commit(self, *, confirmed: bool = True, partial: bool = False, status: str = "complete") -> Path:
        manifests = self.root / "manifests"
        manifests.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(record) for record in self.records]
        if confirmed:
            lines.append(json.dumps({
                "schemaVersion": "1.0.0",
                "recordType": "run",
                "runId": self.run_id,
                "targetId": f"codex-{self.source_id}",
                "status": status,
                "startedAt": self.captured_at,
                "finishedAt": self.captured_at,
                "counts": {},
                "failed": [],
            }))
        path = manifests / (f"{self.run_id}.jsonl" + (".partial" if partial else ""))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path


def adopt_legacy(root: Path, source_id: str) -> ArchiveRun:
    """Register an old-pipeline Raw store in place, as `legacy adopt` does; no old file changes."""
    manifest = root / "manifest.jsonl"
    write_marker(root, source_id, legacyStore={
        "applicationId": "tkn-genai-chat-note-pipeline",
        "manifestSha256": sha256(manifest.read_bytes()).hexdigest(),
    })
    run = ArchiveRun.at(root, source_id, datetime.now().astimezone().replace(microsecond=0))
    for number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        record = json.loads(line)
        reference = record["sourceRef"]
        run.version(
            reference,
            root.joinpath(*reference.split("/")).read_bytes(),
            stored_path=reference,
            captured_at=record["capturedAt"],
            method="legacy-import",
            write=False,
            legacy={
                "applicationId": "tkn-genai-chat-note-pipeline",
                "manifest": "manifest.jsonl",
                "line": number,
                "captureRef": record["captureRef"],
                "threadId": record.get("threadId"),
                "lastEventAt": record.get("lastEventAt"),
            },
        )
    for snapshot in sorted((root / "metadata").glob("*.json")):
        modified = datetime.fromtimestamp(snapshot.stat().st_mtime).astimezone()
        run.version(
            ".codex-global-state.json",
            snapshot.read_bytes(),
            stored_path=f"metadata/{snapshot.name}",
            captured_at=modified.isoformat(),
            method="legacy-import",
            write=False,
            legacy={"kind": "appStateSnapshot", "storedName": snapshot.name},
        )
    run.commit()
    return run


def tree(root: Path) -> dict[str, tuple[int, int, str]]:
    """Every entry below root with size, modification time and content digest."""
    result: dict[str, tuple[int, int, str]] = {}
    for path in sorted(root.rglob("*")):
        status = path.stat()
        is_file = path.is_file()
        digest = sha256(path.read_bytes()).hexdigest() if is_file else "directory"
        result[path.relative_to(root).as_posix()] = (status.st_size if is_file else -1, status.st_mtime_ns, digest)
    return result
