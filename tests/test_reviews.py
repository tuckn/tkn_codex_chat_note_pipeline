from __future__ import annotations

import json
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from tkn_codex_chat_note.cli import build_parser, main
from tkn_codex_chat_note.config import AppConfig, load_app_config, write_config
from tkn_codex_chat_note.provenance import ProvenanceStore, json_bytes
from tkn_codex_chat_note.review_evidence import period_evidence, review_path
from tkn_codex_chat_note.review_model import ReviewDraft, closed_periods, period_for
from tkn_codex_chat_note.reviews import run_reviews, run_scheduled, validate_review
from tkn_codex_chat_note.runtime import PipelineError, atomic_write_json


def config_for(root: Path, **settings: Any) -> AppConfig:
    return AppConfig(
        sources={"windows": {"source_root": root / "codex", "raw_root": root / "raw",
                             "data_root": root / "data", "state_root": root / "state", "reviews": settings}},
        cache_root=root / "cache",
        generation={"profiles": {"codex": {"provider": "codex", "model": "test-model"}}},
    )


def seed(config: AppConfig, entries: list[tuple[str, str]], *, current: bool = True) -> None:
    source = config.enabled_source_configs()[0]
    root = source.data_root
    root.mkdir(parents=True, exist_ok=True)
    raw_ref = "raw:/codex/windows/sessions/thread.jsonl"
    raw = b"".join(json_bytes({"type": "response_item", "timestamp": ts,
                               "payload": {"type": "message", "role": "user", "text": text}})
                   .replace(b"\n", b" ").rstrip() + b"\n" for ts, text in entries)
    provenance = ProvenanceStore(root)
    raw_entity = provenance.entity(raw, identity="raw:test", ref=raw_ref, kind="raw")
    events = [{"id": f"test:{i}", "localId": f"L{i:06}", "timestamp": ts,
               "actor": "user", "kind": "user_message", "name": "", "turnId": str(i),
               "text": text, "rawRef": f"{raw_ref}#L{i:06}"} for i, (ts, text) in enumerate(entries, 1)]
    canonical = {"events": events, "sourceCaptureSha256": raw_entity["sha256"], "sourceCaptureRef": raw_ref}
    encoded = json_bytes(canonical)
    sha = sha256(encoded).hexdigest()
    canonical_path = root / "source-aligned/test" / (sha + ".json")
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_path.write_bytes(encoded)
    lines = ['---', 'type: sessionNote', 'schemaVersion: 6', 'title: Test note', 'reviewStatus: unreviewed',
             '---', '', '## Timeline', '']
    for i, (_ts, text) in enumerate(entries, 1):
        lines += ['- **12:00:00**', '  - Actor: User', '  - Type: Request', f'  - Text: {text}',
                  f'  - EventRange: L{i:06} -> L{i:06}', f'  - Sources: L{i:06}', '']
    note = root / "session-notes/test.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("\n".join(lines), encoding="utf-8")
    catalog = {"asOf": "2026-06-01T00:00:00Z", "threads": [{"threadId": "thread", "threadKey": "test",
               "status": "current" if current else "pending", "sourceId": "windows", "title": "Test note",
               "canonicalRef": "data:/" + canonical_path.relative_to(root).as_posix(), "canonicalSha256": sha,
               "noteRef": "data:/session-notes/test.md", "noteSha256": sha256(note.read_bytes()).hexdigest()}]}
    atomic_write_json(root / "catalog/threads.json", catalog)
    atomic_write_json(root / "store.json", {"rawRefPrefix": "raw:/codex/windows/", "dataRefAliases": {}})


def item(id: str, *, text: str = "The user asked for a reusable procedure.",
         category: str = "questions") -> dict[str, Any]:
    return {"category": category, "title": "A recorded question", "text": text, "basis": "reported",
            "evidenceIds": [id], "caveat": "Understanding was not confirmed.", "nextCheck": "",
            "intelligenceKind": "none"}


class FakeGenerator:
    def __init__(self, *, request_raw: bool = False) -> None:
        self.prompts: list[str] = []
        self.request_raw = request_raw

    def __call__(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.prompts.append(prompt)
        data = json.loads(prompt.split("BEGIN_REVIEW_INPUT_JSON\n", 1)[1].split("\nEND_REVIEW_INPUT_JSON", 1)[0])
        if "auditUnits" in data:
            return {"decisions": [{"itemId": u["id"], "action": "keep", "replacements": [],
                                   "reason": "Supported by supplied source."} for u in data["auditUnits"]]}
        if "findings" in data:
            return {"decisions": [{"findingId": f["id"], "action": "omit", "targetId": "",
                                   "reason": "Redundant fixture finding."} for f in data["findings"]]}
        if "items" in data:
            return {"items": data["items"], "rawRequests": []}
        if "draft" in data:
            result = data["draft"]
            result["rawRequests"] = []
            if data["rawEvidence"]:
                result["items"][0]["evidenceIds"] = [data["rawEvidence"][0]["id"]]
            return result
        id = data["evidence"][0]["id"]
        result = {"items": [item(id)], "rawRequests": []}
        if self.request_raw:
            result["rawRequests"] = [{"evidenceId": id, "reason": "Verify the original wording and interruption."}]
        return result


def weekly(config: AppConfig, generate: FakeGenerator, **kwargs: Any) -> dict[str, Any]:
    return run_reviews(config, kind="weekly", day=date(2026, 5, 19), as_of=date(2026, 6, 1),
                       generate=generate, **kwargs)


def test_calendar_iso_year_leap_month_and_timezone_boundaries() -> None:
    p = period_for("weekly", date(2027, 1, 1))
    assert (p.key, p.start, p.end) == ("2026-W53", date(2026, 12, 28), date(2027, 1, 4))
    assert period_for("monthly", date(2024, 2, 12)).end == date(2024, 3, 1)
    may = period_for("monthly", date(2026, 5, 1))
    assert may.contains("2026-04-30T15:00:00Z")
    assert not may.contains("2026-05-31T15:00:00Z")
    assert not may.contains("2026-05-01T12:00:00")
    assert not may.contains("unknown")
    closed = closed_periods(date(2026, 12, 1), date(2027, 1, 1), 540)
    assert {p.kind for p in closed} == {"weekly", "monthly", "yearly"}
    assert all(p.end <= date(2027, 1, 1) for p in closed)


def test_dry_run_writes_nothing_and_never_calls_model(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    seed(config, [("2026-05-19T00:00:00Z", "Question")])
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    model = FakeGenerator()
    result = weekly(config, model, dry_run=True)
    assert result["plannedCount"] == 1 and not model.prompts
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_repeat_is_noop_and_later_append_does_not_rebuild_closed_week(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    entries = [("2026-05-19T00:00:00Z", "Question")]
    seed(config, entries)
    model = FakeGenerator()
    first = weekly(config, model)
    assert first["generatedCount"] == 1
    path = Path(first["results"][0]["jsonPath"])
    before = path.read_bytes()
    seed(config, [*entries, ("2026-06-02T00:00:00Z", "New question")])
    second = weekly(config, model)
    assert second["results"][0]["status"] == "current" and len(model.prompts) == 3
    assert before == path.read_bytes()
    assert validate_review(path)["ok"]


def test_changed_historical_event_rebuilds(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    seed(config, [("2026-05-19T00:00:00Z", "Question")])
    model = FakeGenerator()
    weekly(config, model)
    seed(config, [("2026-05-19T00:00:00Z", "Corrected question")])
    assert weekly(config, model)["generatedCount"] == 1
    assert len(model.prompts) == 6


def test_edited_and_reviewed_outputs_are_preserved(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    seed(config, [("2026-05-19T00:00:00Z", "Question")])
    model = FakeGenerator()
    first = weekly(config, model)
    md = Path(first["results"][0]["notePath"])
    md.write_text(md.read_text(encoding="utf-8") + "\nHuman annotation.\n", encoding="utf-8")
    assert weekly(config, model)["results"][0]["status"] == "preserved"
    blocked = weekly(config, model, force=True)
    assert blocked["results"][0]["status"] == "blocked"
    assert len(model.prompts) == 3
    assert weekly(config, model, allow_edited=True, force=True)["generatedCount"] == 1
    assert any("Human annotation" in p.read_text(encoding="utf-8")
               for p in (config.review_state_root / "backups").rglob("*.md"))
    md.write_text(md.read_text(encoding="utf-8").replace('reviewStatus: "unreviewed"', 'reviewStatus: "accepted"'),
                  encoding="utf-8")
    assert weekly(config, model, force=True, allow_edited=True)["results"][0]["status"] == "blocked"


def test_raw_requests_resolve_hash_verified_originals(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    seed(config, [("2026-05-19T00:00:00Z", "Original question")])
    model = FakeGenerator(request_raw=True)
    result = weekly(config, model)
    assert result["ok"], result
    assert len(model.prompts) == 4 and '"type": "response_item"' in model.prompts[1].replace('\\"', '"')
    doc = json.loads(Path(result["results"][0]["jsonPath"]).read_text(encoding="utf-8"))
    assert doc["items"][0]["evidenceIds"][0].startswith("R")
    assert any(e.get("rawLine") == 1 for e in doc["evidence"])
    assert validate_review(Path(result["results"][0]["jsonPath"]))["ok"]


def test_raw_hash_mismatch_is_disclosed_not_used(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    seed(config, [("2026-05-19T00:00:00Z", "Question")])
    blob = next((config.data_root / "provenance/blobs").glob("*/*"))
    blob.write_text("tampered", encoding="utf-8")
    result = weekly(config, FakeGenerator(request_raw=True))
    assert result["ok"]
    assert any("hash mismatch" in g for g in result["results"][0]["gaps"])


def test_raw_uses_cited_snapshot_even_when_same_locator_has_new_version(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    seed(config, [("2026-05-19T00:00:00Z", "Original wording")])
    ProvenanceStore(config.data_root).entity(b'{"text":"Later rewritten wording"}\n', identity="raw:later",
                                           ref="raw:/codex/windows/sessions/thread.jsonl", kind="raw")
    model = FakeGenerator(request_raw=True)
    result = weekly(config, model)
    assert result["ok"] and len(model.prompts) == 4
    assert "Original wording" in model.prompts[1]
    assert "Later rewritten wording" not in model.prompts[1]


def test_unsupported_citation_does_not_publish(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    seed(config, [("2026-05-19T00:00:00Z", "Question")])
    result = weekly(config, lambda *_: {"items": [item("made-up-id")], "rawRequests": []})  # type: ignore[arg-type]
    assert not result["ok"]
    assert not list(config.review_data_root.rglob("*.md"))


def test_empty_model_response_does_not_claim_no_activity(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    seed(config, [("2026-05-19T00:00:00Z", "Question")])
    result = run_reviews(config, kind="weekly", day=date(2026, 5, 19), as_of=date(2026, 6, 1),
                         generate=lambda *_: {"items": [], "rawRequests": []})
    assert not result["ok"] and "nonempty evidence" in result["results"][0]["reason"]


def test_fallback_and_raw_pass_replace_embedded_images_with_metadata(tmp_path: Path) -> None:
    import base64

    config = config_for(tmp_path)
    encoded = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"placeholder" * 50).decode()
    seed(config, [("2026-05-19T00:00:00Z", "Inspect data:image/png;base64," + encoded)], current=False)
    model = FakeGenerator(request_raw=True)
    result = weekly(config, model)
    assert result["ok"]
    assert all(encoded not in prompt for prompt in model.prompts)
    assert all("visual content was not inspected" in prompt for prompt in model.prompts[:2])
    assert "Images have NOT been" in model.prompts[-1]


def test_month_uses_weekly_items_and_filters_boundary_evidence(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    seed(config, [("2026-04-30T00:00:00Z", "April only"), ("2026-05-01T00:00:00Z", "May only")])
    first = run_reviews(config, kind="weekly", day=date(2026, 4, 30), as_of=date(2026, 6, 1), generate=FakeGenerator())
    assert first["ok"]
    p = period_for("monthly", date(2026, 5, 1))
    inputs = period_evidence(config, p)
    assert inputs.weekly_inputs and any(e["text"] == "May only" for e in inputs.evidence.values())
    assert all(p.contains(t) for e in inputs.evidence.values() for t in e["timestamps"])
    assert "April only" not in json.dumps(inputs.input_records())
    result = run_reviews(config, kind="monthly", day=p.start, as_of=date(2026, 6, 1), generate=FakeGenerator())
    assert result["ok"] and validate_review(Path(result["results"][0]["jsonPath"]))["ok"]


def test_year_uses_monthly_items_and_retains_transitive_sources(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    seed(config, [("2026-05-19T00:00:00Z", "Question")])
    weekly(config, FakeGenerator())
    month = run_reviews(config, kind="monthly", day=date(2026, 5, 1), as_of=date(2026, 6, 1),
                        generate=FakeGenerator())
    assert month["ok"]
    path = Path(month["results"][0]["jsonPath"])
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["items"][0]["text"] = "Monthly synthesis distinct from the weekly account."
    atomic_write_json(path, doc)
    inputs = period_evidence(config, period_for("yearly", date(2026, 1, 1)))
    assert inputs.monthly_inputs == [str(path)]
    assert [e["kind"] for e in inputs.input_records()] == ["monthly-review"]
    assert "Monthly synthesis distinct" in inputs.input_records()[0]["text"]
    assert any(e["kind"] == "weekly-review" for e in inputs.evidence.values())
    assert any(e["kind"] == "canonical-excerpt" for e in inputs.evidence.values())
    result = run_reviews(config, kind="yearly", day=date(2026, 1, 1), as_of=date(2027, 1, 5),
                         generate=FakeGenerator())
    assert result["ok"], result
    year_path = Path(result["results"][0]["jsonPath"])
    assert validate_review(year_path)["ok"]
    assert json.loads(year_path.read_text(encoding="utf-8"))["coverage"]["monthlyInputs"] == [str(path)]


@pytest.mark.parametrize("invalid_month", [False, True])
def test_year_falls_back_to_weekly_when_month_missing_or_invalid(tmp_path: Path, invalid_month: bool) -> None:
    config = config_for(tmp_path)
    seed(config, [("2026-05-19T00:00:00Z", "Question")])
    weekly(config, FakeGenerator())
    if invalid_month:
        path = review_path(config.review_data_root, period_for("monthly", date(2026, 5, 1)))
        atomic_write_json(path, {"text": "INVALID MONTH MUST NOT BE USED"})
    inputs = period_evidence(config, period_for("yearly", date(2026, 1, 1)))
    assert not inputs.monthly_inputs and inputs.weekly_inputs
    assert any(e["kind"] == "weekly-review" for e in inputs.input_records())
    assert "INVALID MONTH" not in json.dumps(inputs.input_records())
    assert any("2026-05: monthly review unavailable" in g for g in inputs.gaps)


def test_year_raw_request_traverses_month_and_week_and_reuses_unchanged_inputs(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    seed(config, [("2026-05-19T00:00:00Z", "Original question for annual verification")])
    weekly(config, FakeGenerator())
    month = run_reviews(config, kind="monthly", day=date(2026, 5, 1), as_of=date(2026, 6, 1),
                        generate=FakeGenerator())
    assert month["ok"]
    model = FakeGenerator(request_raw=True)
    args = {"kind": "yearly", "day": date(2026, 1, 1), "as_of": date(2027, 1, 5), "generate": model}
    first = run_reviews(config, **args)  # type: ignore[arg-type]
    assert first["ok"] and len(model.prompts) == 4, first
    assert "Original question for annual verification" in model.prompts[1]
    doc = json.loads(Path(first["results"][0]["jsonPath"]).read_text(encoding="utf-8"))
    assert doc["items"][0]["evidenceIds"][0].startswith("R")
    assert validate_review(Path(first["results"][0]["jsonPath"]))["ok"]
    assert run_reviews(config, **args)["results"][0]["status"] == "current"  # type: ignore[arg-type]
    assert len(model.prompts) == 4
    path = Path(month["results"][0]["jsonPath"])
    monthly = json.loads(path.read_text(encoding="utf-8"))
    monthly["items"][0]["text"] = "Revised monthly interpretation."
    atomic_write_json(path, monthly)
    assert run_reviews(config, **args)["generatedCount"] == 1  # type: ignore[arg-type]
    assert len(model.prompts) == 6  # identical final findings reuse composition and audit checkpoints


def test_budget_resume_reuses_completed_stages(tmp_path: Path) -> None:
    config = config_for(tmp_path, max_calls=1, input_characters=14000)
    seed(config, [(f"2026-05-19T{i:02}:00:00Z", (str(i) + " evidence ") * 230) for i in range(8)])
    model = FakeGenerator()
    first = weekly(config, model)
    assert not first["complete"] and first["results"][0]["status"] == "deferred"
    config.reviews.max_calls = 20
    final = weekly(config, model)
    assert final["ok"] and final["complete"], final
    assert final["cacheHits"] > 0 and final["generatedCount"] == 1


def test_interrupted_pair_commit_recovers_without_inference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tkn_codex_chat_note.reviews as reviews

    config = config_for(tmp_path)
    seed(config, [("2026-05-19T00:00:00Z", "Question")])
    original = reviews.atomic_write_bytes

    def fail_md(path: Path, content: bytes) -> None:
        if path.suffix == ".md":
            raise OSError("simulated interruption")
        original(path, content)

    model = FakeGenerator()
    monkeypatch.setattr(reviews, "atomic_write_bytes", fail_md)
    assert not weekly(config, model)["ok"]
    monkeypatch.setattr(reviews, "atomic_write_bytes", original)
    recovered = weekly(config, model)
    assert recovered["ok"] and recovered["results"][0]["status"] == "current"
    assert len(model.prompts) == 3


def test_calendar_start_no_implicit_backfill_and_offline_catchup(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    first = run_reviews(config, as_of=date(2026, 10, 12), generate=FakeGenerator())
    assert first["results"] == []
    second = run_reviews(config, as_of=date(2026, 10, 26), generate=FakeGenerator())
    assert [r["period"]["key"] for r in second["results"]] == ["2026-W42", "2026-W43"]
    assert second["modelCalls"] == 0  # empty inputs never require paid inference


def test_open_period_is_rejected(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    with pytest.raises(PipelineError, match="closed"):
        run_reviews(config, kind="monthly", day=date(2026, 10, 1), as_of=date(2026, 10, 12), dry_run=True)


def test_source_and_raw_output_overlap_is_rejected(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    config.sources["windows"].data_root = config.raw_root / "reviews"
    with pytest.raises(PipelineError, match="overlap"):
        weekly(config, FakeGenerator(), dry_run=True)


def test_review_config_paths_and_cli_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = config_for(tmp_path)
    path = tmp_path / "config.yaml"
    write_config(config, path)
    loaded = load_app_config(explicit_path=path, cwd=tmp_path)
    assert loaded.reviews.language == "ja" and loaded.reviews.utc_offset_minutes == 540
    args = build_parser().parse_args(["review-notes", "build", "--period", "yearly"])
    assert args.period == "yearly" and not args.dry_run
    monkeypatch.chdir(tmp_path)
    assert main(["--config", str(path), "review-notes", "due", "--dry-run"]) == 0
    assert not config.review_state_root.exists()


def test_scheduled_capture_does_not_generate_before_due(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tkn_codex_chat_note.pipeline as pipeline

    config = config_for(tmp_path)
    calls: list[str] = []

    def run(*_: Any, mode: str, **__: Any) -> dict[str, Any]:
        calls.append(mode)
        return {"ok": True, "complete": True}

    monkeypatch.setattr(pipeline, "run_pipeline", run)
    result = run_scheduled(config)
    assert result["ok"] and calls == ["clone"]


def test_intelligence_and_improvement_candidates_keep_status_and_citations(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    seed(config, [("2026-05-19T00:00:00Z", "Repeated the same import because the existing result was not located.")])

    def generate(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        base = FakeGenerator()(prompt, schema)
        if "decisions" in base:
            return base
        source = base["items"][0]
        base["items"] = [
            {**source, "category": "intelligence-candidate", "intelligenceKind": "knowledge", "basis": "inferred",
             "text": "Locate existing imports before repeating the import.", "nextCheck": "Try on one new import."},
            {**source, "category": "ai-improvement", "basis": "proposed", "intelligenceKind": "none",
             "text": "Supply an index of existing results.", "nextCheck": "Check whether repeated imports decrease."},
        ]
        return base

    result = run_reviews(config, kind="weekly", day=date(2026, 5, 19), as_of=date(2026, 6, 1), generate=generate)
    path = Path(result["results"][0]["jsonPath"])
    value = json.loads(path.read_text(encoding="utf-8"))
    assert value["intelligenceCandidates"][0]["status"] == "candidate"
    assert value["aiImprovements"][0]["basis"] == "proposed"
    assert validate_review(path)["ok"]
    value["aiImprovements"][0]["status"] = "adopted"
    atomic_write_json(path, value)
    with pytest.raises(PipelineError, match="projections"):
        validate_review(path)


@pytest.mark.parametrize("profile", ["default-jp", "default-en"])
def test_generated_schema_resource_matches_contract(profile: str) -> None:
    from importlib.resources import files

    resource = files("tkn_codex_chat_note").joinpath("profiles", profile, "output.schema.json")
    schema = json.loads(resource.read_text("utf-8"))
    assert schema == ReviewDraft.model_json_schema()


@pytest.mark.parametrize("period", [None, {"kind": "unexpected"}])
def test_invalid_review_structure_has_actionable_error(tmp_path: Path, period: Any) -> None:
    path = tmp_path / "invalid.json"
    atomic_write_json(path, {"schemaVersion": "1.0.0", "type": "reviewNote", "period": period})
    with pytest.raises(PipelineError, match="structure|period kind"):
        validate_review(path)


def test_review_usage_is_included_in_offline_report(tmp_path: Path) -> None:
    from tkn_codex_chat_note.usage_records import UsageJournal, new_record
    from tkn_codex_chat_note.usage_report import collect_usage

    config = config_for(tmp_path)
    journal = UsageJournal(config.review_state_root / "usage/run", {"sourceId": "reviews", "runId": "run",
                           "threadId": "weekly/2026-W21", "command": "review-notes", "noteStatus": "generated"})
    journal({"type": "usage-complete", **new_record("codex", "test-model", "high"), "status": "received"})
    report = collect_usage(config)
    assert len(report["records"]) == 1
    assert report["records"][0]["command"] == "review-notes"
    assert report["records"][0]["inputTokens"] is None


def test_scheduled_due_run_only_captures_normalizes_and_reviews(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tkn_codex_chat_note.pipeline as pipeline
    import tkn_codex_chat_note.reviews as reviews

    config = config_for(tmp_path, start_date=date(2026, 5, 18))
    calls: list[dict[str, Any]] = []

    def run(*_: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"ok": True, "complete": True}

    monkeypatch.setattr(pipeline, "run_pipeline", run)
    monkeypatch.setattr(reviews, "review_today", lambda _: date(2026, 5, 25))
    result = run_scheduled(config)
    assert result["ok"]
    assert [c["mode"] for c in calls] == ["clone"]
    assert "limit" not in calls[0]


def test_review_repair_resumes_without_repeating_evidence(tmp_path: Path) -> None:
    from tkn_codex_chat_note.review_generation import ReviewBudgetStop, ReviewGenerator

    config = config_for(tmp_path, max_calls=1).enabled_source_configs()[0]
    period = period_for("weekly", date(2026, 5, 19))
    bad = {"items": [{**item("e1", category="insights"), "nextCheck": "Verify across chats."}],
           "rawRequests": []}
    first = ReviewGenerator(config, "first", generate=lambda *_: bad)
    with pytest.raises(ReviewBudgetStop):
        first._call("review", period, {"evidence": "large original input"}, {"e1"})
    assert first.calls == 1
    prompts = []

    def repair(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        prompts.append(prompt)
        return {"items": [{**bad["items"][0], "basis": "inferred"}], "rawRequests": []}

    second = ReviewGenerator(config, "second", generate=repair)
    result = second._call("review", period, {"evidence": "large original input"}, {"e1"})
    assert result.items[0].basis == "inferred"
    assert second.calls == 1
    assert "Stage: repair" in prompts[0]
    assert "large original input" not in prompts[0]
    assert not list(second.cache_root.glob("*.pending.json"))
    second._call("review", period, {"evidence": "large original input"}, {"e1"})
    assert second.cache_hits == 1


def test_repair_does_not_accept_unavailable_citations(tmp_path: Path) -> None:
    from tkn_codex_chat_note.review_generation import ReviewGenerator

    config = config_for(tmp_path).enabled_source_configs()[0]
    generator = ReviewGenerator(config, "bad", generate=lambda *_: {"items": [item("invented")], "rawRequests": []})
    with pytest.raises(ValueError, match="unavailable evidence"):
        generator._call("review", period_for("weekly", date(2026, 5, 19)), {}, {"e1"})
    assert generator.calls == 2
    assert not [p for p in generator.cache_root.glob("*.json") if not p.name.endswith(".pending.json")]


@pytest.mark.parametrize("kind,day,as_of", [
    ("weekly", date(2026, 5, 19), date(2026, 6, 1)),
    ("monthly", date(2026, 5, 1), date(2026, 6, 1)),
    ("yearly", date(2026, 1, 1), date(2027, 1, 1)),
])
def test_all_periods_have_a_final_editorial_pass(tmp_path: Path, kind: Any, day: date, as_of: date) -> None:
    config = config_for(tmp_path)
    seed(config, [("2026-05-19T00:00:00Z", "A small but distinct question")])
    model = FakeGenerator()
    result = run_reviews(config, kind=kind, day=day, as_of=as_of, generate=model)
    assert result["generatedCount"] == 1
    assert len(model.prompts) == 3
    assert "Stage: compose" in model.prompts[-2]
    assert "Preserve distinct small questions" in model.prompts[-2]
    assert "do not concatenate monthly overviews" in model.prompts[-2]
    assert "Stage: evidence-check" in model.prompts[-1]
    assert "Stage: review" in model.prompts[0]


def test_editorial_change_reuses_extraction_but_rebuilds_final_output(tmp_path: Path) -> None:
    from tkn_codex_chat_note.review_generation import ReviewGenerator

    config = config_for(tmp_path).enabled_source_configs()[0]
    seed(config, [("2026-05-19T00:00:00Z", "Keep the recorded question")])
    period = period_for("weekly", date(2026, 5, 19))
    first = ReviewGenerator(config, "first", generate=FakeGenerator())
    first.build(period, period_evidence(config, period))
    second = ReviewGenerator(config, "second", generate=FakeGenerator())
    second.synthesis += "\nA revised editorial instruction."
    second.build(period, period_evidence(config, period))
    assert second.cache_hits == 2  # extraction and unchanged audit reused
    assert second.calls == 1  # final composition only
    third = ReviewGenerator(config, "third", generate=FakeGenerator())
    third.synthesis = second.synthesis
    third.build(period, period_evidence(config, period))
    assert third.cache_hits == 3 and third.calls == 0


def test_editorial_pass_resumes_after_budget_without_repeating_extraction(tmp_path: Path) -> None:
    config = config_for(tmp_path, max_calls=1)
    seed(config, [("2026-05-19T00:00:00Z", "Keep this question")])
    first = weekly(config, FakeGenerator())
    assert first["results"][0]["status"] == "deferred"
    assert not list(config.review_data_root.rglob("*.md"))
    model = FakeGenerator()
    second = weekly(config, model)
    assert second["results"][0]["status"] == "deferred" and second["cacheHits"] == 1
    assert not list(config.review_data_root.rglob("*.md"))
    assert len(model.prompts) == 1 and "Stage: compose" in model.prompts[0]
    third = weekly(config, FakeGenerator())
    assert third["generatedCount"] == 1 and third["cacheHits"] == 2


@pytest.mark.parametrize("language", ["ja", "en"])
def test_editorial_render_keeps_citations_tables_and_bounded_source_quotes(tmp_path: Path, language: str) -> None:
    from tkn_codex_chat_note.review_render import render_review

    config = config_for(tmp_path, language=language)
    source_text = "PRIVATE_FULL_SOURCE_SENTINEL " + "X" * 400
    seed(config, [("2026-05-19T00:00:00Z", source_text)])
    result = weekly(config, FakeGenerator())
    value = json.loads(Path(result["results"][0]["jsonPath"]).read_text(encoding="utf-8"))
    id = value["items"][0]["evidenceIds"][0]
    value["items"] = [item(id, text="A | B\n<literal>"), item(id, category="activities")]
    text = render_review(value, tmp_path / "review.json", language)
    assert "A &#124; B<br>&lt;literal&gt;" in text
    assert source_text not in text
    assert "PRIVATE_FULL_SOURCE_SENTINEL" in text and "<details>" in text
    assert text.count("### S001") == 1 and "### S002" not in text
    assert text.count("[S001](#s001-c1)") == 2
    assert "review.json" in text
    assert value["evidence"][0]["text"] == source_text


def test_editorial_pass_cannot_introduce_unknown_evidence(tmp_path: Path) -> None:
    from tkn_codex_chat_note.review_generation import ReviewGenerator

    config = config_for(tmp_path).enabled_source_configs()[0]
    generator = ReviewGenerator(config, "invalid", generate=lambda *_: {"items": [item("absent")], "rawRequests": []})
    with pytest.raises(ValueError, match="unavailable evidence"):
        generator.synthesize(period_for("weekly", date(2026, 5, 19)),
                             [ReviewDraft.model_validate({"items": [item("known")], "rawRequests": []})])
