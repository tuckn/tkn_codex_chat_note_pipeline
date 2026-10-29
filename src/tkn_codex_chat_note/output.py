"""CLI-only result projection; saved reports and Python return values stay intact."""

from __future__ import annotations

import json
from collections import Counter
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any

from .config_output import config_lines

# Only these item collections can be summarized. Diagnostics, sources in config,
# report paths and provenance are never removed by a generic list-size limit.
_DETAIL_COLLECTIONS = {"threads", "results", "files"}
_ROUTINE_STATUSES = {"current", "unchanged", "excluded", "skipped-empty"}


def with_target_paths(value: dict[str, Any], roots: dict[str, dict[str, str]]) -> dict[str, Any]:
    """Resolve CLI diagnostic targets from structured identities, never from error prose."""
    def visit(item: Any, source_id: str | None = None) -> Any:
        if isinstance(item, list):
            return [visit(child, source_id) for child in item]
        if not isinstance(item, dict):
            return item
        source_id = item.get("sourceId", source_id)
        source = roots.get(source_id or "", {})
        result = {key: visit(child, source_id) for key, child in item.items()}
        if source and (item.get("error") or item.get("sourceRef") or item.get("sourceRefs")
                       or item.get("threadId") and item.get("reason") or item.get("status") in
                       {"failed", "blocked", "deferred", "pending", "preserved"}):
            refs = item.get("sourceRefs", [])
            if item.get("sourceRef"):
                refs = [*refs, item["sourceRef"]]
            paths = [str(Path(source["sourceRoot"]).joinpath(*PurePosixPath(ref).parts))
                     for ref in refs if isinstance(ref, str) and not PurePosixPath(ref).is_absolute()
                     and ".." not in PurePosixPath(ref).parts]
            if paths:
                result.setdefault("sourcePaths", paths)
            if isinstance(item.get("period"), dict):
                from .review_evidence import review_path
                from .review_model import KINDS, period_for

                period = item["period"]
                if period.get("kind") in KINDS and period.get("start"):
                    target = period_for(period["kind"], date.fromisoformat(period["start"]))
                    path = review_path(Path(source["dataRoot"]) / "reviews", target)
                    result.setdefault("jsonPath", str(path))
                    result.setdefault("notePath", str(path.with_suffix(".md")))
            if not paths and not item.get("period"):
                result.setdefault("sourceRoot", source["sourceRoot"])
        return result

    result: dict[str, Any] = visit(value, next(iter(roots)) if len(roots) == 1 else None)
    return result


def public_result(value: dict[str, Any], *, full_output: bool) -> dict[str, Any]:
    """Keep actionable targets; summarize routine records with a route to details."""
    if full_output:
        return dict(value)

    def project(item: Any, parent: str = "") -> Any:
        if isinstance(item, list):
            return [project(child, parent) for child in item]
        if not isinstance(item, dict):
            return item
        result: dict[str, Any] = {}
        omitted = 0
        for key, child in item.items():
            detail = key in _DETAIL_COLLECTIONS or key == "sources" and parent == "rawIngest"
            if detail and isinstance(child, list):
                counts = Counter(str(entry.get("status", "item")) if isinstance(entry, dict) else "item"
                                 for entry in child)
                result[key + "Count"] = len(child)
                result[key + "Counts"] = dict(counts)
                retained = []
                for entry in child:
                    routine = (key == "files" or isinstance(entry, dict)
                               and entry.get("status") in _ROUTINE_STATUSES
                               and not any(entry.get(field) for field in ("error", "warnings", "gaps")))
                    if routine:
                        omitted += 1
                    else:
                        retained.append(project(entry, key))
                result[key] = retained
            else:
                result[key] = project(child, key)
        if omitted:
            result["omittedDetailsCount"] = omitted
            result["detailsHint"] = (
                "Use --full-output with the same output format for every item; "
                "when a reportPath/reportPaths is present, read the saved report to avoid rerunning."
            )
        return result

    projected: dict[str, Any] = project(value)
    return projected


def emit(value: dict[str, Any], *, json_output: bool) -> None:
    """Write exactly one final result, independent of verbosity or terminal type."""
    if json_output:
        print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print("\n".join(config_lines(value)))
