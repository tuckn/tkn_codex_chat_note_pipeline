"""Empty source periods are not review work; unavailable evidence is an error."""
from __future__ import annotations

import json
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from test_reviews import FakeGenerator, config_for, seed

from tkn_codex_chat_note.reviews import run_reviews
from tkn_codex_chat_note.runtime import PipelineError, atomic_write_json


@pytest.mark.parametrize("kind", ["weekly", "monthly", "yearly"])
def test_empty_period_creates_no_note_or_model_calls(tmp_path: Path, kind: Any) -> None:
    config = config_for(tmp_path)
    seed(config, [("2025-05-19T00:00:00Z", "Earlier activity")])
    model = FakeGenerator()
    result = run_reviews(config, kind=kind, day=date(2026, 10, 5), as_of=date(2027, 1, 2), generate=model)
    assert result["ok"] and result["complete"] and result["pendingPeriods"] == 0
    assert result["skippedEmptyCount"] == 1 and result["generatedCount"] == 0
    assert result["results"][0]["status"] == "skipped-empty"
    assert not model.prompts and result["modelCalls"] == 0
    assert not list(config.review_data_root.rglob("*.md"))
    assert not list(config.review_data_root.rglob("*.json"))


def test_gap_and_trailing_periods_do_not_block_completion(tmp_path: Path) -> None:
    config = config_for(tmp_path, max_calls=100)
    seed(config, [("2026-05-05T00:00:00Z", "First active week"),
                  ("2026-05-19T00:00:00Z", "Last active week")])
    model = FakeGenerator()
    first = run_reviews(config, as_of=date(2026, 6, 1), generate=model)
    assert first["ok"] and first["complete"] and first["pendingPeriods"] == 0
    assert first["generatedCount"] == 3  # two active weeks and their month
    assert first["skippedEmptyCount"] == 2
    again = run_reviews(config, as_of=date(2026, 7, 6), generate=model)
    assert again["ok"] and again["complete"] and again["pendingPeriods"] == 0
    assert again["modelCalls"] == 0 and again["generatedCount"] == 0
    assert again["skippedEmptyCount"] > first["skippedEmptyCount"]
    assert len(list(config.review_data_root.rglob("*.md"))) == 3


def test_after_budget_counts_only_real_unfinished_work(tmp_path: Path) -> None:
    config = config_for(tmp_path, max_calls=100, previous_period=False)
    seed(config, [("2026-05-19T00:00:00Z", "Unfinished week"),
                  ("2026-05-26T00:00:00Z", "Already generated later week")])
    generated = run_reviews(config, kind="weekly", day=date(2026, 5, 26),
                            as_of=date(2026, 6, 8), generate=FakeGenerator())
    assert generated["generatedCount"] == 1
    config.reviews.max_calls = 1
    result = run_reviews(config, as_of=date(2026, 6, 8), generate=FakeGenerator())
    assert result["ok"] and not result["complete"]
    statuses = {(r["period"]["kind"], r["period"]["key"]): r["status"] for r in result["results"]}
    assert statuses == {("weekly", "2026-W21"): "deferred", ("weekly", "2026-W22"): "current",
                        ("monthly", "2026-05"): "pending", ("weekly", "2026-W23"): "skipped-empty"}
    assert result["pendingPeriods"] == 2 and result["skippedEmptyCount"] == 1
    assert result["modelCalls"] == 1 and result["plannedCount"] == 0
    # Only the pre-existing later week has a published note.
    assert len(list(config.review_data_root.rglob("*.md"))) == 1


def test_dry_run_counts_only_nonempty_plans_and_writes_nothing(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    seed(config, [("2026-05-19T00:00:00Z", "Question")])
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    model = FakeGenerator()
    result = run_reviews(config, as_of=date(2026, 6, 8), dry_run=True, generate=model)
    assert result["ok"] and result["complete"]
    assert result["plannedCount"] == result["pendingPeriods"] == 2
    assert result["skippedEmptyCount"] == 2
    assert not model.prompts
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


@pytest.mark.parametrize("force", [False, True])
def test_existing_empty_notes_are_preserved(tmp_path: Path, force: bool) -> None:
    config = config_for(tmp_path)
    seed(config, [("2026-05-19T00:00:00Z", "Earlier question")])
    folder = config.review_data_root / "weekly/2026"
    folder.mkdir(parents=True)
    for suffix in (".json", ".md"):
        (folder / ("2026-W41" + suffix)).write_text("Existing human-edited artifact", encoding="utf-8")
    before = {p: p.read_bytes() for p in folder.iterdir()}
    result = run_reviews(config, kind="weekly", day=date(2026, 10, 5), as_of=date(2026, 10, 25),
                         force=force, allow_edited=True, generate=FakeGenerator())
    assert result["complete"] and result["skippedEmptyCount"] == 1
    assert before == {p: p.read_bytes() for p in folder.iterdir()}


@pytest.mark.parametrize("problem", ["missing", "hash", "json", "failed", "timestamp", "permission", "shape"])
def test_unavailable_source_is_not_reported_as_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, problem: str,
) -> None:
    import tkn_codex_chat_note.review_evidence as evidence

    config = config_for(tmp_path)
    seed(config, [("2026-05-19T00:00:00Z", "Question")])
    catalog_path = config.data_root / "catalog/threads.json"
    catalog = json.loads(catalog_path.read_text("utf-8"))
    entry = catalog["threads"][0]
    path = config.data_root / entry["canonicalRef"].removeprefix("data:/")
    if problem == "missing":
        path.unlink()
    elif problem == "hash":
        path.write_text("tampered", encoding="utf-8")
    elif problem == "failed":
        entry["status"] = "failed"
        entry["reason"] = "normalization failed"
    elif problem == "permission":
        original = evidence.checked_json

        def unreadable(target: Path, expected: str | None = None) -> dict[str, Any]:
            if target == path:
                raise PermissionError("snapshot cannot be read")
            return original(target, expected)

        monkeypatch.setattr(evidence, "checked_json", unreadable)
    else:
        value = json.loads(path.read_text("utf-8"))
        if problem == "timestamp":
            value["events"][0]["timestamp"] = "not-a-date"
        elif problem == "shape":
            value["events"] = None
        path.write_text("not json" if problem == "json" else json.dumps(value), encoding="utf-8")
        entry["canonicalSha256"] = sha256(path.read_bytes()).hexdigest()
    atomic_write_json(catalog_path, catalog)
    model = FakeGenerator()
    with pytest.raises(PipelineError, match="not classified as empty"):
        run_reviews(config, as_of=date(2026, 6, 1), generate=model)
    assert not model.prompts
    assert not list(config.review_data_root.rglob("*.md"))


def test_blank_messages_are_not_review_work(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    seed(config, [("2026-05-19T00:00:00Z", "   ")])
    result = run_reviews(config, as_of=date(2026, 6, 1), generate=FakeGenerator())
    assert result["complete"] and result["results"] == [] and result["pendingPeriods"] == 0


def test_undated_messages_are_reported_separately_from_empty_history(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    seed(config, [("", "Historical message without a timestamp")])
    model = FakeGenerator()
    result = run_reviews(config, as_of=date(2026, 6, 1), generate=model)
    assert not result["ok"] and not result["complete"]
    assert result["undatedEventCount"] == 1
    assert result["historyIssues"][0]["reason"] == "undated-events"
    assert result["pendingPeriods"] == 0 and result["skippedEmptyCount"] == 0
    assert not model.prompts and not list(config.review_data_root.rglob("*.md"))


def test_dated_work_continues_with_undated_messages(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    seed(config, [("", "Undated old message"), ("2026-05-19T00:00:00Z", "Dated question")])
    model = FakeGenerator()
    result = run_reviews(config, as_of=date(2026, 5, 25), generate=model)
    assert result["generatedCount"] == 1 and result["pendingPeriods"] == 0
    assert result["undatedEventCount"] == 1 and not result["complete"] and not result["ok"]
    assert model.prompts


def test_exact_call_limit_with_only_empty_periods_left_is_complete(tmp_path: Path) -> None:
    config = config_for(tmp_path, max_calls=3, previous_period=False)
    seed(config, [("2026-05-05T00:00:00Z", "Only active week")])
    result = run_reviews(config, as_of=date(2026, 5, 25), generate=FakeGenerator())
    assert result["modelCalls"] == 3 and result["generatedCount"] == 1
    assert result["skippedEmptyCount"] == 2 and result["pendingPeriods"] == 0
    assert result["ok"] and result["complete"]


def test_cli_does_not_claim_completion_for_undated_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    import tkn_codex_chat_note.cli as cli

    config = config_for(tmp_path)
    seed(config, [("", "Undated message")])
    monkeypatch.setattr(cli, "load_app_config", lambda **_: config)
    assert cli.main(["--json", "review-notes", "due", "--full-output"]) == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["pendingPeriods"] == 0 and report["undatedEventCount"] == 1
    assert "No dated review periods are pending" in captured.err
    assert "rerunning alone cannot resolve missing timestamps" in captured.err
    assert captured.err.count("[ERROR]") == 1
    assert captured.err.count("have no timestamp") == 1
    assert "Some processing failed or was protected" not in captured.err
    assert "Review generation complete" not in captured.err


def test_failed_evidence_preparation_is_not_an_empty_period(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tkn_codex_chat_note.reviews as reviews
    from tkn_codex_chat_note.review_evidence import EvidenceSet

    config = config_for(tmp_path)
    seed(config, [("2026-05-19T00:00:00Z", "Saved question")])
    monkeypatch.setattr(reviews, "period_evidence", lambda *_: EvidenceSet())
    result = run_reviews(config, as_of=date(2026, 5, 25), generate=FakeGenerator())
    assert not result["ok"] and not result["complete"]
    assert result["results"][0]["status"] == "failed" and result["pendingPeriods"] == 1
    assert result["skippedEmptyCount"] == 0 and result["modelCalls"] == 0
