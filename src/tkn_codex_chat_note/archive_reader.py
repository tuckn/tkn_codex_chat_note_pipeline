"""Read-only access to a Raw archive written by tkn-genai-chat-archiver.

The archiver owns every write below an archive root. This module reads only
confirmed manifests, verifies stored bytes before use, and decodes compressed
versions into a directory outside the archive.
"""

from __future__ import annotations

import gzip
import io
import json
import re
import zlib
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any

import zstandard

from .chat_titles import load_chat_titles, parse_chat_titles
from .config import AppConfig
from .raw_capture import ARCHIVE_MARKER, RawCaptureError, RawSourceInput, archive_managed
from .runtime import atomic_write_bytes

ARCHIVE_APPLICATION_ID = "tkn-genai-chat-archiver"
ARCHIVE_ROOT_KIND = "chatLogArchive"
ARCHIVE_PROFILE = "codex"
ARCHIVE_SCHEMA_MAJOR = "1"
APP_STATE_PATH = ".codex-global-state.json"
TITLE_INDEX_PATH = "session_index.jsonl"
CONTENT_DIRECTORY = "archive-content"
COMPRESSION_SUFFIXES = (".zst", ".gz")
_CHAT_PATH = re.compile(r"(?:sessions|archived_sessions)/.+\.jsonl(?:\.zst|\.gz)?")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ENCODINGS = frozenset({"identity", "zstd", "gzip"})
_COMPLETE_RUN_STATUSES = frozenset({"complete", "recovered"})


class ArchiveError(RawCaptureError):
    """The archive cannot be read safely; nothing below its root is changed."""


@dataclass(frozen=True)
class ArchiveVersion:
    """One stored version from a confirmed manifest."""

    record_id: str
    run_id: str
    relative_path: str
    stored_path: str
    captured_at: datetime
    byte_count: int
    sha256: str
    encoding: str | None
    content_byte_count: int | None
    content_sha256: str | None
    content_error: str | None
    legacy: dict[str, Any]
    sequence: int


@dataclass
class ArchiveIndex:
    root: Path
    source_id: str
    versions: dict[str, list[ArchiveVersion]] = field(default_factory=dict)
    missing: dict[str, int] = field(default_factory=dict)
    manifest_count: int = 0
    ignored: list[dict[str, str]] = field(default_factory=list)
    last_run: dict[str, Any] = field(default_factory=dict)

    def latest(self, relative_path: str) -> ArchiveVersion | None:
        versions = self.versions.get(relative_path)
        return versions[-1] if versions else None

    def present(self, relative_path: str) -> bool:
        """Whether the original still existed when the archiver last looked."""
        last = max((version.sequence for version in self.versions.get(relative_path, [])), default=-1)
        return self.missing.get(relative_path, -1) < last


def logical_path(relative_path: str) -> str:
    """The uncompressed name; Codex replaces x.jsonl with x.jsonl.zst."""
    for suffix in COMPRESSION_SUFFIXES:
        if relative_path.endswith(suffix):
            return relative_path[: -len(suffix)]
    return relative_path


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA256.fullmatch(value))


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _relative(value: Any, where: str, name: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or value.startswith("/"):
        raise ArchiveError(f"unsafe {name} in archive manifest: {where}")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ArchiveError(f"unsafe {name} in archive manifest: {where}")
    return value


def _captured_at(value: Any, where: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ArchiveError(f"invalid capturedAt in archive manifest: {where}") from exc
    if parsed.tzinfo is None:
        raise ArchiveError(f"capturedAt has no time zone in archive manifest: {where}")
    return parsed


def validate_archive_marker(raw_root: Path, source_id: str) -> dict[str, Any]:
    """Check that the archive belongs to this Codex source before reading it."""
    marker = raw_root / ARCHIVE_MARKER
    if marker.is_symlink() or not marker.is_file():
        raise ArchiveError(f"archive marker must be a regular file: {marker}")
    try:
        value = json.loads(marker.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ArchiveError(f"cannot read archive marker: {marker}: {exc}") from exc
    if not isinstance(value, dict):
        raise ArchiveError(f"archive marker must be a JSON object: {marker}")
    if str(value.get("schemaVersion") or "").split(".")[0] != ARCHIVE_SCHEMA_MAJOR:
        raise ArchiveError(f"unsupported archive marker schemaVersion: {marker}")
    expected = {
        "applicationId": ARCHIVE_APPLICATION_ID,
        "rootKind": ARCHIVE_ROOT_KIND,
        "profile": ARCHIVE_PROFILE,
        "sourceId": source_id,
    }
    mismatched = sorted(key for key, expected_value in expected.items() if value.get(key) != expected_value)
    if mismatched:
        raise ArchiveError(f"archive marker does not match this source ({', '.join(mismatched)}): {marker}")
    return value


def _confirmed_records(path: Path) -> list[tuple[int, dict[str, Any]]] | None:
    """Records of a confirmed manifest, or None when its last line is not a run record."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ArchiveError(f"cannot read archive manifest: {path}: {exc}") from exc
    parsed: list[tuple[int, Any]] = []
    # JSONL uses physical newlines, not Unicode string separators.
    for number, line in enumerate(text.split("\n"), 1):
        if not line.strip():
            continue
        try:
            parsed.append((number, json.loads(line)))
        except ValueError:
            parsed.append((number, None))
    if not parsed or not isinstance(parsed[-1][1], dict) or parsed[-1][1].get("recordType") != "run":
        return None
    records: list[tuple[int, dict[str, Any]]] = []
    for number, value in parsed:
        if not isinstance(value, dict):
            raise ArchiveError(f"invalid record in confirmed archive manifest: {path.name}:{number}")
        records.append((number, value))
    return records


def _version(record: dict[str, Any], source_id: str, sequence: int, where: str) -> ArchiveVersion:
    if record.get("sourceId") != source_id or record.get("profile") != ARCHIVE_PROFILE:
        raise ArchiveError(f"archive manifest record belongs to another source or profile: {where}")
    digest, byte_count = record.get("sha256"), record.get("byteCount")
    if not _is_sha256(digest) or not _is_count(byte_count):
        raise ArchiveError(f"invalid stored size or SHA-256 in archive manifest: {where}")
    content = record.get("content")
    if not isinstance(content, dict):
        raise ArchiveError(f"archive manifest record has no content description: {where}")
    error = content.get("error")
    encoding, content_sha256, content_bytes = content.get("encoding"), content.get("sha256"), content.get("byteCount")
    if error is None:
        if encoding not in _ENCODINGS or not _is_sha256(content_sha256) or not _is_count(content_bytes):
            raise ArchiveError(f"invalid decoded size or SHA-256 in archive manifest: {where}")
        if encoding == "identity" and (content_sha256 != digest or content_bytes != byte_count):
            raise ArchiveError(f"uncompressed content differs from stored bytes in archive manifest: {where}")
    run_id = record.get("runId")
    if not isinstance(run_id, str) or not run_id:
        raise ArchiveError(f"archive manifest record has no runId: {where}")
    legacy = record.get("legacy")
    return ArchiveVersion(
        record_id=str(record.get("recordId") or ""),
        run_id=run_id,
        relative_path=_relative(record.get("relativePath"), where, "relativePath"),
        stored_path=_relative(record.get("storedPath"), where, "storedPath"),
        captured_at=_captured_at(record.get("capturedAt"), where),
        byte_count=int(record["byteCount"]),
        sha256=str(digest),
        encoding=str(encoding) if error is None else None,
        content_byte_count=int(content["byteCount"]) if error is None else None,
        content_sha256=str(content_sha256) if error is None else None,
        content_error=None if error is None else str(error),
        legacy=legacy if isinstance(legacy, dict) else {},
        sequence=sequence,
    )


def load_archive_index(raw_root: Path, source_id: str) -> ArchiveIndex:
    """Index the versions of every confirmed manifest; partial manifests are ignored."""
    root = raw_root.expanduser().absolute()
    validate_archive_marker(root, source_id)
    index = ArchiveIndex(root, source_id)
    manifests = root / "manifests"
    if manifests.is_symlink() or (manifests.exists() and not manifests.is_dir()):
        raise ArchiveError(f"archive manifests must be an ordinary directory: {manifests}")
    if not manifests.is_dir():
        return index
    index.ignored.extend(
        {"manifest": path.name, "reason": "partial"} for path in sorted(manifests.glob("*.jsonl.partial"))
    )
    sequence = 0
    for path in sorted(manifests.glob("*.jsonl")):
        if path.is_symlink() or not path.is_file():
            raise ArchiveError(f"archive manifest must be a regular file: {path}")
        records = _confirmed_records(path)
        if records is None:
            index.ignored.append({"manifest": path.name, "reason": "unconfirmed"})
            continue
        index.manifest_count += 1
        index.last_run = records[-1][1]
        for number, record in records:
            sequence += 1
            where = f"{path.name}:{number}"
            if str(record.get("schemaVersion") or "").split(".")[0] != ARCHIVE_SCHEMA_MAJOR:
                raise ArchiveError(f"unsupported archive manifest schemaVersion: {where}")
            kind = record.get("recordType")
            if kind == "version":
                version = _version(record, source_id, sequence, where)
                index.versions.setdefault(version.relative_path, []).append(version)
            elif kind == "sourceMissing":
                index.missing[_relative(record.get("relativePath"), where, "relativePath")] = sequence
    for versions in index.versions.values():
        versions.sort(key=lambda version: (version.captured_at, version.run_id))
    return index


def decode_content(data: bytes, encoding: str, where: str) -> bytes:
    try:
        if encoding == "identity":
            return data
        if encoding == "gzip":
            return gzip.decompress(data)
        if encoding == "zstd":
            chunks: list[bytes] = []
            with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data), read_across_frames=True) as reader:
                while chunk := reader.read(1 << 20):
                    chunks.append(chunk)
            return b"".join(chunks)
    except (zstandard.ZstdError, OSError, EOFError, zlib.error) as exc:
        raise ArchiveError(f"cannot decode archived version: {where}: {exc}") from exc
    raise ArchiveError(f"unsupported content encoding {encoding!r}: {where}")


def _matches(content: bytes, version: ArchiveVersion) -> bool:
    return len(content) == version.content_byte_count and sha256(content).hexdigest() == version.content_sha256


class ArchiveContent:
    """Verified access to archived versions; decoded copies live outside the archive."""

    def __init__(self, index: ArchiveIndex, directory: Path | None) -> None:
        self.index = index
        self.directory = directory
        self.used: set[Path] = set()

    def stored_file(self, version: ArchiveVersion) -> Path:
        path = self.index.root.joinpath(*version.stored_path.split("/"))
        for parent in (path, *path.parents):
            if parent == self.index.root:
                break
            if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
                raise ArchiveError(f"archived version path must not contain links: {version.stored_path}")
        return path

    def _verified(self, version: ArchiveVersion) -> tuple[Path, bytes]:
        path = self.stored_file(version)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ArchiveError(f"archived version is unreadable: {version.stored_path}: {exc}") from exc
        if len(data) != version.byte_count or sha256(data).hexdigest() != version.sha256:
            raise ArchiveError(f"archived version does not match its manifest: {version.stored_path}")
        if version.content_error is not None or version.encoding is None:
            raise ArchiveError(
                f"the archiver could not decode this version: {version.stored_path}: {version.content_error}"
            )
        return path, data

    def content(self, version: ArchiveVersion) -> bytes:
        """The verified decoded bytes of a version."""
        _path, data = self._verified(version)
        assert version.encoding is not None
        content = decode_content(data, version.encoding, version.stored_path)
        if not _matches(content, version):
            raise ArchiveError(f"decoded content does not match its manifest: {version.stored_path}")
        return content

    def path(self, version: ArchiveVersion) -> Path:
        """A file whose bytes are the verified decoded content of a version."""
        stored, data = self._verified(version)
        if version.encoding == "identity":
            return stored
        if self.directory is None:
            raise ArchiveError(f"no directory for decoded archive content: {version.stored_path}")
        assert version.encoding is not None and version.content_sha256 is not None
        suffix = PurePosixPath(logical_path(version.relative_path)).suffix
        target = self.directory / version.content_sha256[:2] / f"{version.content_sha256}{suffix}"
        self.used.add(target)
        if target.is_file() and not target.is_symlink() and _matches(target.read_bytes(), version):
            return target
        content = decode_content(data, version.encoding, version.stored_path)
        if not _matches(content, version):
            raise ArchiveError(f"decoded content does not match its manifest: {version.stored_path}")
        atomic_write_bytes(target, content)
        return target

    def prune(self) -> int:
        """Remove decoded copies that no current input uses; the directory is a cache."""
        if self.directory is None or not self.directory.is_dir():
            return 0
        removed = 0
        for path in self.directory.glob("*/*"):
            name = path.name.split(".", 1)[0]
            if (
                path in self.used
                or path.is_symlink()
                or not path.is_file()
                or not _SHA256.fullmatch(name)
                or path.parent.name != name[:2]
            ):
                continue
            path.unlink()
            removed += 1
        return removed


def open_archive(config: AppConfig, *, dry_run: bool, stack: ExitStack) -> ArchiveContent | None:
    """Open raw_root as an archive when the archiver owns it; dry runs decode into a temporary folder."""
    if not archive_managed(config.raw_root):
        return None
    index = load_archive_index(config.raw_root, config.source_id)
    if dry_run:
        directory = Path(stack.enter_context(TemporaryDirectory(prefix="tkn-codex-archive-")))
    else:
        directory = config.source_cache_root / CONTENT_DIRECTORY
    return ArchiveContent(index, directory)


def chat_source_inputs(
    archive: ArchiveContent, *, include_archived: bool, dry_run: bool
) -> tuple[list[RawSourceInput], dict[str, Any]]:
    """Latest archived version of every chat path, with compressed aliases of the same content merged."""
    index = archive.index
    groups: dict[str, list[ArchiveVersion]] = {}
    for relative, versions in index.versions.items():
        if not _CHAT_PATH.fullmatch(relative):
            continue
        if relative.startswith("archived_sessions/") and not include_archived:
            continue
        groups.setdefault(logical_path(relative), []).append(versions[-1])
    inputs: list[RawSourceInput] = []
    details: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    alias_count = 0
    for _logical, latest in sorted(groups.items()):
        buckets: dict[str, list[ArchiveVersion]] = {}
        for version in latest:
            buckets.setdefault(version.content_sha256 or "undecodable:" + version.relative_path, []).append(version)
        for bucket in buckets.values():
            # The first capture of unchanged content keeps its reference after Codex compresses it.
            bucket.sort(key=lambda version: (version.captured_at, version.run_id, version.relative_path))
            present = any(index.present(version.relative_path) for version in bucket)
            errors: list[str] = []
            for candidate in bucket:
                try:
                    path = archive.path(candidate)
                except (OSError, ArchiveError) as exc:
                    errors.append(str(exc))
                    continue
                aliases = [version.relative_path for version in bucket if version is not candidate]
                alias_count += len(aliases)
                item = RawSourceInput(
                    source_path=path,
                    source_ref=candidate.relative_path,
                    capture_ref=f"raw:/codex/{index.source_id}/{candidate.stored_path}",
                    capture_sha256=str(candidate.content_sha256),
                    byte_count=int(candidate.content_byte_count or 0),
                    thread_id=str(candidate.legacy.get("threadId") or ""),
                    last_event_at=str(candidate.legacy.get("lastEventAt") or ""),
                    original_present=present,
                )
                inputs.append(item)
                details.append(
                    {
                        "sourceRef": item.source_ref,
                        "captureRef": item.capture_ref,
                        "captureSha256": item.capture_sha256,
                        "byteCount": item.byte_count,
                        "threadId": item.thread_id or None,
                        "lastEventAt": item.last_event_at or None,
                        "originalPresent": item.original_present,
                        "archiveRecordId": candidate.record_id or None,
                        "aliasRefs": aliases,
                        "status": "archived",
                    }
                )
                break
            else:
                failed.append(
                    {
                        "sourceRef": bucket[0].relative_path,
                        "sourcePath": str(archive.stored_file(bucket[0])),
                        "error": "; ".join(errors),
                    }
                )
    if index.manifest_count == 0:
        failed.append(
            {
                "reason": "archive-empty",
                "error": "the Raw archive has no confirmed manifest; run `tkn-genai-chat-archiver capture` first",
            }
        )
    inputs.sort(key=lambda item: item.source_ref)
    details.sort(key=lambda detail: str(detail["sourceRef"]))
    chat_paths = [version.relative_path for versions in groups.values() for version in versions]
    report = {
        "schemaVersion": 1,
        "mode": "archive-read",
        "dryRun": dry_run,
        "sourceId": index.source_id,
        "sourceProvider": "codex",
        "rawRoot": str(index.root),
        "manifestPath": str(index.root / "manifests"),
        "discoveredCount": sum(index.present(relative) for relative in chat_paths),
        "availableCaptureCount": len(inputs),
        "capturedCount": 0,
        "plannedCaptureCount": 0,
        "unchangedCount": 0,
        "bronzeOnlyCount": sum(not item.original_present for item in inputs),
        "blobCreatedCount": 0,
        "failed": failed,
        "sources": details,
        "archive": {
            "manifestCount": index.manifest_count,
            "ignoredManifests": index.ignored,
            "chatPathCount": len(chat_paths),
            "aliasCount": alias_count,
            "lastRunId": index.last_run.get("runId"),
            "lastRunStatus": index.last_run.get("status"),
            "lastRunFinishedAt": index.last_run.get("finishedAt"),
        },
    }
    return inputs, report


def archive_warnings(archive: ArchiveContent) -> list[str]:
    status = archive.index.last_run.get("status")
    if archive.index.manifest_count and status not in _COMPLETE_RUN_STATUSES:
        return [
            f"Latest Raw archive run {archive.index.last_run.get('runId')} ended with status {status!r}; "
            "some recent files may not be archived yet"
        ]
    return []


def source_chat_titles(config: AppConfig, archive: ArchiveContent | None = None) -> dict[str, str]:
    """Display titles from the archived session index, else from the live Codex home."""
    try:
        if archive is None and archive_managed(config.raw_root):
            archive = ArchiveContent(load_archive_index(config.raw_root, config.source_id), None)
        version = archive.index.latest(TITLE_INDEX_PATH) if archive is not None else None
        if archive is not None and version is not None:
            return parse_chat_titles(archive.content(version).decode("utf-8-sig").split("\n"))
    except (OSError, UnicodeError, RawCaptureError):
        pass  # Titles are optional display metadata.
    return load_chat_titles(config.codex_home / TITLE_INDEX_PATH)
