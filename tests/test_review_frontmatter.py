"""Metadata types and presentation-only publication safety."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import yaml
from test_reviews import FakeGenerator, config_for, seed, weekly

from tkn_codex_chat_note.review_frontmatter import render_frontmatter
from tkn_codex_chat_note.review_model import period_for
from tkn_codex_chat_note.runtime import atomic_write_json


@pytest.mark.parametrize("kind", ["weekly", "monthly", "yearly"])
@pytest.mark.parametrize("stamp", ["2026-10-07T00:30:00.123456789Z", "2026-10-07T09:30:00+09:00",
                                   "2026-10-07T00:30:00-04:00"])
def test_flat_metadata_preserves_dates_types_and_identity(tmp_path, kind, stamp):
    value = {
        "id": "existing-id", "sourceId": "example", "generatedAt": stamp,
        "period": period_for(kind, date(2026, 10, 7)).document(),
        "coverage": {"status": "partial"}, "reviewStatus": "unreviewed", "inputFingerprint": "abc",
        "items": [{"category": "overview", "text": "A decision.\n\nReported, not verified.",
                   "caveat": "Limited evidence."}],
        "generation": {"model": None, "provider": "codex", "profile": "on", "settingsSha256": "00123"},
    }
    text = render_frontmatter(value, tmp_path / "review.json", 'Title: "test" # literal\n---\ninjected: yes')
    header = yaml.safe_load(text.split("---\n", 1)[1].rsplit("\n---", 1)[0])
    assert header["type"] == "chatReview" and "type: chatReview\n" in text
    assert header["schemaVersion"] == "3.0.0" and 'schemaVersion: "3.0.0"' in text
    assert header["noteId"] == value["id"] and "id" not in header
    assert header["generatedAt"] == stamp and f'generatedAt: "{stamp}"' in text
    assert header["periodStart"] == value["period"]["start"]
    assert header["utcOffsetMinutes"] == 540 and type(header["utcOffsetMinutes"]) is int
    assert header["requestedModel"] is None and "requestedModel:\n" in text
    assert header["generationProfile"] == "on" and header["generationSettingsSha256"] == "00123"
    assert header["source"] == (tmp_path / "review.json").as_uri()
    assert header["description"] == "A decision. Reported, not verified. Limited evidence."
    assert header["sourceApplication"] == "Codex"
    assert header["created"] is None and header["updated"] == stamp
    assert text.splitlines()[-4:-1] == ["created:", f'updated: "{stamp}"', 'noteId: "existing-id"']
    assert "injected" not in header
    assert all(v is None or type(v) in (str, int) for v in header.values())
    value["items"] = []
    assert 'description: ""' in render_frontmatter(value, tmp_path / "review.json", "Empty")


@pytest.mark.parametrize("path", ["C:/path/to/it's.json", r"C:\path\to\it's.json", r"\\server\share\file.json"])
def test_windows_paths_are_single_quoted(path):
    from tkn_codex_chat_note.review_frontmatter import _scalar

    encoded = _scalar(path)
    assert encoded.startswith("'") and yaml.safe_load(encoded) == path


def prepare_old_presentation(tmp_path):
    cfg = config_for(tmp_path)
    seed(cfg, [("2026-05-19T00:00:00Z", "Question")])
    model = FakeGenerator()
    result = weekly(cfg, model)
    json_path = Path(result["results"][0]["jsonPath"])
    index_path = cfg.enabled_source_configs()[0].review_state_root / "index.json"
    index = json.loads(index_path.read_text("utf-8"))
    index["reviews"]["weekly/2026-W21"].pop("presentationSignature")
    atomic_write_json(index_path, index)
    return cfg, model, json_path, index_path


def test_metadata_refresh_is_read_only_on_dry_run_and_uses_no_inference(tmp_path):
    cfg, model, path, index_path = prepare_old_presentation(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    plan = weekly(cfg, model, dry_run=True)
    assert plan["plannedCount"] == 1 and plan["modelCalls"] == 0
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    old_json = path.read_bytes()
    body = path.with_suffix(".md").read_text("utf-8").split("\n---\n", 1)[1]
    result = weekly(cfg, model)
    assert result["renderedCount"] == 1 and result["generatedCount"] == result["modelCalls"] == 0
    assert path.read_bytes() == old_json
    assert path.with_suffix(".md").read_text("utf-8").split("\n---\n", 1)[1] == body
    assert len(model.prompts) == 3
    again = weekly(cfg, model)
    assert again["results"][0]["status"] == "current" and again["modelCalls"] == 0
    assert json.loads(index_path.read_text("utf-8"))["reviews"]["weekly/2026-W21"]["presentationSignature"]
    backups = cfg.enabled_source_configs()[0].review_state_root / "backups"
    assert list(backups.rglob("*.md"))


@pytest.mark.parametrize("reviewed", [False, True])
def test_presentation_refresh_preserves_manual_edits_and_reviewed_notes(tmp_path, reviewed):
    from hashlib import sha256

    cfg, model, path, index_path = prepare_old_presentation(tmp_path)
    md = path.with_suffix(".md")
    text = md.read_text("utf-8")
    if reviewed:
        text = text.replace('reviewStatus: "unreviewed"', 'reviewStatus: "accepted"')
    else:
        text += "\nUser annotation\n"
    md.write_bytes(text.encode("utf-8"))
    if reviewed:
        # Even owned/hash-matching accepted output must never be reformatted.
        index = json.loads(index_path.read_text("utf-8"))
        index["reviews"]["weekly/2026-W21"]["hashes"][".md"] = sha256(md.read_bytes()).hexdigest()
        atomic_write_json(index_path, index)
    before = md.read_bytes()
    result = weekly(cfg, model, allow_edited=True)
    assert result["results"][0]["status"] == "preserved"
    assert md.read_bytes() == before and result["modelCalls"] == 0


def test_note_lifecycle_retains_creation_and_only_updates_changed_content(tmp_path, monkeypatch):
    cfg = config_for(tmp_path)
    seed(cfg, [("2026-05-19T00:00:00Z", "Question")])
    model = FakeGenerator()
    monkeypatch.setattr("tkn_codex_chat_note.reviews.now_iso", lambda: "2026-06-01T10:00:00+09:00")
    result = weekly(cfg, model)
    path = Path(result["results"][0]["jsonPath"])
    original = json.loads(path.read_text("utf-8"))
    assert original["created"] == original["updated"] == original["generatedAt"]
    monkeypatch.setattr("tkn_codex_chat_note.reviews.now_iso", lambda: "2026-06-02T10:00:00+09:00")
    weekly(cfg, model, force=True)
    unchanged = json.loads(path.read_text("utf-8"))
    assert unchanged["created"] == unchanged["updated"] == original["created"]
    assert unchanged["generatedAt"] != unchanged["updated"]
    seed(cfg, [("2026-05-19T00:00:00Z", "Corrected question")])
    weekly(cfg, model)
    revised = json.loads(path.read_text("utf-8"))
    assert revised["created"] == original["created"]
    assert revised["updated"] == revised["generatedAt"] != original["updated"]


def test_request_template_change_triggers_generation_even_with_same_inputs(tmp_path, monkeypatch):
    from tkn_codex_chat_note.review_profiles import ReviewProfile

    cfg, model, path, _ = prepare_old_presentation(tmp_path)
    read = ReviewProfile.text

    def changed(profile, name):
        return read(profile, name) + ("\nExtra generation instruction." if name == "request.template.md" else "")

    monkeypatch.setattr(ReviewProfile, "text", changed)
    result = weekly(cfg, model)
    assert result["generatedCount"] == 1 and result["modelCalls"] > 0
    assert weekly(cfg, model)["results"][0]["status"] == "current"


@pytest.mark.parametrize("language", ["ja", "en"])
def test_external_markdown_template_controls_output_without_model_calls(tmp_path, monkeypatch, language):
    from tkn_codex_chat_note.review_profiles import ReviewProfile

    cfg = config_for(tmp_path, language=language)
    seed(cfg, [("2026-05-19T00:00:00Z", "Question")])
    model = FakeGenerator()
    result = weekly(cfg, model)
    path = Path(result["results"][0]["jsonPath"])
    before_json = path.read_bytes()
    read = ReviewProfile.text

    def changed(profile, name):
        return read(profile, name) + ("\nTemplate edit marker\n" if name == "template.md" else "")

    monkeypatch.setattr(ReviewProfile, "text", changed)
    refreshed = weekly(cfg, model)
    assert refreshed["renderedCount"] == 1 and refreshed["modelCalls"] == 0
    assert "Template edit marker" in path.with_suffix(".md").read_text("utf-8")
    assert path.read_bytes() == before_json
    assert weekly(cfg, model)["results"][0]["status"] == "current"
