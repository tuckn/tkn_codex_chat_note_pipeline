"""Resumable end-to-end clone/pull orchestration over immutable captured evidence."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from .catalog import CATALOG_SCHEMA_VERSION, capture_sources, discover
from .config import AppConfig
from .provenance import ProvenanceStore
from .raw_capture import RawCaptureError
from .runtime import PipelineError, atomic_write_json, now_iso, now_local
from .storage import legacy_storage_pending, pipeline_storage, read_json

Progress = Callable[[dict[str, Any]], None]


def _run_source_pipeline(
    config: AppConfig,
    *,
    mode: str,
    dry_run: bool = False,
    config_path: Path | None = None,
    progress: Progress | None = None,
) -> dict[str, Any]:
    if mode not in {"clone", "pull", "raw"}:
        raise PipelineError(f"unknown pipeline mode: {mode}")
    run_id = str(uuid4())
    started = now_local()
    report: dict[str, Any] = {
        "schemaVersion": 4,
        "sourceProvider": config.source_provider,
        "sourceId": config.source_id,
        "runId": run_id,
        "mode": mode,
        "dryRun": dry_run,
        "startedAt": started.isoformat(),
        "complete": False,
        "ok": False,
        "failed": [],
        "reportPath": None,
        "coverage": {
            "provider": "codex",
            "cloudHistoryFetched": False,
            "includesArchivedDirectory": config.include_archived,
            "internalThreads": "captured-and-normalized-but-excluded-from-reviews",
        },
    }
    with pipeline_storage(config, initialize=mode in {"clone", "raw"}, dry_run=dry_run, config_path=config_path):
        if not dry_run:
            atomic_write_json(config.source_state_root / "last-run.json", {**report, "status": "running"})
        if mode == "raw":
            _inputs, raw_report = capture_sources(config, dry_run=dry_run, captured_at=now_iso())
            report.update(
                rawIngest=raw_report,
                failed=raw_report["failed"],
                ok=not raw_report["failed"],
                threadCounts={},
                threads=[],
                complete=not raw_report["failed"],
                normalizedThreadCount=0,
                finishedAt=now_iso(),
            )
            if not dry_run:
                report["reportPath"] = str(config.reports_root / f"{run_id}.json")
                atomic_write_json(Path(report["reportPath"]), report)
                atomic_write_json(config.source_state_root / "last-run.json", report)
            return report
        provenance = ProvenanceStore(config.data_root, dry_run=dry_run)
        discovery = discover(config, provenance, run_id=run_id)
        report.update(rawIngest=discovery.raw_report, warnings=discovery.warnings)
        report["failed"].extend(discovery.failures)
        report["threads"] = discovery.entries
        report["threadCounts"] = dict(Counter(entry["status"] for entry in discovery.entries))
        report["normalizedThreadCount"] = sum("canonicalRef" in entry for entry in discovery.entries)
        failures = [entry for entry in discovery.entries if entry["status"] in {"failed", "blocked"}]
        report["ok"] = not report["failed"] and not failures
        report["complete"] = bool(
            report["ok"]
            and mode in {"clone", "pull"}
            and not dry_run
            and all(entry["status"] in {"current", "excluded"} for entry in discovery.entries)
        )
        report["finishedAt"] = now_iso()
        if not dry_run:
            atomic_write_json(
                config.data_root / "catalog" / "threads.json",
                {
                    "schemaVersion": CATALOG_SCHEMA_VERSION,
                    "asOf": report["finishedAt"],
                    "threads": [
                        *[
                            entry
                            for entry in read_json(config.data_root / "catalog" / "threads.json").get("threads", [])
                            if (entry.get("sourceProvider"), entry.get("sourceId"))
                            != (config.source_provider, config.source_id)
                        ],
                        *discovery.entries,
                    ],
                },
            )
            artifacts: list[dict[str, Any]] = []
            for entry in discovery.entries:
                if entry["threadKey"] in discovery.evidence:
                    artifacts.append({
                        **discovery.evidence[entry["threadKey"]][-1],
                        "status": entry["status"], "threadKey": entry["threadKey"],
                        "sourceProvider": config.source_provider, "sourceId": config.source_id,
                    })
            provenance.publish_index(
                artifacts,
                run_id=run_id,
                complete=report["complete"],
                source=(config.source_provider, config.source_id),
            )
            report_path = config.reports_root / f"{run_id}.json"
            report["reportPath"] = str(report_path)
            atomic_write_json(report_path, report)
            atomic_write_json(config.source_state_root / "last-run.json", report)
    return report


def _source_status(config: AppConfig) -> dict[str, Any]:
    last = read_json(config.source_state_root / "last-run.json")
    return {
        "initialized": (config.source_state_root / "pipeline.json").is_file(),
        "migrationRequired": legacy_storage_pending(config),
        "sourceProvider": config.source_provider,
        "sourceId": config.source_id,
        "storageRoot": str(config.source_state_root),
        "asOf": last.get("finishedAt"),
        "lastMode": last.get("mode"),
        "complete": last.get("complete", False),
        "threadCounts": last.get("threadCounts", {}),
        "reportPath": last.get("reportPath"),
        "liveSourceScan": False,
    }


def run_pipeline(
    config: AppConfig,
    *,
    mode: str,
    dry_run: bool = False,
    config_path: Path | None = None,
    progress: Progress | None = None,
    source_id: str | None = None,
) -> dict[str, Any]:
    """Capture and normalize enabled sources in map order without inference."""
    if mode not in {"clone", "pull", "raw"}:
        raise PipelineError(f"unknown pipeline mode: {mode}")
    sources = config.enabled_source_configs(source_id)
    # Validate every destination before the first one creates files or calls AI.
    for source in sources:
        with pipeline_storage(source, initialize=mode in {"clone", "raw"}, dry_run=True, config_path=config_path):
            pass
    started = now_local()
    reports: list[dict[str, Any]] = []
    for source in sources:
        identity = {"sourceProvider": source.source_provider, "sourceId": source.source_id}

        def source_progress(event: dict[str, Any], identity: dict[str, str] = identity) -> None:
            if progress is not None:
                progress({**event, **identity})

        source_progress({"type": "source-start"})
        try:
            report = _run_source_pipeline(
                source,
                mode=mode,
                dry_run=dry_run,
                config_path=config_path,
                progress=source_progress,
            )
        except (PipelineError, RawCaptureError, OSError, ValueError) as exc:
            if len(sources) == 1:
                raise
            report = {
                "schemaVersion": 4,
                "runId": str(uuid4()),
                "mode": mode,
                "dryRun": dry_run,
                "startedAt": started.isoformat(),
                "finishedAt": now_iso(),
                **identity,
                "ok": False,
                "complete": False,
                "error": str(exc),
                "failed": [{**identity, "error": str(exc)}],
                "threadCounts": {},
                "threads": [],
                "normalizedThreadCount": 0,
                "reportPath": None,
            }
            if not dry_run and (source.state_root / "pipeline.json").is_file():
                try:
                    with pipeline_storage(source, initialize=False, dry_run=False, config_path=config_path):
                        report_path = source.reports_root / f"{report['runId']}.json"
                        report["reportPath"] = str(report_path)
                        atomic_write_json(report_path, report)
                        atomic_write_json(source.state_root / "last-run.json", report)
                except (PipelineError, OSError, ValueError) as save_error:
                    report["reportPath"] = None
                    report["warnings"] = [f"Could not persist source failure report: {save_error}"]
        reports.append(report)
    if len(reports) == 1:
        return reports[0]
    counts: Counter[str] = Counter()
    for report in reports:
        counts.update(report["threadCounts"])
    return {
        "schemaVersion": 4,
        "mode": mode,
        "dryRun": dry_run,
        "startedAt": started.isoformat(),
        "finishedAt": now_iso(),
        "ok": all(report["ok"] for report in reports),
        "complete": all(report["complete"] for report in reports),
        "sourceResults": reports,
        "threadCounts": dict(counts),
        "normalizedThreadCount": sum(report["normalizedThreadCount"] for report in reports),
        "failed": [
            {**failure, "sourceProvider": report["sourceProvider"], "sourceId": report["sourceId"]}
            for report in reports
            for failure in report["failed"]
        ],
        "warnings": [
            f"{report['sourceProvider']}/{report['sourceId']}: {warning}"
            for report in reports
            for warning in report.get("warnings", [])
        ],
        "reportPaths": [report["reportPath"] for report in reports if report.get("reportPath")],
    }


def pipeline_status(config: AppConfig, *, source_id: str | None = None) -> dict[str, Any]:
    results = [_source_status(source) for source in config.enabled_source_configs(source_id)]
    if len(results) == 1:
        return results[0]
    return {
        "sourceResults": results,
        "liveSourceScan": False,
        "initialized": all(result["initialized"] for result in results),
        "migrationRequired": any(result["migrationRequired"] for result in results),
        "complete": all(result["complete"] for result in results),
    }
