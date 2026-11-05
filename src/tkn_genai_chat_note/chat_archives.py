"""Find conversation logs in tkn-genai-chat-archiver archives (read only).

The archive format belongs to tkn_genai_chat_archiver (`ArchiveView`); the log formats belong to
tkn_genai_chat_search (`library`). This module only chooses which archived files are
conversations and which of their versions to read.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tkn_genai_chat_archiver.view import ArchiveError, ArchiveVersion, ArchiveView
from tkn_genai_chat_search.library import (
    Title,
    claude_code_transcript_kind,
    codex_rollout_status,
    read_codex_titles,
)

from .config import AppConfig, validate_source_id
from .runtime import PipelineError

LOGGER = logging.getLogger(__name__)
SUPPORTED_PROFILES = ("codex", "claude-code")


@dataclass(frozen=True)
class LogFile:
    """One conversation file of an archive and the version to read."""

    archive: str
    app: str
    source_id: str
    relative_path: str
    kind: str  # main, subagent (Claude Code file layout) or rollout (Codex; the content decides)
    version: ArchiveVersion

    @property
    def modified_at(self) -> datetime | None:
        stat = self.version.record.get("sourceStat")
        value = stat.get("modifiedAt") if isinstance(stat, dict) else None
        return _parse(value) or _parse(self.version.captured_at)


@dataclass
class OpenArchive:
    name: str
    view: ArchiveView
    codex_titles: dict[str, list[Title]] = field(default_factory=dict)

    @property
    def app(self) -> str:
        return str(self.view.profile)


@dataclass
class ArchiveScan:
    archives: list[OpenArchive] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


def open_archives(config: AppConfig) -> ArchiveScan:
    """Open every enabled archive; an unreadable or unsupported one is reported, not fatal."""
    if not config.archives:
        raise PipelineError("no archives are configured; list tkn-genai-chat-archiver archive roots under archives")
    scan = ArchiveScan()
    for name, settings in config.archives.items():
        if not settings.enabled:
            scan.skipped.append({"archive": name, "reason": "disabled"})
            continue
        try:
            view = ArchiveView(settings.root)
        except ArchiveError as exc:
            LOGGER.warning("Archive %s is not readable: %s", name, exc)
            scan.skipped.append({"archive": name, "reason": "unreadable", "detail": str(exc)})
            continue
        if view.profile not in SUPPORTED_PROFILES:
            scan.skipped.append({"archive": name, "reason": "unsupported-profile", "profile": view.profile})
            continue
        try:
            # The marker's source ID names cache folders, so it must be one safe path component.
            validate_source_id(view.source_id)
        except ValueError as exc:
            LOGGER.warning("Archive %s has an unsafe source ID: %s", name, exc)
            scan.skipped.append({"archive": name, "reason": "unsafe-source-id", "sourceId": view.source_id})
            continue
        archive = OpenArchive(name, view)
        if view.profile == "codex":
            index = view.latest("session_index.jsonl")
            if index is not None:
                try:
                    archive.codex_titles, _store = read_codex_titles(view.read_content(index))
                except ArchiveError as exc:
                    LOGGER.warning("Archive %s: cannot read session titles: %s", name, exc)
        if view.ignored_manifests:
            LOGGER.info("Archive %s: %s unfinished manifests ignored", name, len(view.ignored_manifests))
        scan.archives.append(archive)
    return scan


def conversation_files(archive: OpenArchive, *, modified_since: datetime | None = None) -> list[LogFile]:
    """Latest version of each conversation file, optionally only files changed since a time."""
    view = archive.view
    files: list[LogFile] = []
    prefixes = ("sessions/", "archived_sessions/") if view.profile == "codex" else ("projects/",)
    for prefix in prefixes:
        for relative_path in view.relative_paths(prefix):
            if view.profile == "codex":
                kind = "rollout" if codex_rollout_status(relative_path) else None
            else:
                kind = claude_code_transcript_kind(relative_path)
            version = view.latest(relative_path) if kind else None
            if version is None or kind is None:
                continue
            item = LogFile(archive.name, view.profile, view.source_id, relative_path, kind, version)
            modified = item.modified_at
            if modified_since is not None and modified is not None and modified < modified_since:
                continue
            files.append(item)
    return _without_same_content(files)


def _without_same_content(files: list[LogFile]) -> list[LogFile]:
    """`x.jsonl` and Codex's compressed `x.jsonl.zst` with the same content are one conversation."""
    seen: dict[tuple[str, str], LogFile] = {}
    for item in sorted(files, key=lambda f: (f.version.captured_at, f.version.run_id, f.relative_path)):
        seen.setdefault((item.archive, item.version.identity_sha256), item)
    return sorted(seen.values(), key=lambda f: (f.archive, f.relative_path))
