from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from test_reviews import FakeGenerator, config_for, item, seed

from tkn_codex_chat_note.review_comparison import load_previous, previous_period
from tkn_codex_chat_note.review_evidence import checked_json, period_evidence, review_path
from tkn_codex_chat_note.review_generation import ReviewGenerator
from tkn_codex_chat_note.review_model import ReviewDraft, ReviewItem, period_for
from tkn_codex_chat_note.review_quality import EvidenceResult
from tkn_codex_chat_note.review_render import render_review
from tkn_codex_chat_note.reviews import run_reviews, validate_review
from tkn_codex_chat_note.runtime import PipelineError, atomic_write_json


def payload(prompt: str) -> dict[str, Any]:
    return json.loads(prompt.split("BEGIN_REVIEW_INPUT_JSON\n", 1)[1].split("\nEND_REVIEW_INPUT_JSON", 1)[0])


class ComparingGenerator(FakeGenerator):
    def __call__(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        result = super().__call__(prompt, schema)
        data = payload(prompt)
        if "evidence" in data and "auditUnits" not in data:
            result["items"][0].update(category="activities", title="Project export validation",
                                      text="The user reported project export validation progress.")
        if data.get("previousContext"):
            result["items"][0]["evidenceIds"].append(data["previousContext"][0]["id"])
            result["items"][0]["text"] += " This follows the 2026-W20 export report; completion is not confirmed."
        return result


def pair(tmp_path: Path) -> tuple[Any, dict[str, Any], dict[str, Any], ComparingGenerator]:
    config = config_for(tmp_path).enabled_source_configs()[0]
    seed(config, [("2026-05-12T00:00:00Z", "Project export validation started."),
                  ("2026-05-19T00:00:00Z", "Project export validation continued.")])
    model = ComparingGenerator()
    prior = run_reviews(config, kind="weekly", day=date(2026, 5, 12), as_of=date(2026, 6, 1), generate=model)
    current = run_reviews(config, kind="weekly", day=date(2026, 5, 19), as_of=date(2026, 6, 1), generate=model)
    assert prior["generatedCount"] == current["generatedCount"] == 1, (prior, current)
    return config, prior, current, model


@pytest.mark.parametrize(("kind", "day", "expected"), [
    ("weekly", date(2027, 1, 4), "2026-W53"),
    ("monthly", date(2026, 1, 1), "2025-12"),
    ("yearly", date(2026, 1, 1), "2025"),
])
def test_adjacent_period_boundaries(kind: Any, day: date, expected: str) -> None:
    p = period_for(kind, day, -300)
    previous = previous_period(p)
    assert previous.key == expected and previous.end == p.start and previous.offset == -300


def test_comparison_separate_and_no_extra_stage(tmp_path: Path) -> None:
    config, prior, current, model = pair(tmp_path)
    assert prior["modelCalls"] == 3 and current["modelCalls"] == 4
    assert all(any(k in payload(p) for k in ("evidence", "items", "findings", "auditUnits"))
               for p in model.prompts)  # Existing coverage check runs when composition changes the row.
    assert prior["results"][0]["comparison"]["status"] == "missing"
    path = Path(current["results"][0]["jsonPath"])
    value = checked_json(path)
    assert validate_review(path)["ok"]
    refs = value["comparison"]["evidence"]
    assert len(refs) == 1 and refs[0]["basis"] == "reported"
    assert refs[0]["id"] in value["items"][0]["evidenceIds"]
    assert refs[0]["id"] not in {e["id"] for e in value["evidence"]}
    period = period_for("weekly", date(2026, 5, 19))
    assert all(period.contains(t) for e in value["evidence"] for t in e["timestamps"])
    assert len(value["items"]) == 1
    for language, label in (("ja", "前期間の比較資料"), ("en", "Previous-period context")):
        rendered = render_review(value, path, language)
        assert label in rendered and "2026-W20" in rendered
        assert "#s002-c1" in rendered
    for prompt in model.prompts:
        data = payload(prompt)
        if "batchCount" in data:
            assert "previousContext" not in data and "Adjacent-period comparison" not in prompt
    # The source audit sees the historical marker, date and uncertainty, not old raw logs.
    audits = [payload(p) for p in model.prompts if "auditUnits" in payload(p)]
    historical = [e for e in audits[-1]["evidence"] if e.get("kind") == "previous-review"]
    assert historical[0]["scope"] == "comparison-only" and historical[0]["period"]["key"] == "2026-W20"


def test_prior_edit_invalidates_composition_but_reuses_extraction(tmp_path: Path) -> None:
    config, prior, current, _ = pair(tmp_path)
    old = Path(prior["results"][0]["jsonPath"])
    saved = checked_json(old)
    saved["items"][0]["text"] += " Export progress was corrected."
    atomic_write_json(old, saved)
    model = ComparingGenerator()
    result = run_reviews(config, kind="weekly", day=date(2026, 5, 19), as_of=date(2026, 6, 1), generate=model)
    assert result["generatedCount"] == 1 and result["cacheHits"] >= 1
    assert all("batchCount" not in payload(p) for p in model.prompts)
    assert result["results"][0]["comparison"]["sha256"] != current["results"][0]["comparison"]["sha256"]
    no_op = run_reviews(config, kind="weekly", day=date(2026, 5, 19), as_of=date(2026, 6, 1), generate=model)
    assert no_op["results"][0]["status"] == "current" and no_op["modelCalls"] == 0


def test_historical_only_claim_rejected_in_draft_audit_and_saved_note(tmp_path: Path) -> None:
    _, _, current, _ = pair(tmp_path)
    path = Path(current["results"][0]["jsonPath"])
    saved = checked_json(path)
    history = saved["comparison"]["evidence"][0]
    historical_item = item(history["id"])
    with pytest.raises(ValueError, match="current-period"):
        ReviewDraft.model_validate({"items": [historical_item], "rawRequests": []}).validate_sources(
            {history["id"], "now"}, current_ids={"now"})
    result = EvidenceResult.model_validate({"decisions": [
        {"itemId": "D0", "action": "keep", "replacements": [], "reason": "Old only"}]})
    with pytest.raises(ValueError, match="current-period"):
        result.check({"D0": {"item": historical_item, "restoreFindings": [], "evidence": [history]}})
    saved["items"][0]["evidenceIds"] = [history["id"]]
    atomic_write_json(path, saved)
    with pytest.raises(ValueError, match="current-period"):
        validate_review(path)


def test_comparison_validation_does_not_relax_period_validation(tmp_path: Path) -> None:
    _, _, current, _ = pair(tmp_path)
    path = Path(current["results"][0]["jsonPath"])
    saved = checked_json(path)
    saved["evidence"][0]["timestamps"] = ["2026-05-12T00:00:00Z"]
    atomic_write_json(path, saved)
    with pytest.raises(PipelineError, match="outside the period"):
        validate_review(path)


def test_historical_narrative_not_promoted_to_monthly(tmp_path: Path) -> None:
    config, _, current, _ = pair(tmp_path)
    value = checked_json(Path(current["results"][0]["jsonPath"]))
    result = period_evidence(config, period_for("monthly", date(2026, 5, 1)))
    ids = {e["id"] for e in value["comparison"]["evidence"]}
    assert not ids.intersection(result.evidence)
    assert all("This follows" not in r["text"] for r in result.input_records())
    assert any("validation continued" in r["text"] for r in result.input_records())


def test_missing_disabled_and_wrong_source(tmp_path: Path) -> None:
    config, prior, _, _ = pair(tmp_path)
    period = period_for("weekly", date(2026, 5, 19))
    config.reviews.previous_period = False
    assert load_previous(config, period).metadata["status"] == "disabled"
    config.reviews.previous_period = True
    path = Path(prior["results"][0]["jsonPath"])
    saved = checked_json(path)
    saved["sourceId"] = "other-source"
    atomic_write_json(path, saved)
    previous = load_previous(config, period)
    assert previous.metadata["status"] == "unavailable" and not previous.candidates


def test_selection_budget_and_empty_current_cannot_resurrect_prior(tmp_path: Path) -> None:
    config, _, _, _ = pair(tmp_path)
    period = period_for("weekly", date(2026, 5, 19))
    previous = load_previous(config, period)
    evidence = period_evidence(config, period)
    findings = [ReviewItem.model_validate(item(evidence.selected[0], text="Project export validation"))]
    previous.select(findings, evidence, 50)
    assert not previous.selected  # Never truncate a caveat just to fit.
    previous.select([], evidence, 6000)
    assert not previous.selected
    config.reviews.previous_period = False
    generator = ReviewGenerator(config, "no-previous")
    first = generator.extraction_signature
    config.reviews.previous_period = True
    assert ReviewGenerator(config, "with-previous").extraction_signature == first


def test_yearly_uses_previous_year_only(tmp_path: Path) -> None:
    config = config_for(tmp_path).enabled_source_configs()[0]
    period = period_for("yearly", date(2026, 1, 1))
    previous = load_previous(config, period)
    assert previous.metadata["period"]["key"] == "2025"
    assert not review_path(config.review_data_root, previous_period(period)).exists()


@pytest.mark.parametrize(("kind", "older", "newer"), [
    ("monthly", date(2025, 5, 12), date(2025, 6, 19)),
    ("yearly", date(2024, 5, 12), date(2025, 6, 19)),
])
def test_higher_periods_use_adjacent_note(tmp_path: Path, kind: Any, older: date, newer: date) -> None:
    config = config_for(tmp_path).enabled_source_configs()[0]
    seed(config, [(older.isoformat() + "T00:00:00Z", "Project export validation started."),
                  (newer.isoformat() + "T00:00:00Z", "Project export validation continued.")])
    model = ComparingGenerator()
    for day in (older, newer):
        result = run_reviews(config, kind=kind, day=day, as_of=date(2026, 6, 1), generate=model)
        assert result["generatedCount"] == 1, result
    saved = checked_json(Path(result["results"][0]["jsonPath"]))
    assert saved["comparison"]["period"] == period_for(kind, older).document()
    assert saved["comparison"]["selectedCount"] == 1
    assert validate_review(Path(result["results"][0]["jsonPath"]))["ok"]
