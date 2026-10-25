"""Review progress must reach stderr before inference returns, with explicit budget stops."""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from test_reviews import FakeGenerator, config_for, seed, weekly

from tkn_codex_chat_note.api_inference import ApiBudgetExceeded
from tkn_codex_chat_note.cli import main
from tkn_codex_chat_note.review_generation import ReviewGenerator
from tkn_codex_chat_note.reviews import run_reviews


@pytest.mark.parametrize("quiet", [False, True])
def test_cli_progress_is_live_and_json_stays_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], quiet: bool,
) -> None:
    import tkn_codex_chat_note.cli as cli
    import tkn_codex_chat_note.reviews as reviews

    config = config_for(tmp_path)
    seed(config, [("2026-05-19T00:00:00Z", "PRIVATE SOURCE TEXT")])
    monkeypatch.setattr(cli, "load_app_config", lambda **_: config)
    model = FakeGenerator()
    live: list[str] = []

    def generate(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        captured = capsys.readouterr()
        assert captured.out == ""
        live.append(captured.err)
        if quiet:
            assert captured.err == ""
        else:
            # This assertion runs INSIDE inference, before any model response or final report exists.
            assert f"Model call {len(live)}/12: starting" in captured.err
            assert "waiting for response" in captured.err
            if len(live) == 1:
                assert "preparing inputs" in captured.err
                assert "generated" not in captured.err
            else:
                assert "response received" in captured.err
        return model(prompt, schema)

    def run(config: Any, **kwargs: Any) -> dict[str, Any]:
        return run_reviews(config, generate=generate, **kwargs)

    monkeypatch.setattr(reviews, "run_reviews", run)
    args = ["review-notes", "build", "--period", "weekly", "--date", "2026-05-19", "--full-output"]
    assert main(args + (["--quiet"] if quiet else [])) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["generatedCount"] == 1
    assert bool(live)
    if quiet:
        assert captured.err == ""
    else:
        assert captured.err.count("weekly 2026-W21: generated") == 1
        assert "response received" in captured.err and "Review note:" in captured.err
    assert "PRIVATE SOURCE TEXT" not in "".join(live) + captured.err


def test_completed_period_is_logged_before_next_model_call(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    config = config_for(tmp_path, max_calls=100)
    seed(config, [("2026-05-12T00:00:00Z", "First week"), ("2026-05-19T00:00:00Z", "Second week")])
    model = FakeGenerator()
    caplog.set_level(logging.INFO)

    def generate(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        if '"key": "2026-W21"' in prompt:
            assert "weekly 2026-W20: generated" in caplog.text
        return model(prompt, schema)

    result = run_reviews(config, as_of=date(2026, 5, 25), generate=generate)
    assert result["generatedCount"] == 2


def test_call_limit_warns_and_counts_current_period_as_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    import tkn_codex_chat_note.cli as cli
    import tkn_codex_chat_note.reviews as reviews

    config = config_for(tmp_path, max_calls=1)
    seed(config, [("2026-05-19T00:00:00Z", "Question")])
    monkeypatch.setattr(cli, "load_app_config", lambda **_: config)

    def run(config: Any, **kwargs: Any) -> dict[str, Any]:
        return run_reviews(config, generate=FakeGenerator(), **kwargs)

    monkeypatch.setattr(reviews, "run_reviews", run)
    assert main(["review-notes", "due", "--as-of", "2026-05-25", "--full-output"]) == 2
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["ok"] and not report["complete"] and report["pendingPeriods"] == 1
    stop = report["results"][0]["generationStop"]
    assert stop["reason"] == "review-call-budget" and stop["requestCount"] == stop["maxCalls"] == 1
    assert "[WARNING] weekly 2026-W21: deferred; Model call limit reached (1/1)" in captured.err
    assert "generation incomplete; 1 pending periods" in captured.err
    assert "rerun the same command to resume" in captured.err
    saved = json.loads(Path(report["reportPath"]).read_text("utf-8"))
    assert saved["results"][0]["generationStop"] == stop


def test_time_limit_is_distinct_and_does_not_call_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    import tkn_codex_chat_note.reviews as reviews

    config = config_for(tmp_path)
    seed(config, [("2026-05-19T00:00:00Z", "Question")])
    model = FakeGenerator()

    def generator(config: Any, run_id: str, **kwargs: Any) -> ReviewGenerator:
        result = ReviewGenerator(config, run_id, **kwargs)
        result.deadline = 0
        return result

    monkeypatch.setattr(reviews, "ReviewGenerator", generator)
    caplog.set_level(logging.INFO)
    result = weekly(config, model)
    assert not result["complete"] and not model.prompts and result["modelCalls"] == 0
    assert result["results"][0]["generationStop"]["reason"] == "runtime-deadline"
    assert "Review runtime limit reached (230 minutes)" in caplog.text
    assert "Model call limit reached" not in caplog.text


def test_api_budget_stop_retains_its_reason(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    config = config_for(tmp_path)
    seed(config, [("2026-05-19T00:00:00Z", "Question")])
    details = {"reason": "api-cost-budget", "message": "API cost budget reached"}

    def generate(*_: Any) -> dict[str, Any]:
        raise ApiBudgetExceeded(details)

    result = weekly(config, generate)
    assert result["results"][0]["generationStop"] == details
    assert not result["complete"] and "API cost budget reached" in caplog.text


def test_dry_run_and_cache_do_not_log_model_calls(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    config = config_for(tmp_path)
    seed(config, [("2026-05-19T00:00:00Z", "Question")])
    model = FakeGenerator()
    caplog.set_level(logging.INFO)
    weekly(config, model, dry_run=True)
    assert not model.prompts and "Model call " not in caplog.text
    weekly(config, model)
    caplog.clear()
    result = weekly(config, model, force=False)
    assert result["modelCalls"] == 0 and "Model call " not in caplog.text
    # Rebuild without published state; validated cached stages must still be described as cache hits.
    source = config.enabled_source_configs()[0]
    (source.review_state_root / "index.json").unlink()
    for path in source.review_data_root.rglob("*"):
        if path.is_file():
            path.unlink()
    caplog.clear()
    result = weekly(config, model)
    assert result["cacheHits"] > 0 and result["modelCalls"] == 0
    assert "reusing cached" in caplog.text and "Model call " not in caplog.text
