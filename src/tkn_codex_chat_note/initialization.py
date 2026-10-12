"""Create or transactionally rebuild application-owned pipeline storage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import AppConfig
from .runtime import PipelineError

# Stable storage ownership identifier; retained across the CLI/package rename.
ROOT_OWNERSHIP_MARKER = ".tkn-genai-chat-note-root.json"
ROOT_OWNERSHIP_SCHEMA_VERSION = 1
ROOT_OWNER_APPLICATION_ID = "tkn-genai-chat-note-pipeline"
ROOT_KINDS = ("data", "state", "cache", "raw")
SAFE_RESET_OWNERSHIP_STATUSES = frozenset({"missing", "empty", "owned"})


def _resolved(path: Path) -> Path:
    return path.expanduser().absolute().resolve(strict=False)


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def validate_reset_targets(config: AppConfig, config_path: Path) -> tuple[Path, ...]:
    targets = tuple(
        _resolved(path) for path in (config.data_root, config.state_root, config.source_cache_root, config.raw_root)
    )
    home = _resolved(Path.home())
    codex_home = _resolved(config.codex_home)
    resolved_config = _resolved(config_path)
    for target in targets:
        if target.parent == target or target == home or home.is_relative_to(target):
            raise PipelineError(f"unsafe reset target: {target}")
        if _overlaps(target, codex_home):
            raise PipelineError(f"reset target overlaps Codex home: {target}")
        if resolved_config == target or resolved_config.is_relative_to(target):
            raise PipelineError(f"reset target contains config: {target}")
    for index, left in enumerate(targets):
        for right in targets[index + 1 :]:
            if _overlaps(left, right):
                raise PipelineError(f"reset targets overlap: {left} and {right}")
    return targets


def _ownership_marker_document(kind: str) -> dict[str, str | int]:
    return {
        "schemaVersion": ROOT_OWNERSHIP_SCHEMA_VERSION,
        "applicationId": ROOT_OWNER_APPLICATION_ID,
        "rootKind": kind,
    }


def _inspect_root_ownership(kind: str, path: Path) -> dict[str, Any]:
    marker = path / ROOT_OWNERSHIP_MARKER
    legacy_marker = path / ".tkn-codex-context-root.json"
    legacy = not marker.exists() and legacy_marker.exists()
    if legacy:
        marker = legacy_marker
    report: dict[str, Any] = {
        "kind": kind,
        "path": str(path),
        "exists": path.exists() or path.is_symlink(),
        "markerPath": str(marker),
        "status": "missing",
        "reason": None,
    }
    if not report["exists"]:
        return report
    if not path.is_dir():
        report.update(status="not-directory", reason="configured root is not a directory")
        return report
    if marker.is_symlink():
        report.update(status="invalid-marker", reason="ownership marker must not be a symbolic link")
        return report
    if marker.exists() and not marker.is_file():
        report.update(status="invalid-marker", reason="ownership marker must be a regular file")
        return report
    if not marker.is_file():
        try:
            empty = next(path.iterdir(), None) is None
        except OSError as exc:
            raise PipelineError(f"cannot inspect reset target ownership: {path}: {exc}") from exc
        report.update(
            status="empty" if empty else "unowned",
            reason=None if empty else "ownership marker is missing from a non-empty directory",
        )
        return report
    try:
        value = json.loads(marker.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        report.update(status="invalid-marker", reason=f"cannot read ownership marker: {exc}")
        return report
    expected = _ownership_marker_document(kind)
    if legacy:
        expected["applicationId"] = "tkn-codex-context-pipeline"
    if not isinstance(value, dict) or any(value.get(key) != expected_value for key, expected_value in expected.items()):
        report.update(
            status="invalid-marker",
            reason=("ownership marker does not match this application, root kind, or supported marker schema"),
        )
        return report
    report["status"] = "owned"
    return report


def inspect_reset_target_ownership(targets: tuple[Path, ...]) -> list[dict[str, Any]]:
    return [_inspect_root_ownership(kind, path) for kind, path in zip(ROOT_KINDS, targets, strict=True)]
