"""Idempotent weekly/monthly/yearly reviews and Task Scheduler entry point."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from .api_inference import ApiBudgetExceeded
from .config import AppConfig
from .frontmatter import parse_simple_frontmatter
from .inference import InferenceExecutionError
from .provenance import json_bytes
from .review_comparison import load_previous, validate_comparison
from .review_evidence import checked_json, digest, period_evidence, review_path
from .review_frontmatter import presentation_signature
from .review_generation import Generator, ReviewBudgetStop, ReviewGenerator
from .review_model import REVIEW_VERSION, Period, PeriodKind, ReviewDraft, closed_periods, period_for
from .review_profiles import ReviewProfile
from .review_render import render_review
from .runtime import PipelineError, atomic_write_bytes, atomic_write_json, now_iso
from .storage import _root_lock, read_json, validate_source_layout


def review_today(config: AppConfig) -> date:
    return datetime.now(timezone(timedelta(minutes=config.reviews.utc_offset_minutes))).date()


def _validate_roots(config: AppConfig) -> None:
    data, state = config.review_data_root.resolve(), config.review_state_root.resolve()
    if data == state or data.is_relative_to(state) or state.is_relative_to(data):
        raise PipelineError("source review data and state roots must be separate directories")
    for root in (config.review_data_root, config.review_state_root):
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            raise PipelineError(f"review root must be an ordinary directory: {root}")
    for source in config.enabled_source_configs():
        for root in (data, state):
            for protected in (source.source_root.resolve(), source.raw_root.resolve()):
                if root == protected or root.is_relative_to(protected) or protected.is_relative_to(root):
                    raise PipelineError("review output must not overlap source or Raw storage")
            if root in {source.data_root.resolve(), source.state_root.resolve()}:
                raise PipelineError("use a dedicated review subdirectory, not an existing source root")


def _paths(config: AppConfig, period: Period) -> tuple[Path, Path]:
    path = review_path(config.review_data_root, period)
    for candidate in (path, path.with_suffix(".md")):
        if candidate.is_symlink() or not candidate.resolve().is_relative_to(config.review_data_root.resolve()):
            raise PipelineError("review output escapes its configured root")
    return path, path.with_suffix(".md")


def _hash(path: Path) -> str | None:
    return sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _protect(paths: tuple[Path, Path], saved: dict[str, Any], *, allow_edited: bool) -> None:
    for path in paths:
        if not path.exists():
            continue
        if path.suffix == ".json":
            status = checked_json(path).get("reviewStatus")
        else:
            status = parse_simple_frontmatter(path.read_text(encoding="utf-8-sig")).get("reviewStatus")
        if status != "unreviewed":
            raise PipelineError(f"reviewed output is protected: {path}")
        expected = saved.get("hashes", {}).get(path.suffix)
        if _hash(path) != expected and not allow_edited:
            raise PipelineError(
                f"edited or unowned output is protected: {path}; use --allow-edited for unreviewed files")


def _recover(config: AppConfig, index: dict[str, Any], *, dry_run: bool) -> None:
    for key, record in list(index.get("reviews", {}).items()):
        pending = record.get("pending")
        if not pending:
            continue
        transaction_path = config.review_state_root / "transactions" / (pending["transaction"] + ".json")
        transaction = checked_json(transaction_path, pending["sha256"])
        for target in transaction["outputs"]:
            path = config.review_data_root / target["relativePath"]
            if path.is_symlink() or not path.resolve().is_relative_to(config.review_data_root.resolve()):
                raise PipelineError("unsafe pending review output")
            if _hash(path) not in (target["previousSha256"], target["sha256"]):
                raise PipelineError(f"pending review output was edited; retained transaction: {transaction_path}")
        if not dry_run:
            for target in transaction["outputs"]:
                atomic_write_bytes(config.review_data_root / target["relativePath"], target["text"].encode("utf-8"))
            index["reviews"][key] = transaction["record"]
            atomic_write_json(config.review_state_root / "index.json", index)


def _publish(config: AppConfig, period: Period, value: dict[str, Any], index: dict[str, Any]) -> None:
    paths = _paths(config, period)
    outputs = (json_bytes(value), render_review(value, paths[0], config.reviews.language).encode("utf-8"))
    key = period.kind + "/" + period.key
    record = {"inputFingerprint": value["inputFingerprint"], "generatedAt": value["generatedAt"],
              "presentationSignature": presentation_signature(config.reviews.language),
              "promptFrameSignature": ReviewProfile(config.reviews.language).request_signature(),
              "hashes": {p.suffix: sha256(b).hexdigest() for p, b in zip(paths, outputs, strict=True)}}
    txid = str(uuid4())
    transaction = {"record": record, "outputs": [
        {"relativePath": path.relative_to(config.review_data_root).as_posix(),
         "sha256": sha256(content).hexdigest(), "previousSha256": _hash(path), "text": content.decode("utf-8")}
        for path, content in zip(paths, outputs, strict=True)]}
    # Retain old bytes before replacement, including explicitly allowed manual edits.
    for path in paths:
        if path.is_file():
            atomic_write_bytes(config.review_state_root / "backups" / txid / path.name, path.read_bytes())
    txpath = config.review_state_root / "transactions" / (txid + ".json")
    atomic_write_json(txpath, transaction)
    index["reviews"][key] = {**index["reviews"].get(key, {}), "pending": {
        "transaction": txid, "sha256": _hash(txpath)}}
    atomic_write_json(config.review_state_root / "index.json", index)
    _recover(config, index, dry_run=False)


def _build(config: AppConfig, period: Period, generator: ReviewGenerator, index: dict[str, Any],
           *, dry_run: bool, force: bool, allow_edited: bool) -> dict[str, Any]:
    paths = _paths(config, period)
    key = period.kind + "/" + period.key
    saved = index["reviews"].get(key, {})
    evidence = period_evidence(config, period)
    previous_context = load_previous(config, period)
    fingerprint = digest({"period": period.document(), "generation": generator.signature,
                          "inputs": evidence.input_records(), "gaps": evidence.gaps,
                          "comparison": previous_context.metadata,
                          "sources": sorted(s.source_id for s in config.enabled_source_configs())})
    result: dict[str, Any] = {"period": period.document(), "jsonPath": str(paths[0]),
                              "notePath": str(paths[1]), "inputCount": len(evidence.selected),
                              "weeklyInputs": evidence.weekly_inputs, "monthlyInputs": evidence.monthly_inputs,
                              "gaps": evidence.gaps, "comparison": previous_context.metadata,
                              "initialBatchCount": len(generator._batches(evidence.input_records()))}
    hashes_match = bool(saved.get("hashes")) and all(_hash(p) == saved["hashes"].get(p.suffix) for p in paths)
    frames_changed = saved.get("promptFrameSignature", generator.request_signature) != generator.request_signature
    if saved.get("inputFingerprint") == fingerprint and hashes_match and not force and not frames_changed:
        if saved.get("presentationSignature") == presentation_signature(config.reviews.language):
            return {**result, "status": "current"}
        try:
            _protect(paths, saved, allow_edited=False)
        except PipelineError as exc:
            return {**result, "status": "preserved", "reason": str(exc)}
        if dry_run:
            return {**result, "status": "planned", "reason": "presentation update only; no model calls"}
        validate_review(paths[0])
        _publish(config, period, checked_json(paths[0]), index)
        return {**result, "status": "rendered", "reason": "presentation updated from saved JSON; no model calls"}
    if (saved.get("inputFingerprint") == fingerprint and all(p.is_file() for p in paths)
            and not force and not frames_changed):
        return {**result, "status": "preserved", "reason": "manual changes retained; source inputs are unchanged"}
    if not saved.get("pending"):
        try:
            _protect(paths, saved, allow_edited=allow_edited)
        except PipelineError as exc:
            return {**result, "status": "blocked", "reason": str(exc)}
    if dry_run:
        return {**result, "status": "planned"}
    draft = generator.build(period, evidence, previous_context)
    draft.validate_sources(set(evidence.evidence) | generator.previous_evidence.keys(),
                           current_ids=set(evidence.evidence))
    generated_at = now_iso()
    previous = checked_json(paths[0]) if paths[0].is_file() else None
    value: dict[str, Any] = {
        "created": previous.get("created") if previous is not None else generated_at,
        "updated": generated_at,
        "schemaVersion": REVIEW_VERSION, "type": "reviewNote", "sourceId": config.source_id,
        "id": str(uuid5(NAMESPACE_URL, str(config.review_data_root.resolve()) + "/" + key)),
        "period": period.document(), "generatedAt": generated_at, "reviewStatus": "unreviewed",
        "inputFingerprint": fingerprint,
        "items": list({digest(item.model_dump()): item.model_dump() for item in draft.items}.values()),
        "evidence": list(evidence.evidence.values()), "sourceInventory": evidence.sources,
        "comparison": previous_context.document(),
        "coverage": {"status": "partial" if evidence.gaps else "observed-records",
                     "completeHistory": False, "inputCount": len(evidence.selected),
                     "weeklyInputs": evidence.weekly_inputs, "monthlyInputs": evidence.monthly_inputs,
                     "gaps": sorted(set(evidence.gaps))},
        "generation": {"profile": config.generation.active_profile, "provider": config.provider,
                       "model": config.model, "settingsSha256": generator.signature,
                       "qualityAudit": generator.quality_report, "promptTemplatesSha256": generator.request_signature},
    }
    if previous is not None and all(value[k] == previous.get(k) for k in (
            "period", "items", "evidence", "sourceInventory", "coverage", "comparison")):
        value["updated"] = previous.get("updated", previous["generatedAt"])
    for field, category in (("intelligenceCandidates", "intelligence-candidate"), ("aiImprovements", "ai-improvement")):
        value[field] = [{"id": "C" + digest([value["id"], item])[:24], "status": "candidate", **item}
                        for item in value["items"] if item["category"] == category]
    _publish(config, period, value, index)
    return {**result, "status": "generated", "gaps": value["coverage"]["gaps"],
            "comparison": {k: v for k, v in value["comparison"].items() if k != "evidence"}}


def _run_source_reviews(config: AppConfig, *, kind: PeriodKind | None = None, day: date | None = None,
                as_of: date | None = None, dry_run: bool = False, force: bool = False,
                allow_edited: bool = False, generate: Generator | None = None) -> dict[str, Any]:
    _validate_roots(config)
    today = as_of or review_today(config)
    if not dry_run:
        config.review_state_root.mkdir(parents=True, exist_ok=True)
        config.review_data_root.mkdir(parents=True, exist_ok=True)
    run_id = str(uuid4())
    report: dict[str, Any] = {"schemaVersion": REVIEW_VERSION, "runId": run_id, "startedAt": now_iso(),
                              "sourceId": config.source_id,
                              "command": "review-notes", "dryRun": dry_run, "asOf": today.isoformat(),
                              "maxCalls": config.reviews.max_calls,
                              "results": [], "ok": True, "complete": True, "reportPath": None}
    lock = nullcontext() if dry_run else _root_lock(config.review_state_root)
    with lock:
        path = config.review_state_root / "index.json"
        index = read_json(path, {"schemaVersion": REVIEW_VERSION, "startedOn": today.isoformat(), "reviews": {}})
        if index.get("schemaVersion") != REVIEW_VERSION:
            raise PipelineError("unsupported review state schema")
        _recover(config, index, dry_run=dry_run)
        if kind:
            target = day or (period_for(kind, today, config.reviews.utc_offset_minutes).start - timedelta(days=1))
            periods = [period_for(kind, target, config.reviews.utc_offset_minutes)]
            if periods[0].end > today:
                raise PipelineError("only closed periods can be reviewed; choose an earlier --date")
        else:
            start = config.reviews.start_date or date.fromisoformat(index["startedOn"])
            periods = closed_periods(start, today, config.reviews.utc_offset_minutes)
        if not dry_run:
            atomic_write_json(path, index)
        generator = ReviewGenerator(config, run_id, generate=generate, force=force)
        try:
            for p in periods:
                try:
                    result = _build(config, p, generator, index, dry_run=dry_run,
                                    force=force, allow_edited=allow_edited)
                except (ReviewBudgetStop, ApiBudgetExceeded) as exc:
                    report["results"].append({"period": p.document(), "status": "deferred", "reason": str(exc)})
                    report["complete"] = False
                    break
                except (PipelineError, InferenceExecutionError, OSError, ValueError) as exc:
                    report["results"].append({"period": p.document(), "status": "failed", "reason": str(exc)})
                    report["ok"] = report["complete"] = False
                    # Stop on provider/validation errors; do not repeatedly submit bad requests.
                    break
                report["results"].append(result)
                if result["status"] == "blocked":
                    report["ok"] = report["complete"] = False
        finally:
            generator.close()
        report.update(modelCalls=generator.calls, cacheHits=generator.cache_hits,
                      plannedCount=sum(r["status"] == "planned" for r in report["results"]),
                      generatedCount=sum(r["status"] == "generated" for r in report["results"]),
                      renderedCount=sum(r["status"] == "rendered" for r in report["results"]),
                      pendingPeriods=len(periods) - len(report["results"]))
        report["finishedAt"] = now_iso()
        if not dry_run:
            report["reportPath"] = str(config.review_state_root / "reports" / f"{run_id}.json")
            atomic_write_json(Path(report["reportPath"]), report)
    return report


def _combine_reviews(reports: list[dict[str, Any]]) -> dict[str, Any]:
    if len(reports) == 1:
        return reports[0]
    return {"ok": all(r["ok"] for r in reports), "complete": all(r["complete"] for r in reports),
            "sourceResults": reports,
            "results": [{"sourceId": r["sourceId"], **item} for r in reports for item in r.get("results", [])],
            "reportPaths": [r["reportPath"] for r in reports if r.get("reportPath")],
            **{key: sum(r.get(key, 0) for r in reports)
               for key in ("modelCalls", "cacheHits", "generatedCount", "renderedCount",
                           "plannedCount", "pendingPeriods")}}


def run_reviews(config: AppConfig, *, kind: PeriodKind | None = None, day: date | None = None,
                as_of: date | None = None, dry_run: bool = False, force: bool = False,
                allow_edited: bool = False, generate: Generator | None = None) -> dict[str, Any]:
    sources = config.enabled_source_configs()
    validate_source_layout(config)
    for source in sources:
        _validate_roots(source)
    reports = []
    for source in sources:
        reports.append(_run_source_reviews(source, kind=kind, day=day, as_of=as_of, dry_run=dry_run,
                                           force=force, allow_edited=allow_edited, generate=generate))
    return _combine_reviews(reports)


def _validate_review(path: Path) -> dict[str, Any]:
    if path.suffix == ".md":
        path = path.with_suffix(".json")
    value = checked_json(path)
    if value.get("schemaVersion") != REVIEW_VERSION or value.get("type") != "reviewNote":
        raise PipelineError("unsupported review schema")
    p = value["period"]
    if p.get("kind") not in {"weekly", "monthly", "yearly"}:
        raise PipelineError("unsupported review period kind")
    period = period_for(p["kind"], date.fromisoformat(p["start"]), p["utcOffsetMinutes"])
    if p != period.document():
        raise PipelineError("invalid review period boundary")
    evidence = {e["id"]: e for e in value["evidence"]}
    if len(evidence) != len(value["evidence"]):
        raise PipelineError("duplicate review evidence IDs")
    draft = ReviewDraft.model_validate({"items": value["items"], "rawRequests": []})
    historical = validate_comparison(value, period)
    draft.validate_sources(set(evidence) | historical.keys(), current_ids=set(evidence))
    for e in evidence.values():
        if not e["timestamps"] or not all(period.contains(t) for t in e["timestamps"]):
            raise PipelineError("review evidence lies outside the period")
        if set(e.get("supports", [])) - evidence.keys():
            raise PipelineError("review has unresolved transitive evidence")
    for field, category in (("intelligenceCandidates", "intelligence-candidate"), ("aiImprovements", "ai-improvement")):
        expected = [{"id": "C" + digest([value["id"], item])[:24], "status": "candidate", **item}
                    for item in value["items"] if item["category"] == category]
        if value.get(field) != expected:
            raise PipelineError("candidate projections differ from review items")
    return {"ok": True, "path": str(path), "itemCount": len(draft.items), "evidenceCount": len(evidence),
            "coverage": value["coverage"],
            "validationScope": "structure, periods, and citation IDs; not semantic accuracy"}


def validate_review(path: Path) -> dict[str, Any]:
    try:
        return _validate_review(path)
    except (KeyError, TypeError, AttributeError) as exc:
        raise PipelineError(f"invalid review document structure: {exc}") from exc


def run_scheduled(config: AppConfig, *, dry_run: bool = False, config_path: Path | None = None) -> dict[str, Any]:
    from .pipeline import run_pipeline

    sources = config.enabled_source_configs()
    validate_source_layout(config)
    if len(sources) > 1:
        results = [run_scheduled(source, dry_run=dry_run, config_path=config_path) for source in sources]
        return {"ok": all(r["ok"] for r in results), "complete": all(r["complete"] for r in results),
                "sourceResults": results,
                "reviews": _combine_reviews([r["reviews"] for r in results if "reviews" in r])}
    config = sources[0]
    _validate_roots(config)
    capture = run_pipeline(config, mode="clone", dry_run=dry_run, config_path=config_path)
    if not capture["ok"]:
        return {"ok": False, "complete": False, "capture": capture}
    reviews = run_reviews(config, dry_run=dry_run)
    return {"ok": reviews["ok"], "complete": reviews["complete"], "capture": capture, "reviews": reviews}
