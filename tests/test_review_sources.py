"""End-to-end contracts for source-local reviews without Session Note generation."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from chat_fixtures import write_chat

from tkn_codex_chat_note.cli import build_parser
from tkn_codex_chat_note.config import AppConfig
from tkn_codex_chat_note.pipeline import run_pipeline
from tkn_codex_chat_note.provenance import validate_provenance
from tkn_codex_chat_note.review_evidence import period_evidence
from tkn_codex_chat_note.review_model import period_for
from tkn_codex_chat_note.reviews import run_reviews, run_scheduled, validate_review


def configured(root: Path, *names: str) -> AppConfig:
    return AppConfig(sources={name: {
        "source_root": root / name / "input", "raw_root": root / name / "raw",
        "data_root": root / name / "data", "state_root": root / name / "state",
        "reviews": {"start_date": date(2026, 5, 18)},
    } for name in names}, cache_root=root / "cache",
        generation={"profiles": {"codex": {"provider": "codex", "model": "test"}}})


def response(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(prompt.split("BEGIN_REVIEW_INPUT_JSON\n")[1].split("\nEND_REVIEW_INPUT_JSON")[0])
    if "items" in payload:
        return {"items": payload["items"], "rawRequests": []}
    evidence = payload["evidence"][0]
    return {"items": [{"category": "questions", "title": "Recorded request", "text": evidence["text"],
                       "basis": "reported", "evidenceIds": [evidence["id"]], "caveat": "", "nextCheck": "",
                       "intelligenceKind": "none"}], "rawRequests": []}


def test_capture_and_normalization_do_not_infer_or_touch_existing_notes(tmp_path: Path,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    import tkn_codex_chat_note.api_inference as api
    import tkn_codex_chat_note.inference as inference

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("capture/normalization must not call a model")

    monkeypatch.setattr(api, "ApiClient", forbidden)
    monkeypatch.setattr(inference, "invoke_structured", forbidden)
    config = configured(tmp_path, "pc")
    source = config.enabled_source_configs()[0]
    log = source.sessions_root / "one.jsonl"
    write_chat(log, thread_id="one", cwd=tmp_path, request="Original request", started_at="2026-05-19T00:00:00Z")
    original = log.read_bytes()
    result = run_pipeline(config, mode="clone")
    assert result["complete"] and result["normalizedThreadCount"] == 1
    assert not (source.data_root / "session-notes").exists()
    assert not (source.state_root / "ledger.json").exists()
    assert validate_provenance(source.data_root)["ok"]
    old_note = source.data_root / "session-notes/user-edited.md"
    old_note.parent.mkdir()
    old_note.write_bytes(b"Human edits and outdated assumptions, preserve exactly.")
    before = old_note.read_bytes()
    updated = run_pipeline(config, mode="pull")
    assert updated["complete"] and old_note.read_bytes() == before
    assert (source.raw_root / "sessions/one.jsonl").read_bytes() == original
    assert "noteRef" not in updated["threads"][0]
    assert list(source.data_root.rglob("*.md")) == [old_note]
    evidence = period_evidence(source, period_for("weekly", date(2026, 5, 19)))
    assert {e["kind"] for e in evidence.input_records()} == {"canonical-excerpt"}
    assert "Original request" in json.dumps(evidence.input_records())
    assert "outdated assumptions" not in json.dumps(evidence.input_records())
    assert not any("Session Note" in gap for gap in evidence.gaps)


def test_two_sources_produce_independent_reviews_and_state(tmp_path: Path) -> None:
    config = configured(tmp_path, "pc", "wsl")
    for source in config.enabled_source_configs():
        write_chat(source.sessions_root / "one.jsonl", thread_id="same-thread", cwd=tmp_path,
                   request=source.source_id + " exclusive request", started_at="2026-05-19T00:00:00Z")
    assert run_pipeline(config, mode="clone")["complete"]
    result = run_reviews(config, kind="weekly", day=date(2026, 5, 19), as_of=date(2026, 5, 25), generate=response)
    assert result["ok"] and result["generatedCount"] == 2
    ids = set()
    for source, report in zip(config.enabled_source_configs(), result["sourceResults"], strict=True):
        note = source.review_data_root / "weekly/2026/2026-W21.json"
        value = json.loads(note.read_text(encoding="utf-8"))
        ids.add(value["id"])
        assert value["sourceId"] == report["sourceId"] == source.source_id
        assert {e["sourceId"] for e in value["evidence"]} == {source.source_id}
        assert source.source_id + " exclusive request" in json.dumps(value["items"])
        assert (source.review_state_root / "index.json").is_file()
        assert validate_review(note)["ok"]
    assert len(ids) == 2
    again = run_reviews(config, kind="weekly", day=date(2026, 5, 19), as_of=date(2026, 5, 25), generate=response)
    assert again["modelCalls"] == 0 and again["generatedCount"] == 0


def test_scheduled_run_initializes_normalizes_and_generates_due_review(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    import tkn_codex_chat_note.reviews as reviews
    from tkn_codex_chat_note.review_generation import ReviewGenerator

    config = configured(tmp_path, "pc")
    source = config.enabled_source_configs()[0]
    write_chat(source.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path,
               request="Scheduled review", started_at="2026-05-19T00:00:00Z")
    original = ReviewGenerator.__init__

    def fake(self: ReviewGenerator, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **{**kwargs, "generate": response})

    monkeypatch.setattr(ReviewGenerator, "__init__", fake)
    monkeypatch.setattr(reviews, "review_today", lambda _: date(2026, 5, 25))
    result = run_scheduled(config)
    assert result["ok"] and result["capture"]["normalizedThreadCount"] == 1
    assert result["reviews"]["generatedCount"] == 1
    assert not (source.data_root / "session-notes").exists()


def test_catalog_rows_from_another_source_are_not_reviewed(tmp_path: Path) -> None:
    config = configured(tmp_path, "pc")
    source = config.enabled_source_configs()[0]
    write_chat(source.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path,
               request="Must be excluded after source identity changes", started_at="2026-05-19T00:00:00Z")
    run_pipeline(config, mode="clone")
    path = source.data_root / "catalog/threads.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    catalog["threads"][0]["sourceId"] = "other-pc"
    path.write_text(json.dumps(catalog), encoding="utf-8")
    evidence = period_evidence(source, period_for("weekly", date(2026, 5, 19)))
    assert evidence.input_records() == []


def test_wrong_source_weekly_review_falls_back_to_local_events(tmp_path: Path) -> None:
    config = configured(tmp_path, "pc")
    source = config.enabled_source_configs()[0]
    write_chat(source.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path,
               request="Local evidence", started_at="2026-05-19T00:00:00Z")
    run_pipeline(config, mode="clone")
    run_reviews(config, kind="weekly", day=date(2026, 5, 19), as_of=date(2026, 5, 25), generate=response)
    path = source.review_data_root / "weekly/2026/2026-W21.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["sourceId"] = "other-pc"
    value["items"][0]["text"] = "Foreign interpretation"
    path.write_text(json.dumps(value), encoding="utf-8")
    evidence = period_evidence(source, period_for("monthly", date(2026, 5, 19)))
    assert str(path) not in evidence.weekly_inputs
    assert "Foreign interpretation" not in json.dumps(evidence.input_records())
    assert "Local evidence" in json.dumps(evidence.input_records())
    assert any("2026-W21" in gap and "unavailable" in gap for gap in evidence.gaps)


@pytest.mark.parametrize("args", [
    ["session-notes", "build"], ["session-notes", "validate", "note.md"],
    ["--session-note-profile", "default-jp", "pull"], ["pull", "--limit", "10"],
    ["--idle-minutes", "30", "pull"],
])
def test_removed_generation_commands_are_rejected(args: list[str]) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(args)


def test_top_level_review_and_removed_source_settings_are_rejected(tmp_path: Path) -> None:
    config = configured(tmp_path, "pc").model_dump(mode="python")
    with pytest.raises(ValueError, match="reviews"):
        AppConfig.model_validate({**config, "reviews": {}})
    for field in ("session_note_limit", "data_root", "state_root"):
        config["sources"]["pc"]["reviews"] = {field: 10}
        with pytest.raises(ValueError, match=field):
            AppConfig.model_validate(config)
