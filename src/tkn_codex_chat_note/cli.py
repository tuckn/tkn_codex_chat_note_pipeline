"""Command-line interface for Tkn Codex Chat Note Pipeline."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from . import __version__
from .config import (
    config_document,
    initialize_user_config,
    load_app_config,
    resolve_app_config,
)
from .config_output import config_lines
from .console_logging import ColorFormatter, ConsoleFilter, log_success, supports_color
from .raw_capture import RawCaptureError
from .runtime import PipelineError

LOGGER = logging.getLogger("tkn_codex_chat_note")


def _utf8_console() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def _add_runtime_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, help="Explicit YAML config path")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--profile", help="Named generation profile (generation.active_profile)")
    selection.add_argument(
        "--provider",
        choices=("codex", "claude-code", "github-copilot", "antigravity", "ollama", "azure-openai"),
        help="Compatibility selector: requires a unique profile for this provider; prefer --profile",
    )
    parser.add_argument("--source", help="Select one enabled source_id from sources")
    parser.add_argument("--model")
    parser.add_argument(
        "--reasoning-effort",
        choices=("low", "medium", "high", "xhigh", "max", "ultra"),
    )
    parser.add_argument("--runtime-minutes", type=int)
    parser.add_argument("--model-timeout-seconds", type=int)
    parser.add_argument("--codex-executable")
    parser.add_argument("--claude-executable")
    parser.add_argument("--copilot-executable")
    parser.add_argument("--ollama-base-url")
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress logs; errors are still shown",
    )
    output.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed diagnostic logs",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tkn-codex-chat-note",
        description="Preserve Codex Raw, normalize events, and generate weekly, monthly and yearly reviews.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    _add_runtime_options(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    config = commands.add_parser("config", help="Create or inspect configuration")
    sub = config.add_subparsers(dest="config_command", required=True)
    init = sub.add_parser("init", help="Create config.yaml; protect existing edits")
    init.add_argument("--force", action="store_true", help="Back up and replace an edited config")
    listing = sub.add_parser(
        "list",
        help="List effective values and their sources",
        description="Read-only configuration listing as key=value lines; does not write files.",
    )
    listing.add_argument("--json", action="store_true", help="Print the full configuration report as JSON")
    for name, help_text in (
        ("clone", "Initialize and process all available history; resume safely when repeated"),
        ("pull", "Capture new/changed logs and refresh canonical events without inference"),
    ):
        command = commands.add_parser(
            name, help=help_text, description=help_text + ". Writes by default; no inference."
        )
        command.add_argument("--dry-run", action="store_true", help="Inspect without writes or inference")
        command.add_argument("--full-output", action="store_true", help="Print the full run result")
    usage_report = commands.add_parser("build-report", help="Build an offline token usage and reference-cost report")
    usage_report.add_argument("--dry-run", action="store_true", help="Aggregate without writing or opening")
    usage_report.add_argument("--no-open", action="store_true", help="Write the report without opening a browser")
    scheduled = commands.add_parser("scheduled-run", help="Capture Raw and build reviews when calendar periods close")
    scheduled.add_argument("--dry-run", action="store_true", help="Plan without writes or inference")
    scheduled.add_argument("--full-output", action="store_true", help="Print the complete JSON run result")
    reviews = commands.add_parser("review-notes", help="Build source-cited weekly, monthly and yearly reviews")
    review_sub = reviews.add_subparsers(dest="review_command", required=True)
    review_build = review_sub.add_parser("build", help="Build a closed calendar period (default: latest closed)")
    _add_build_options(review_build)
    review_build.add_argument("--period", choices=("weekly", "monthly", "yearly"), required=True)
    review_build.add_argument("--date", type=date.fromisoformat, help="A date inside the requested closed period")
    review_due = review_sub.add_parser(
        "due", help="Catch up closed periods since sources.<id>.reviews.start_date or first run")
    review_due.add_argument("--all-history", action="store_true",
                            help="Start at the earliest saved event, ignoring configured start_date and first run")
    _add_build_options(review_due)
    review_due.add_argument("--as-of", type=date.fromisoformat, help="Calendar date to evaluate (default: today)")
    review_validate = review_sub.add_parser("validate", help="Validate review JSON, periods and source IDs offline")
    review_validate.add_argument("artifact", type=Path)
    storage = commands.add_parser("storage", help="Inspect or migrate storage layout")
    migrate = storage.add_subparsers(dest="storage_command", required=True).add_parser(
        "migrate", help="Copy one source store into fresh provider-local roots; retain original evidence"
    )
    migrate.add_argument(
        "--from-config",
        type=Path,
        required=True,
        help="Standalone source config (schema 2-8); --config selects the fresh destination",
    )
    migrate.add_argument("--dry-run", action="store_true", help="Show exact migration files without writing")
    commands.add_parser("status", help="Show last-run coverage and pending work without a live source scan")
    provenance = commands.add_parser("provenance", help="Inspect downstream evidence integrity")
    provenance.add_subparsers(dest="provenance_command", required=True).add_parser(
        "validate",
        help="Read-only verification of artifact identities, snapshots, and activity relations",
    )
    raw = commands.add_parser("raw", help="Capture source bytes independently of inference")
    ingest = raw.add_subparsers(dest="raw_command", required=True).add_parser(
        "ingest", help="Write source copies and manifests (writes by default)"
    )
    ingest.add_argument("--dry-run", action="store_true", help="Inspect without writing")
    ingest.add_argument("--full-output", action="store_true", help="Explicitly print the full JSON report to stdout")
    return parser


def _add_build_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="Validate and plan without inference or file writes")
    parser.add_argument(
        "--force", action="store_true", help="Re-evaluate unchanged inputs; does not override reviewed or edited files"
    )
    parser.add_argument("--allow-edited", action="store_true", help="Explicitly replace edited, unreviewed outputs")
    parser.add_argument(
        "--full-output", action="store_true",
        help="Explicitly print the full JSON report to stdout (default: summary and saved report paths)"
    )


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    names = (
        "profile",
        "provider",
        "model",
        "reasoning_effort",
        "runtime_minutes",
        "model_timeout_seconds",
        "codex_executable",
        "claude_executable",
        "copilot_executable",
        "ollama_base_url",
    )
    result = {name: getattr(args, name) for name in names if getattr(args, name, None) is not None}
    return result


def _configure_logging(args: argparse.Namespace) -> None:
    level = logging.DEBUG if args.verbose else logging.ERROR if args.quiet else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    handler.addFilter(ConsoleFilter())
    handler.setFormatter(
        ColorFormatter(
            "[%(levelname)s] %(message)s",
            use_color=supports_color(sys.stderr),
        )
    )
    logging.basicConfig(level=level, handlers=[handler], force=True)


def _emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _usage_summary(value: dict[str, Any]) -> str:
    parts = []
    for key, label in (("inputTokens", "input"), ("outputTokens", "output")):
        number = value.get(key)
        parts.append(f"{label} {number:,} tokens" if isinstance(number, int) else f"{label} tokens unknown")
    cost = value.get("estimatedCostJpy")
    if isinstance(cost, int | float):
        parts.append(f"estimated JPY {cost:.2f}")
    elif value.get("requestCount", 1):
        parts.append("cost unknown")
    return ", ".join(parts)


def _progress(event: dict[str, Any]) -> None:
    if event.get("type") == "source-start":
        LOGGER.info("Source: %s", event.get("sourceId", ""))


def main(argv: Sequence[str] | None = None) -> int:
    _utf8_console()
    args = build_parser().parse_args(argv)
    _configure_logging(args)
    try:
        if args.command == "config":
            if args.config_command == "init":
                if _overrides(args):
                    raise PipelineError(
                        "runtime configuration options cannot be used with config init; "
                        "create the file first, then edit it or use the options at runtime"
                    )
                LOGGER.info("Initializing user configuration")
                report = initialize_user_config(args.config, force=args.force)
                if report["status"] == "unchanged":
                    LOGGER.info("Configuration unchanged: %s", report["configPath"])
                else:
                    log_success(
                        LOGGER,
                        "Configuration %s: %s",
                        report["status"],
                        report["configPath"],
                    )
                if report["backupPath"]:
                    LOGGER.info("Configuration backup: %s", report["backupPath"])
                _emit({"command": "config init", **report})
                return 0
            LOGGER.info("Showing resolved configuration")
            resolution = resolve_app_config(
                explicit_path=args.config,
                overrides=_overrides(args),
            )
            resolved = resolution.config
            report = {
                "command": "config list",
                "config": config_document(resolved),
                "generationResolved": {
                    "profile": resolved.generation.active_profile,
                    "provider": resolved.provider,
                    "model": resolved.model,
                    "reasoningEffort": resolved.reasoning_effort,
                    "inferenceOptions": resolved.active_provider_config.inference_options(),
                },
                "storage": {
                    "layoutVersion": 5,
                    "sourceRoots": {
                        source_id: {
                            **{
                                kind: str(path)
                                for kind, path in resolved.source_storage_paths(source_id).items()
                            },
                            "catalog": str(resolved.source_storage_paths(source_id)["data"] / "catalog"),
                            "provenance": str(resolved.source_storage_paths(source_id)["data"] / "provenance"),
                        }
                        for source_id in resolved.sources
                    },
                },
                "configSchema": {
                    "effectiveVersion": resolution.effective_schema_version,
                    "hasInMemoryMigrations": resolution.has_in_memory_migrations,
                },
                "sources": resolution.sources,
                "layers": list(resolution.layers),
            }
            if args.json:
                _emit(report)
            else:
                print("\n".join(config_lines(report)))
            return 0

        from .pipeline import pipeline_status, run_pipeline

        config = load_app_config(explicit_path=args.config, overrides=_overrides(args))
        if args.source is not None:
            selected = config.enabled_source_configs(args.source)
            config = selected[0]
        if args.command in {"review-notes", "scheduled-run"}:
            from .reviews import run_reviews, run_scheduled, validate_review

            if args.command == "review-notes" and args.review_command == "validate":
                _emit(validate_review(args.artifact.expanduser().absolute()))
                return 0
            if args.command == "scheduled-run":
                result = run_scheduled(config, dry_run=args.dry_run, config_path=args.config)
            else:
                result = run_reviews(config, kind=getattr(args, "period", None), day=getattr(args, "date", None),
                                     as_of=getattr(args, "as_of", None), dry_run=args.dry_run,
                                     force=args.force, allow_edited=args.allow_edited,
                                     all_history=getattr(args, "all_history", False))
            if args.full_output:
                _emit(result)
            review_result = result.get("reviews", result)
            for entry in review_result.get("results", []):
                LOGGER.info("%s %s: %s%s", entry["period"]["kind"], entry["period"]["key"], entry["status"],
                            "; " + entry["reason"] if entry.get("reason") else "")
                if entry.get("notePath") and entry["status"] in {"generated", "rendered", "planned"}:
                    LOGGER.info("Review note: %s", entry["notePath"])
                if entry.get("gaps"):
                    LOGGER.warning("Input limitations: %s; see the review or full run report", len(entry["gaps"]))
            LOGGER.info("Review notes: generated %s; rendered %s; planned %s; model calls %s; cache hits %s",
                        review_result.get("generatedCount", 0), review_result.get("renderedCount", 0),
                        review_result.get("plannedCount", 0),
                        review_result.get("modelCalls", 0), review_result.get("cacheHits", 0))
            if review_result.get("reportPath"):
                LOGGER.info("Run report: %s", review_result["reportPath"])
            for stage in ("capture",):
                stage_result = result.get(stage, {})
                for path in stage_result.get("reportPaths", []):
                    LOGGER.info("%s report: %s", stage, path)
                if stage_result.get("reportPath"):
                    LOGGER.info("%s report: %s", stage, stage_result["reportPath"])
            if not result["ok"]:
                LOGGER.error("Some processing failed or was protected; inspect the run report")
            return 1 if not result["ok"] else 0 if result["complete"] else 2
        if args.command == "build-report":
            from .usage_report import build_usage_report

            result = build_usage_report(config, dry_run=args.dry_run, no_open=args.no_open)
            _emit(result)
            log_success(LOGGER, "Usage report: %s", result["htmlPath"])
            return 0
        if args.command == "provenance":
            from .provenance import validate_provenance

            results = [
                {
                    **validate_provenance(source.data_root),
                    "sourceProvider": source.source_provider,
                    "sourceId": source.source_id,
                }
                for source in config.enabled_source_configs()
            ]
            _emit(
                results[0]
                if len(results) == 1
                else {"ok": all(result["ok"] for result in results), "sourceResults": results}
            )
            return 0
        if args.command == "storage":
            from .storage_migration import migrate_storage

            _emit(migrate_storage(config, from_config=args.from_config, dry_run=args.dry_run, config_path=args.config))
            return 0
        if args.command == "status":
            _emit(pipeline_status(config))
            return 0
        LOGGER.info("%s %s", "Planning" if args.dry_run else "Starting", args.command)
        report = run_pipeline(config, mode=args.command, dry_run=args.dry_run,
                              config_path=args.config, progress=_progress)
        for report_path in report.get("reportPaths", []):
            LOGGER.info("Run report: %s", report_path)
        if report.get("reportPath"):
            LOGGER.info("Run report: %s", report["reportPath"])
        for warning in report.get("warnings", []):
            LOGGER.warning("%s", warning)
        if args.full_output:
            _emit(report)
        counts = report["threadCounts"]
        summary = (
            f"{args.command}: normalized {report.get('normalizedThreadCount', 0)} conversations; "
            f"current {counts.get('current', 0)}, deferred {counts.get('deferred', 0)}, "
            f"excluded {counts.get('excluded', 0)}, failed {len(report['failed']) or counts.get('failed', 0)}"
        )
        if report.get("usageTotals", {}).get("requestCount"):
            summary += "; " + _usage_summary(report["usageTotals"])
        failed = bool(report["failed"] or report["threadCounts"].get("failed"))
        if failed:
            LOGGER.error("%s; inspect the saved report for failures", summary)
            return 1
        if (report.get("generationStop") or not report["ok"]
            or (not args.dry_run and args.command in {"clone", "pull"} and not report["complete"])):
            LOGGER.warning("%s; incomplete, next pull resumes pending work", summary)
            return 2
        log_success(LOGGER, "%s; %s", summary, "plan validated (no report saved)" if args.dry_run else "completed")
        return 0
    except (PipelineError, RawCaptureError, OSError, ValueError, SystemExit) as exc:
        LOGGER.error("%s", exc)
        _emit({"ok": False, "error": str(exc)})
        return 1
