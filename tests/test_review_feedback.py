"""Source names, precise citation scope and reusable explanation contracts."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from chat_fixtures import write_chat
from test_reviews import FakeGenerator, config_for, item, seed, weekly

from tkn_codex_chat_note.chat_logs import clean_user_text, read_thread_source
from tkn_codex_chat_note.chat_titles import load_chat_titles
from tkn_codex_chat_note.pipeline import run_pipeline
from tkn_codex_chat_note.review_evidence import period_evidence, raw_supplement
from tkn_codex_chat_note.review_model import period_for
from tkn_codex_chat_note.review_profiles import ReviewProfile
from tkn_codex_chat_note.review_render import render_review
from tkn_codex_chat_note.review_sources import citation_view, coverage_groups

ADMIN = '<external_codex_apps_open_page>{"page_id":null}</external_codex_apps_open_page>'


def test_management_events_removed_but_requests_and_quoted_examples_survive(tmp_path):
    assert clean_user_text(ADMIN) == ""
    assert clean_user_text(ADMIN + "\nActual request") == "Actual request"
    quoted = "Explain this tag:\n```xml\n" + ADMIN + "\n```"
    assert clean_user_text(quoted) == quoted
    cfg = config_for(tmp_path)
    source = cfg.enabled_source_configs()[0]
    log = source.sessions_root / "one.jsonl"
    write_chat(log, thread_id="one", cwd=tmp_path, request=ADMIN + "\nActual request",
               started_at="2026-05-19T00:00:00Z")
    before = log.read_bytes()
    parsed = read_thread_source(log)
    assert parsed.thread_log.user_messages[0].text == "Actual request"
    index = source.codex_home / "session_index.jsonl"
    index.write_text(json.dumps({"id": "one", "thread_name": "Actual chat title"}) + "\n", encoding="utf-8")
    run_pipeline(cfg, mode="clone")
    catalog = json.loads((source.data_root / "catalog/threads.json").read_text("utf-8"))
    assert catalog["threads"][0]["title"] == "Actual chat title"
    assert catalog["threads"][0]["titleSource"] == "session-index"
    assert log.read_bytes() == before
    assert ADMIN in json.loads(before.decode().splitlines()[2])["payload"]["content"][0]["text"]


def test_optional_session_index_handles_missing_invalid_and_renamed_titles(tmp_path):
    path = tmp_path / "session_index.jsonl"
    assert load_chat_titles(path) == {}
    rows = ["not json", "[]", json.dumps({"id": "a", "thread_name": "Old title"}),
            json.dumps({"id": "a", "thread_name": "Renamed title"}),
            json.dumps({"id": "bad", "thread_name": ADMIN})]
    path.write_text("\n".join(rows), encoding="utf-8")
    assert load_chat_titles(path) == {"a": "Renamed title"}


def test_legacy_canonical_and_bounded_raw_skip_admin_and_keep_true_speakers(tmp_path):
    cfg = config_for(tmp_path)
    seed(cfg, [("2026-05-19T00:00:00Z", ADMIN), ("2026-05-19T00:01:00Z", "Real question")])
    source = cfg.enabled_source_configs()[0]
    period = period_for("weekly", date(2026, 5, 19))
    evidence = period_evidence(source, period)
    assert len(evidence.selected) == 1 and ADMIN not in json.dumps(evidence.input_records())
    assert evidence.evidence[evidence.selected[0]]["title"] == "Test note"
    supplement = raw_supplement(source, evidence, evidence.selected, period, 10000)
    assert ADMIN not in json.dumps(supplement.input_records())
    assert {e["actor"] for e in supplement.evidence.values()} == {"user"}
    # A real normalization fixture supplies a neighboring assistant and tool output.
    other = config_for(tmp_path / "other")
    other_source = other.enabled_source_configs()[0]
    write_chat(other_source.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path,
               request="Question", started_at="2026-05-19T00:00:00Z")
    run_pipeline(other, mode="clone")
    originals = period_evidence(other_source, period)
    asked = next(id for id in originals.selected if originals.evidence[id]["actor"] == "user")
    raw = raw_supplement(other_source, originals, [asked], period, 10000)
    assert {e["actor"] for e in raw.evidence.values()} >= {"user", "assistant", "tool"}


@pytest.mark.parametrize("language", ["ja", "en"])
def test_citations_target_exact_subset_once_and_filter_administrative_support(tmp_path, language):
    cfg = config_for(tmp_path, language=language)
    seed(cfg, [("2026-05-19T00:00:00Z", "Question"),
               ("2026-05-19T00:01:00Z", "Answer: portable is separate from local.")])
    run = weekly(cfg, FakeGenerator())
    path = Path(run["results"][0]["jsonPath"])
    value = json.loads(path.read_text("utf-8"))
    e1, e2 = value["evidence"][:2]
    for e in value["evidence"]:
        e["title"] = ADMIN[:100]
    e2["actor"] = "assistant"
    admin = {**e1, "id": "admin-id", "text": ADMIN}
    value["evidence"].append(admin)
    question = item(e1["id"], text="Question and useful answer; understanding unconfirmed.")
    question["evidenceIds"] = [e1["id"], e2["id"], "admin-id"]
    value["items"] = [question, item(e2["id"], category="activities")]
    items, sources = citation_view(value)
    assert items[0]["refs"] == "[S001](#s001-c1)" and items[1]["refs"] == "[S001](#s001-c2)"
    assert items[0]["uncitableEvidenceIds"] == ["admin-id"]
    assert [r["id"] for r in sources[0]["citations"][0]["records"]] == [e1["id"], e2["id"]]
    assert [r["id"] for r in sources[0]["citations"][1]["records"]] == [e2["id"]]
    assert sources[0]["name"] == "Question" and len(sources[0]["records"]) == 2
    text = render_review(value, path, language)
    assert '<a id="s001-c1"></a>' in text and '<a id="s001-c2"></a>' in text
    assert text.count('<a id="s001-e002"></a>') == 1 and "admin-id" not in text
    assert "2026-05-19T00:01:00Z" in text and "L000002" in text
    assert '<external_codex_apps_open_page>' not in text and "<details>" in text
    assert "Question and useful answer; understanding unconfirmed." in text
    assert [line for line in text.splitlines() if line.startswith("## ")] == (
        ["## 質問・学び", "## テーマ別の実施・判断・残る点", "## 対象範囲と出典"] if language == "ja" else
        ["## Questions and learning", "## Activities, decisions and open questions", "## Coverage and sources"])


def test_limitation_kinds_do_not_confuse_compaction_with_missing_conversations():
    gaps = ["a: routine tool bodies compacted"] * 49 + ["a: excerpt truncated"] * 15
    gaps += ["E1/L2: Raw excerpt truncated", "Raw character budget exhausted", "a: failed normalization"]
    counts = {g["kind"]: g["count"] for g in coverage_groups(gaps)}
    assert counts == {"tool-compaction": 49, "excerpt-truncation": 16,
                      "raw-character-limit": 1, "normalization-failure": 1}
    assert sum(counts.values()) == 67
    assert coverage_groups(["2026-W21: unknown new diagnostic"]) == [{"kind": "other", "count": 1}]
    assert coverage_groups(["2026-05: Raw request limit reached"])[0]["kind"] == "raw-request-limit"


@pytest.mark.parametrize("language", ["ja", "en"])
def test_all_generation_stages_preserve_reusable_answers_with_status_and_citations(language):
    profile = ReviewProfile(language)
    for file in ("prompt.md", "synthesis-prompt.md", "quality-prompt.md"):
        text = profile.text(file)
        assert "Reusable question/learning content" in text
        assert "actual answer or explanation takeaway in 1-2" in text
        assert "Cite\nsupplied answer/explanation evidence" in text
        assert "never supply a new\nanswer from general model knowledge" in text


def test_citation_timestamp_keeps_original_fraction_and_offset(tmp_path):
    cfg = config_for(tmp_path)
    seed(cfg, [("2026-05-19T00:00:00.123456789Z", "Question")])
    result = weekly(cfg, FakeGenerator())
    path = Path(result["results"][0]["jsonPath"])
    value = json.loads(path.read_text("utf-8"))
    text = render_review(value, path, "ja")
    assert "2026-05-19T00:00:00.123456789Z" in text
    assert value["evidence"][0]["timestamps"] == ["2026-05-19T00:00:00.123456789Z"]


def test_lower_reviews_do_not_reintroduce_administrative_citation_targets(tmp_path):
    from tkn_codex_chat_note.runtime import atomic_write_json

    cfg = config_for(tmp_path)
    seed(cfg, [("2026-05-19T00:00:00Z", "Question")])
    result = weekly(cfg, FakeGenerator())
    path = Path(result["results"][0]["jsonPath"])
    value = json.loads(path.read_text("utf-8"))
    for e in value["evidence"]:
        e["text"] = ADMIN
    atomic_write_json(path, value)
    source = cfg.enabled_source_configs()[0]
    monthly = period_evidence(source, period_for("monthly", date(2026, 5, 1)))
    assert ADMIN not in json.dumps(monthly.input_records())
    assert any("no citable evidence" in gap for gap in monthly.gaps)
