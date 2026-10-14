from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from test_reviews import config_for, seed

from tkn_codex_chat_note.review_evidence import period_evidence, raw_supplement
from tkn_codex_chat_note.review_model import period_for
from tkn_codex_chat_note.review_preparation import routine_index, tool_excerpt


def event(text: str, kind: str = "tool_result") -> dict:
    return {"text": text, "kind": kind, "name": "exec"}


def test_nested_transport_keeps_failure_exit_code_and_decodes_diagnostic():
    text = json.dumps([{"text": json.dumps({"value": {"exit_code": 1,
        "output": "FileNotFoundError: missing.md\n" + "irrelevant " * 3000}})}])
    excerpt, signal = tool_excerpt(event(text))
    assert signal and "exit_code=1" in excerpt and "FileNotFoundError: missing.md" in excerpt
    assert len(excerpt) < 1600


def test_exit_zero_does_not_hide_validation_errors():
    excerpt, signal = tool_excerpt(event(json.dumps({"exit_code": 0, "output": "validation_errors=18"})))
    assert signal and "validation_errors=18" in excerpt and "exit_code=0" in excerpt


def test_routine_source_listing_does_not_treat_timeout_variable_as_failure():
    assert tool_excerpt(event(json.dumps({"exit_code": 0, "output": "timeout = 1800\n" * 1000}))) == ("", False)


def test_identical_calls_remain_record_counts_not_vlm_execution_counts():
    calls = [event('same image request', 'tool_call')] * 3
    index = routine_index(calls)
    assert '"tool_call": 3' in index
    assert 'NOT independent executions' in index
    assert '"repeatedExactCallTextHashes"' in index
    assert 'same image request' not in index


def test_failed_normalization_is_excluded_with_visible_coverage_gap(tmp_path: Path):
    config = config_for(tmp_path)
    seed(config, [("2026-05-19T00:00:00Z", "Ambiguous ownership")])
    path = config.data_root / "catalog/threads.json"
    value = json.loads(path.read_text("utf-8"))
    value["threads"][0].update(status="failed", reason="invalid-jsonl")
    path.write_text(json.dumps(value), encoding="utf-8")
    result = period_evidence(config, period_for("weekly", date(2026, 5, 19)))
    assert result.input_records() == []
    assert any("failed normalization" in gap for gap in result.gaps)


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_raw_line_reference_uses_physical_newlines(tmp_path: Path, separator: str):
    config = config_for(tmp_path)
    seed(config, [("2026-05-19T00:00:00Z", "Before" + separator + "After"),
                  ("2026-05-19T00:01:00Z", "Second record")])
    period = period_for("weekly", date(2026, 5, 19))
    evidence = period_evidence(config, period)
    id = next(id for id in evidence.selected if "Second record" in evidence.evidence[id]["text"])
    raw = raw_supplement(config, evidence, [id], period, 16000)
    assert any(e.get("rawLine") == 2 and "Second record" in e["text"] for e in raw.evidence.values())
    assert not raw.gaps
