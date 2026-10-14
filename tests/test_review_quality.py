from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from test_reviews import FakeGenerator, config_for, item, seed, weekly

from tkn_codex_chat_note.review_evidence import EvidenceSet
from tkn_codex_chat_note.review_generation import ReviewGenerator
from tkn_codex_chat_note.review_model import ReviewDraft, period_for
from tkn_codex_chat_note.review_quality import (
    CoverageResult,
    EvidenceResult,
    _batches,
    audit_review,
    audit_units,
)


def evidence(id: str, text: str, minute: int, actor: str = "assistant") -> dict[str, Any]:
    return {"id": id, "text": text, "actor": actor, "threadId": "example-thread",
            "timestamps": [f"2026-05-19T00:{minute:02}:00Z"], "kind": "canonical-excerpt"}


def test_audit_includes_later_completion_and_bounds_long_sources() -> None:
    draft = ReviewDraft.model_validate({"items": [item("request")], "rawRequests": []})
    sources = EvidenceSet(evidence={e["id"]: e for e in [evidence("request", "Add keybindings", 0, "user"),
        evidence("plan", "I will add them", 1), evidence("done", "Added bindings. " + "x" * 6000, 2)]})
    unit = audit_units(draft, sources, {}, ceiling=4000)[0]
    assert {e["id"] for e in unit["evidence"]} == {"request", "plan", "done"}
    done = next(e for e in unit["evidence"] if e["id"] == "done")
    assert done["truncated"] and done["originalCharacters"] > 6000
    assert "omitted middle" in done["text"]
    assert len(json.dumps({"auditUnits": [unit]}, ensure_ascii=False)) <= 4000


def test_shared_sources_are_sent_once_but_unit_permissions_stay_separate() -> None:
    units = [{"id": "a", "item": item("x"), "restoreFindings": [],
              "evidence": [evidence("x", "shared", 0)]},
             {"id": "b", "item": item("x"), "restoreFindings": [],
              "evidence": [evidence("x", "shared", 0), evidence("y", "Only b may cite this", 1)]}]
    payload = _batches(units, {}, 10000, "auditUnits")[0]
    assert len(payload["evidence"]) == 2
    assert payload["auditUnits"][0]["evidenceIds"] == ["x"]
    result = EvidenceResult.model_validate({"decisions": [
        {"itemId": "a", "action": "replace", "replacements": [item("y")], "reason": "Wrong unit"},
        {"itemId": "b", "action": "keep", "replacements": [], "reason": "Supported"}]})
    with pytest.raises(ValueError, match="unavailable"):
        result.check({u["id"]: u for u in units})


@pytest.mark.parametrize("ids", [[], ["a", "a"], ["b"]])
def test_missing_duplicate_or_foreign_decisions_are_rejected(ids: list[str]) -> None:
    result = CoverageResult.model_validate({"decisions": [
        {"findingId": id, "action": "omit", "targetId": "", "reason": "Duplicate"} for id in ids]})
    with pytest.raises(ValueError, match="exactly once"):
        result.check({"a"}, {"D0"})
    checked = EvidenceResult.model_validate({"decisions": [
        {"itemId": id, "action": "keep", "replacements": [], "reason": "Supported"} for id in ids]})
    with pytest.raises(ValueError, match="exactly once"):
        checked.check({"a": {"restoreFindings": [], "evidence": []}})


def test_restored_finding_cannot_be_silently_kept() -> None:
    result = EvidenceResult.model_validate({"decisions": [
        {"itemId": "a", "action": "keep", "replacements": [], "reason": "Supported"}]})
    with pytest.raises(ValueError, match="incorporated"):
        result.check({"a": {"restoreFindings": [item("x")], "evidence": []}})


def test_lost_purpose_restored_and_unrelated_tasks_split_with_actual_completion(tmp_path: Path) -> None:
    original = [item("request", text="Image and keybindings were completed.", category="activities")]
    findings = [item("purpose", text="Descriptions support future discovery and reuse.", category="learning")]
    sources = EvidenceSet(evidence={e["id"]: e for e in [
        evidence("request", "Please revise the image and add editor bindings.", 0, "user"),
        evidence("done", "Editor bindings added. Image completion is not reported.", 1),
        evidence("purpose", "Descriptions are for finding and reusing images.", 2)]})
    stages = []

    def generate(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        data = json.loads(prompt.split("BEGIN_REVIEW_INPUT_JSON\n")[1].split("\nEND_REVIEW_INPUT_JSON")[0])
        if "findings" in data:
            stages.append("coverage")
            return {"decisions": [{"findingId": data["findings"][0]["id"], "action": "restore",
                                   "targetId": "", "reason": "Distinct reuse purpose was lost"}]}
        stages.append("evidence")
        decisions = []
        for u in data["auditUnits"]:
            replacements = ([item("request", text="Image revision requested.", category="activities"),
                             item("done", text="Assistant reported bindings added.", category="activities")]
                            if u["id"] == "D0" else [findings[0]])
            decisions.append({"itemId": u["id"], "action": "replace", "replacements": replacements,
                              "reason": "Correct state and preserve distinct meaning"})
        return {"decisions": decisions}

    generator = ReviewGenerator(config_for(tmp_path), "audit", generate=generate)
    final = audit_review(generator, period_for("weekly", date(2026, 5, 19)),
                         ReviewDraft.model_validate({"items": original, "rawRequests": []}),
                         ReviewDraft.model_validate({"items": findings, "rawRequests": []}).items, sources)
    assert stages == ["coverage", "evidence"]
    assert len(final.items) == 3
    assert final.items[1].evidenceIds == ["done"]
    assert "reuse" in final.items[2].text
    assert generator.quality_report["status"] == "completed"
    # No unverified synthesis follows source-backed replacements.
    assert generator.calls == 2


def test_invalid_audit_never_replaces_existing_note_and_repair_is_bounded(tmp_path: Path) -> None:
    cfg = config_for(tmp_path)
    seed(cfg, [("2026-05-19T00:00:00Z", "Initial question")])
    first = weekly(cfg, FakeGenerator())
    path = Path(first["results"][0]["notePath"])
    before = path.read_bytes()
    seed(cfg, [("2026-05-19T00:00:00Z", "Changed question")])
    fake = FakeGenerator()
    audits = []

    def invalid(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        if "Stage: evidence-check" in prompt or "Stage: audit-repair" in prompt:
            audits.append(prompt)
            return {"decisions": []}
        return fake(prompt, schema)

    result = weekly(cfg, invalid)  # type: ignore[arg-type]
    assert not result["ok"] and len(audits) == 2
    assert path.read_bytes() == before
    assert not list(cfg.review_state_root.rglob("*.pending.md"))


@pytest.mark.parametrize("language,heading", [("ja", "考察・仮説"), ("en", "Insights and hypotheses")])
def test_insight_heading_does_not_claim_multiple_chats_for_one_source(
    tmp_path: Path, language: str, heading: str,
) -> None:
    from tkn_codex_chat_note.review_render import render_review

    cfg = config_for(tmp_path, language=language)
    seed(cfg, [("2026-05-19T00:00:00Z", "A reusable interpretation from one conversation")])
    result = weekly(cfg, FakeGenerator())
    value = json.loads(Path(result["results"][0]["jsonPath"]).read_text(encoding="utf-8"))
    source = value["items"][0]
    value["items"] = [{**source, "category": "insights", "basis": "inferred", "nextCheck": "Verify next time"}]
    rendered = render_review(value, tmp_path / "review.json", language)
    assert "## " + heading in rendered
    assert "Cross-conversation insights" not in rendered and "複数チャットを跨ぐ考察" not in rendered


@pytest.mark.parametrize("kind", ["weekly", "monthly", "yearly"])
def test_verbose_compose_is_repaired_once_then_cached(tmp_path: Path, kind: Any) -> None:
    from tkn_codex_chat_note.review_quality import prose_characters

    cfg = config_for(tmp_path)
    calls = []
    original = ReviewDraft.model_validate({"items": [item("x")], "rawRequests": []})

    def generate(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        calls.append(prompt)
        return {"items": [item("x", text="x" * 7100)] if len(calls) == 1 else [item("x")], "rawRequests": []}

    generator = ReviewGenerator(cfg, "length-repair", generate=generate)
    period = period_for(kind, date(2026, 5, 19))
    result = generator.synthesize(period, [original])
    assert len(calls) == 2 and "Stage: repair" in calls[1]
    assert "editorialLimits" in calls[1] and prose_characters(result.items) < 7000
    assert generator.synthesize(period, [original]) == result
    assert len(calls) == 2 and generator.cache_hits == 1


def test_audit_rejects_expansion_and_excessive_restoration() -> None:
    from tkn_codex_chat_note.review_quality import _check_coverage

    unit = {"restoreFindings": [], "evidence": [evidence("x", "Reported result", 0)],
            "replacementBudget": {"maxItems": 2, "maxProseCharacters": 400}}
    decision = {"itemId": "D0", "action": "replace", "replacements": [item("x", text="x" * 401)],
                "reason": "Unnecessary expansion"}
    with pytest.raises(ValueError, match="editorial budget"):
        EvidenceResult.model_validate({"decisions": [decision]}).check({"D0": unit})
    decision["replacements"] = [item("x")] * 3
    with pytest.raises(ValueError, match="editorial budget"):
        EvidenceResult.model_validate({"decisions": [decision]}).check({"D0": unit})
    coverage = CoverageResult.model_validate({"decisions": [
        {"findingId": "F1", "action": "restore", "targetId": "", "reason": "New topic"}]})
    with pytest.raises(ValueError, match="restoration budget"):
        _check_coverage(coverage, findings={"F1"}, targets={"D0"}, restores=1, new_topics=0)


def test_failed_length_repair_preserves_existing_note(tmp_path: Path) -> None:
    cfg = config_for(tmp_path)
    seed(cfg, [("2026-05-19T00:00:00Z", "Initial question")])
    first = weekly(cfg, FakeGenerator())
    path = Path(first["results"][0]["notePath"])
    before = path.read_bytes()
    seed(cfg, [("2026-05-19T00:00:00Z", "Changed question")])
    fake = FakeGenerator()
    calls = []

    def verbose(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        if "Stage: compose" in prompt or "Stage: repair" in prompt:
            calls.append(prompt)
            data = json.loads(prompt.split("BEGIN_REVIEW_INPUT_JSON\n")[1].split("\nEND_REVIEW_INPUT_JSON")[0])
            id = data.get("availableEvidenceIds", [data.get("items", [{}])[0].get("evidenceIds", [""])[0]])[0]
            return {"items": [item(id, text="x" * 7100)], "rawRequests": []}
        return fake(prompt, schema)

    result = weekly(cfg, verbose)  # type: ignore[arg-type]
    assert not result["ok"] and len(calls) == 2
    assert path.read_bytes() == before



def test_audit_finds_turn_completion_and_later_correction_beyond_progress_messages() -> None:
    original = item("request", text="Canvas was updated.", category="activities")
    rows = [evidence("request", "Update Canvas", 0, "user")]
    rows += [evidence(f"progress{n}", "Checking the existing layout", n) for n in range(1, 6)]
    rows += [evidence("done", "Canvas updated; seven groups added.", 6),
             evidence("correction", "The new layout is correct, but another condition remains.", 7, "user"),
             evidence("terminal", "That remaining condition is now corrected.", 8)]
    sources = EvidenceSet(evidence={e["id"]: e for e in rows})
    draft = ReviewDraft.model_validate({"items": [original], "rawRequests": []})
    ids = {e["id"] for e in audit_units(draft, sources, {})[0]["evidence"]}
    assert {"done", "correction", "terminal"} <= ids



def test_split_budget_is_shared_across_rows() -> None:
    from tkn_codex_chat_note.review_quality import _check_evidence

    units = {id: {"restoreFindings": [], "evidence": [evidence("x", "Supported", 0)]} for id in ["D0", "D1"]}
    result = EvidenceResult.model_validate({"decisions": [
        {"itemId": id, "action": "replace", "replacements": [item("x"), item("x")], "reason": "Split"}
        for id in units]})
    with pytest.raises(ValueError, match="shared split budget"):
        _check_evidence(result, units=units, max_splits=1)
    _check_evidence(result, units=units, max_splits=2)



def test_length_repair_resends_only_offending_row_and_its_sources(tmp_path: Path) -> None:
    from functools import partial

    from tkn_codex_chat_note.review_quality import _check_evidence

    units = {id: {"id": id, "item": item(eid), "restoreFindings": [],
                  "evidence": [evidence(eid, "Original source", n)],
                  "replacementBudget": {"maxItems": 1, "maxProseCharacters": 350}}
             for n, (id, eid) in enumerate([("D0", "x"), ("D1", "y")])}
    payload = _batches(list(units.values()), {}, 10000, "auditUnits")[0]
    prompts = []

    def generate(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        prompts.append(prompt)
        if len(prompts) == 1:
            return {"decisions": [
                {"itemId": "D0", "action": "replace", "replacements": [item("x", text="x" * 400)], "reason": "Fix"},
                {"itemId": "D1", "action": "keep", "replacements": [], "reason": "Supported"}]}
        data = json.loads(prompt.split("BEGIN_REVIEW_INPUT_JSON\n")[1].split("\nEND_REVIEW_INPUT_JSON")[0])
        assert "D0:" in data["validationError"] and "350 characters" in data["validationError"]
        assert [u["id"] for u in data["originalInput"]["auditUnits"]] == ["D0"]
        assert [e["id"] for e in data["originalInput"]["evidence"]] == ["e1"]
        assert data["originalInput"]["auditUnits"][0]["item"]["evidenceIds"] == ["e1"]
        return {"decisions": [{"itemId": "D0", "action": "replace",
                               "replacements": [item("x")], "reason": "Shortened"}]}

    generator = ReviewGenerator(config_for(tmp_path), "targeted-repair", generate=generate)
    result = generator._quality_call("evidence-check", period_for("weekly", date(2026, 5, 19)),
                                     payload, EvidenceResult, partial(_check_evidence, units=units))
    assert [d.itemId for d in result.decisions] == ["D0", "D1"]
    assert result.decisions[1].action == "keep"
    assert len(prompts) == 2



def test_shared_citation_does_not_hide_a_distinct_later_decision() -> None:
    from tkn_codex_chat_note.review_quality import coverage_plan

    initial = item("shared", text="The first version used automatic updates.", category="activities")
    correction = item("shared", text="Later the user rejected automatic updates.", category="decisions")
    draft = ReviewDraft.model_validate({"items": [initial], "rawRequests": []})
    findings = ReviewDraft.model_validate({"items": [initial, correction], "rawRequests": []}).items
    payload = coverage_plan(draft, findings, 10000)[0]
    assert [r["item"]["text"] for r in payload["findings"]] == [correction["text"]]


def test_mapped_findings_bring_distant_correction_and_completion_to_source_audit() -> None:
    initial = item("request", text="The initial version was implemented.", category="activities")
    correction = item("later", text="State saving is no longer wanted.", category="decisions")
    completion = item("done", text="The separate view references were updated.", category="activities")
    rows = [evidence("request", "Implement the initial version", 0, "user")]
    rows += [evidence(f"other{n}", "Unrelated subsequent discussion", n) for n in range(1, 20)]
    rows += [evidence("later", "Do not save prior state; use manual save instead.", 20, "user"),
             evidence("done", "View references updated.", 21), evidence("end", "Unrelated final answer", 22)]
    sources = EvidenceSet(evidence={e["id"]: e for e in rows})
    draft = ReviewDraft.model_validate({"items": [initial], "rawRequests": []})
    unit = audit_units(draft, sources, {}, related={"D0": [correction, completion]})[0]
    assert {"later", "done"} <= {e["id"] for e in unit["evidence"]}
    assert unit["relatedFindings"] == [correction, completion]


@pytest.mark.parametrize("language", ["ja", "en"])
def test_all_row_budgets_fit_global_limit_even_with_restorations(language: str) -> None:
    from tkn_codex_chat_note.review_quality import allocate_editorial_budgets, editorial_limits

    # The old 350-character floor could overallocate a 33-row Japanese review.
    units = [{"id": f"D{n}", "item": item("x", text="x" * (70 + n * 7)),
              "restoreFindings": [item("x")] if n % 4 == 0 else []} for n in range(33)]
    result = allocate_editorial_budgets(units, language)
    assert sum(u["replacementBudget"]["maxProseCharacters"] for u in units) == result["allocatedProseCharacters"]
    assert result["allocatedProseCharacters"] < editorial_limits(language, final=True)["maxProseCharacters"]
    assert all(u["replacementBudget"]["maxProseCharacters"] > 0 for u in units)


def test_keep_is_not_a_bypass_for_a_reduced_budget() -> None:
    unit = {"item": item("x", text="x" * 400), "restoreFindings": [],
            "evidence": [evidence("x", "Recorded result", 0)],
            "replacementBudget": {"maxItems": 1, "maxProseCharacters": 350}}
    result = EvidenceResult.model_validate({"decisions": [
        {"itemId": "D0", "action": "keep", "replacements": [], "reason": "Supported but too long"}]})
    with pytest.raises(ValueError, match="editorial budget"):
        result.check({"D0": unit})


@pytest.mark.parametrize("kind", ["weekly", "monthly", "yearly"])
def test_overview_uses_corrected_body_and_citations_after_source_check(tmp_path: Path, kind: Any) -> None:
    original = [item("request", text="The update is not completed.", category="overview"),
                item("request", text="An update was requested.", category="activities")]
    sources = EvidenceSet(evidence={e["id"]: e for e in [
        evidence("request", "Please update the views", 0, "user"),
        evidence("done", "Updated the views. Application UI is untested.", 1)]})
    stages = []

    def generate(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        data = json.loads(prompt.split("BEGIN_REVIEW_INPUT_JSON\n")[1].split("\nEND_REVIEW_INPUT_JSON")[0])
        if "Stage: overview-check" in prompt:
            stages.append("overview")
            assert stages == ["body", "overview"]
            assert data["verifiedItems"][0]["evidenceIds"] == ["e1"]
            assert data["availableEvidenceIds"] == ["e1"]
            category, text = "overview", "View changes were reported; UI remains untested."
        else:
            stages.append("body")
            category, text = "activities", "Assistant reported view changes; UI remains untested."
        return {"decisions": [{"itemId": data["auditUnits"][0]["id"], "action": "replace",
                               "replacements": [item("done", text=text, category=category)],
                               "reason": "Keep the reported result and untested boundary"}]}

    generator = ReviewGenerator(config_for(tmp_path), "overview-last", generate=generate)
    draft = ReviewDraft.model_validate({"items": original, "rawRequests": []})
    period = period_for(kind, date(2026, 5, 19))
    final = audit_review(generator, period, draft, [], sources)
    assert final.items[0].evidenceIds == ["done"]
    assert "untested" in final.items[0].text
    assert generator.quality_report["overviewChecks"] == 1
    assert generator.calls == 2
    assert audit_review(generator, period, draft, [], sources) == final
    assert generator.calls == 2 and generator.cache_hits == 2


def test_overview_rejects_reintroducing_uncited_old_state() -> None:
    from tkn_codex_chat_note.review_quality import _check_overview

    original = item("request", category="overview")
    units = {"D0": {"item": original, "restoreFindings": [], "requireReplacement": True,
                     "evidence": [{"id": "done"}]}}
    result = EvidenceResult.model_validate({"decisions": [
        {"itemId": "D0", "action": "replace", "replacements": [original], "reason": "Old source"}]})
    with pytest.raises(ValueError, match="unavailable"):
        _check_overview(result, units=units)



def test_overview_length_repair_retains_the_verified_body(tmp_path: Path) -> None:
    from tkn_codex_chat_note.review_quality import _audit_overviews

    body = ReviewDraft.model_validate({"items": [item("done", text="Reported completion.")],
                                       "rawRequests": []}).items
    original = {"id": "D0", "item": item("old", category="overview"), "restoreFindings": [],
                "evidence": [evidence("old", "Initial request", 0)],
                "replacementBudget": {"maxItems": 1, "maxProseCharacters": 250}}
    calls = []

    def generate(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        calls.append(prompt)
        if len(calls) == 2:
            data = json.loads(prompt.split("BEGIN_REVIEW_INPUT_JSON\n")[1].split("\nEND_REVIEW_INPUT_JSON")[0])
            assert data["originalInput"]["verifiedItems"][0]["evidenceIds"] == ["e1"]
        return {"decisions": [{"itemId": "D0", "action": "replace", "reason": "Overview",
                               "replacements": [item("done", category="overview",
                                                     text="x" * 300 if len(calls) == 1 else "Reported completion.")]}]}

    generator = ReviewGenerator(config_for(tmp_path), "overview-repair", generate=generate)
    result = _audit_overviews(generator, period_for("weekly", date(2026, 5, 19)), [original], body)
    assert len(calls) == 2
    assert result[0].replacements[0].text == "Reported completion."
