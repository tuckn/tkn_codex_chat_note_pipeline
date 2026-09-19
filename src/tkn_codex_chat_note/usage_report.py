"""Read saved usage locally and produce reproducible, self-contained report artifacts."""

from __future__ import annotations

import csv
import io
import json
import webbrowser
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from typing import Any

from .config import AppConfig
from .generation_usage import metric_records, usage_totals
from .report_evidence import DIAGNOSTIC_FIELDS, LABEL_FIELDS, ReportEvidence
from .report_settings import PriceScenario
from .session_notes import PipelineError, atomic_write_text
from .usage_records import TOKEN_FIELDS, timestamp, token_number

DIMENSIONS = (
    "usageId",
    "runId",
    "sourceId",
    "threadId",
    "command",
    "generationProfile",
    "provider",
    "model",
    "requestedModel",
    "reasoningEffort",
    "stage",
    "startedAt",
    "finishedAt",
    "status",
    "noteStatus",
    "usageSource",
    "usageScope",
    "usageComplete",
    "durationSeconds",
    "httpStatus",
    "error",
    "evidencePath",
    *LABEL_FIELDS,
)


def _read(path: Path, inventory: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        content = path.read_bytes()
        value = json.loads(content.decode("utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("expected an object")
        inventory.append({"path": str(path), "sha256": sha256(content).hexdigest()})
        return value
    except (ValueError, OSError) as exc:
        raise PipelineError(f"cannot read usage evidence: {path}: {exc}") from exc


def _normalize(record: dict[str, Any], offset: int) -> dict[str, Any]:
    value = {key: record.get(key) for key in DIMENSIONS}
    for key in TOKEN_FIELDS:
        raw = record.get(key)
        if raw is not None and token_number(raw) is None:
            raise PipelineError(f"invalid {key} for usage {value.get('usageId')}")
        value[key] = raw
        known_key = "known" + key[0].upper() + key[1:]
        known = raw if raw is not None else record.get(known_key, 0)
        if token_number(known) is None:
            raise PipelineError(f"invalid {known_key} for usage {value.get('usageId')}")
        value[known_key] = known
    if value["inputTokens"] is not None and value["cachedInputTokens"] is not None:
        if value["cachedInputTokens"] > value["inputTokens"]:
            raise PipelineError("cached input cannot exceed total input")
    if value["outputTokens"] is not None and value["reasoningTokens"] is not None:
        if value["reasoningTokens"] > value["outputTokens"]:
            raise PipelineError("reasoning tokens cannot exceed total output")
    value["usageComplete"] = value["inputTokens"] is not None and value["outputTokens"] is not None
    value["date"] = None
    if value["startedAt"]:
        try:
            instant = datetime.fromisoformat(str(value["startedAt"]).replace("Z", "+00:00"))
            if instant.tzinfo is None:
                raise ValueError("execution timestamp has no UTC offset")
            value["date"] = instant.astimezone(timezone(timedelta(minutes=offset))).date().isoformat()
        except ValueError as exc:
            raise PipelineError(f"invalid usage execution date: {value['startedAt']}") from exc
    value["displayModel"] = value["model"] or value["requestedModel"] or "unknown"
    return value


def collect_usage(config: AppConfig) -> dict[str, Any]:
    inventory: list[dict[str, Any]] = []
    records: dict[str, dict[str, Any]] = {}
    notes: dict[tuple[str, str, str], dict[str, Any]] = {}
    warnings: list[str] = []
    evidence = ReportEvidence(inventory)
    for source in config.enabled_source_configs():
        for path in sorted((source.state_root / "usage").glob("*/*.json")):
            record = _read(path, inventory)
            if record.get("schemaVersion") != 1 or record.get("sourceId") != source.source_id:
                raise PipelineError(f"unsupported or mismatched usage record: {path}")
            if not record.get("usageId") or not record.get("runId"):
                raise PipelineError(f"usage record has no identity: {path}")
            key = f"{source.source_id}/{record['usageId']}"
            if key in records:
                raise PipelineError(f"duplicate usage identity: {path}")
            records[key] = {**record, "evidencePath": str(path)}
        # Never read last-run.json: it is a duplicate pointer/snapshot, not history.
        for path in sorted(source.reports_root.glob("*.json")):
            report = _read(path, inventory)
            if report.get("dryRun"):
                continue
            run_id = report.get("runId")
            if not isinstance(run_id, str) or not run_id or not isinstance(report.get("threads", []), list):
                raise PipelineError(f"invalid run report: {path}")
            run_context = evidence.run(report, source.source_id, path)
            for entry in report.get("threads", []):
                if not isinstance(entry, dict):
                    raise PipelineError(f"invalid thread record: {path}")
                thread_id = entry.get("threadId")
                if not isinstance(thread_id, str) or not thread_id:
                    raise PipelineError(f"invalid thread identity: {path}")
                context = {
                    **run_context,
                    "threadId": thread_id,
                    "startedAt": entry.get("generationStartedAt") or report.get("startedAt"),
                    "noteStatus": "generated" if entry.get("generated") else entry.get("status"),
                    "taskTitle": entry.get("title"),
                }
                if entry.get("generated"):
                    notes[(source.source_id, run_id, thread_id)] = context
                metrics = entry.get("generationMetrics") or {}
                if not isinstance(metrics, dict):
                    raise PipelineError(f"invalid generation metrics: {path}")
                evidence.thread(entry, {**context, "reportStartedAt": report.get("startedAt")}, source.data_root)
                requests = metric_records(metrics)
                if any(not isinstance(request, dict) for request in requests):
                    raise PipelineError(f"invalid usage entries: {path}")
                if not requests:
                    calls = metrics.get("modelCalls", 0)
                    if type(calls) is not int or calls < 0:
                        raise PipelineError(f"invalid modelCalls: {path}")
                    requests = [{"usageSource": "legacy-unavailable", "status": "unknown"} for _ in range(calls)]
                    if not calls and entry.get("generated") and not metrics:
                        warning = f"{source.source_id}/{run_id}/{thread_id}: historical usage was not recorded"
                        warnings.append(warning)
                        evidence.add(context, "WARNING", "usage-missing", warning)
                for index, request in enumerate(requests):
                    usage_id = request.get("usageId") or f"legacy:{run_id}:{thread_id}:{index}"
                    key = f"{source.source_id}/{usage_id}"
                    if key not in records:
                        records[key] = {**context, **request, "usageId": usage_id,
                                        "requestedModel": request.get("requestedModel")
                                        or request.get("requestedDeployment")}
                    else:
                        records[key]["taskTitle"] = context["taskTitle"]
                    # Journal has the finer execution timestamp and remains authoritative.
    normalized = [_normalize(record, config.usage_report.utc_offset_minutes) for record in records.values()]
    evidence.enrich(normalized)
    normalized.sort(key=lambda record: (record.get("startedAt") or "", record["sourceId"], record["usageId"]))
    for record in normalized:
        if record.get("noteStatus") == "generated":
            note_key = (record["sourceId"], record["runId"], record["threadId"])
            if note_key not in notes or not notes[note_key].get("usageId"):
                notes[note_key] = record
    note_rows = []
    for note in notes.values():
        normalized_note = _normalize(note, config.usage_report.utc_offset_minutes)
        note_rows.append(
            {
                key: normalized_note.get(key)
                for key in (
                    "sourceId",
                    "runId",
                    "threadId",
                    "date",
                    "provider",
                    "generationProfile",
                    "command",
                    *LABEL_FIELDS,
                )
            }
        )
    evidence.enrich(note_rows)
    evidence.attempts(normalized)
    evidence.enrich(evidence.diagnostics)
    diagnostics: dict[str, dict[str, Any]] = {}
    for item in evidence.diagnostics:
        item["date"] = _normalize(item, config.usage_report.utc_offset_minutes)["date"]
        related = [r for r in normalized if r["sourceId"] == item["sourceId"] and r["runId"] == item["runId"]
                   and (not item.get("threadId") or r["threadId"] == item["threadId"])
                   and (not item.get("usageId") or r["usageId"] == item["usageId"])]
        models = sorted({r["displayModel"] for r in related}) or ["unknown"]
        row = {key: item.get(key) for key in DIAGNOSTIC_FIELDS}
        row.update(models=models, displayModel=", ".join(models), sourceRef=item.get("sourceRef"))
        # Remove duplicated run/raw failures, retaining distinct requests and validation attempts.
        identity = json.dumps([row.get(k) for k in (
            "sourceId", "runId", "threadId", "usageId", "severity", "message",
            "stage", "attempt", "occurrence", "sourceRef"
        )], ensure_ascii=False)
        row["diagnosticId"] = sha256(identity.encode()).hexdigest()[:20]
        diagnostics.setdefault(identity, row)
    diagnostic_rows = sorted(diagnostics.values(), key=lambda r: (r.get("startedAt") or "", r["diagnosticId"]))
    return {"records": normalized, "notes": note_rows, "sources": inventory,
            "warnings": warnings, "diagnostics": diagnostic_rows}


def reference_cost(record: dict[str, Any], price: PriceScenario) -> float | None:
    """A same-count scenario, never an invoice or a prediction of another model's work."""
    input_count, output_count = record.get("inputTokens"), record.get("outputTokens")
    if input_count is None or output_count is None:
        return None
    cached = writes = 0
    if price.cache_policy == "observed":
        cached_value = record.get("cachedInputTokens")
        if cached_value is None:
            return None
        cached = int(cached_value)
        if price.cache_write_per_million is not None:
            write_value = record.get("cacheWriteTokens")
            if write_value is None:
                return None
            writes = int(write_value)
    if cached + writes > input_count:
        return None
    return (
        float(
            (input_count - cached - writes) * price.input_per_million
            + cached * (price.cached_input_per_million or 0)
            + writes * (price.cache_write_per_million or 0)
            + output_count * price.output_per_million
        )
        / 1_000_000
    )


def _validate_destination(config: AppConfig) -> Path:
    root = config.report_path.expanduser().absolute()
    resolved = root.resolve()
    for source_id, source_settings in config.sources.items():
        protected = [source_settings.source_root, *config.source_storage_paths(source_id).values()]
        for path in protected:
            path = path.expanduser().resolve()
            if resolved == path or resolved.is_relative_to(path) or path.is_relative_to(resolved):
                raise PipelineError("report_path must be separate from source/raw/data/state/cache roots")
    for path in (root,):
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise PipelineError(f"report_path must not be a link: {path}")
    for name in ("index.html", "usage.json", "usage.csv", "diagnostics.csv"):
        path = root / name
        if path.exists() or path.is_symlink():
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)() or not path.is_file():
                raise PipelineError(f"invalid report artifact: {path}")
    if root.exists() and not root.is_dir():
        raise PipelineError("report_path must be a directory")
    if root.exists() and not (root / "usage.json").is_file() and any(root.iterdir()):
        raise PipelineError("report_path is not an empty or application-owned report directory")
    if (root / "usage.json").is_file():
        saved = _read(root / "usage.json", [])
        if saved.get("application") != "tkn-codex-chat-note" or saved.get("schemaVersion") != 1:
            raise PipelineError("report_path contains an unrecognized usage report")
    return root


def _csv_text(records: list[dict[str, Any]], columns: list[str] | None = None) -> str:
    stream = io.StringIO(newline="")
    columns = columns or [
        *DIMENSIONS,
        "date",
        "displayModel",
        *TOKEN_FIELDS,
        *("known" + key[0].upper() + key[1:] for key in TOKEN_FIELDS),
    ]
    writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for record in records:
        # Human-controlled labels must remain text when opened in a spreadsheet.
        writer.writerow(
            {
                key: "'" + value
                if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r"))
                else value
                for key, value in record.items()
            }
        )
    return stream.getvalue()


def build_usage_report(config: AppConfig, *, dry_run: bool = False, no_open: bool = False) -> dict[str, Any]:
    root = _validate_destination(config)
    data = collect_usage(config)
    scenarios = config.usage_report.price_scenarios
    for record in data["records"]:
        record["referenceCosts"] = {name: reference_cost(record, price) for name, price in scenarios.items()}
    payload = {
        "application": "tkn-codex-chat-note",
        "schemaVersion": 1,
        **data,
        "utcOffsetMinutes": config.usage_report.utc_offset_minutes,
        "priceScenarios": {name: price.model_dump(mode="json") for name, price in scenarios.items()},
        "usageTotals": usage_totals(data["records"]),
        "scope": "Token usage of this pipeline's inference; excludes the original source chats' usage.",
    }
    payload["snapshotId"] = sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    payload["builtAt"] = timestamp()
    result: dict[str, Any] = {
        "ok": True,
        "dryRun": dry_run,
        "recordCount": len(data["records"]),
        "diagnosticCount": len(data["diagnostics"]),
        "usageTotals": payload["usageTotals"],
        "warnings": data["warnings"],
        "htmlPath": str(root / "index.html"),
        "jsonPath": str(root / "usage.json"),
        "csvPath": str(root / "usage.csv"),
        "diagnosticsCsvPath": str(root / "diagnostics.csv"),
        "opened": False,
    }
    if dry_run:
        return result
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    embedded = (
        serialized.replace("<", "\\u003c")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    template = files("tkn_codex_chat_note").joinpath("resources/usage_report.html").read_text(encoding="utf-8")
    script = files("tkn_codex_chat_note").joinpath("resources/usage_report.js").read_text(encoding="utf-8")
    totals = payload["usageTotals"]
    static = (
        f"<p>試行 {len(data['records'])} 件 / 取得済み入力 {totals['knownInputTokens']:,} tokens / "
        f"取得済み出力 {totals['knownOutputTokens']:,} tokens</p>"
        f"<p>入力不明 {totals['inputTokensMissingRequests']} 件 / "
        f"出力不明 {totals['outputTokensMissingRequests']} 件</p>"
    )
    html = template.replace("__USAGE_SCRIPT__", script).replace("__STATIC_SUMMARY__", static)
    html = html.replace("__USAGE_PAYLOAD__", embedded)
    atomic_write_text(root / "usage.json", serialized + "\n")
    atomic_write_text(root / "usage.csv", _csv_text(data["records"]))
    atomic_write_text(root / "diagnostics.csv", _csv_text(
        data["diagnostics"], ["diagnosticId", *DIAGNOSTIC_FIELDS, "sourceRef"]))
    atomic_write_text(root / "index.html", html)
    if not no_open:
        try:
            result["opened"] = webbrowser.open((root / "index.html").as_uri())
        except (OSError, webbrowser.Error) as exc:
            result["warnings"].append(f"Report saved; browser could not open: {exc}")
    return result
