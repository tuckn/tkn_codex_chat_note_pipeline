from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import Mock

import pytest
from test_api_inference import settings
from test_session_note_pipeline import note_data
from test_thread_timeline import candidate, config, event

from tkn_codex_chat_note import offline_tokens
from tkn_codex_chat_note.api_inference import ApiClient
from tkn_codex_chat_note.generation_usage import estimate_totals, usage_totals
from tkn_codex_chat_note.session_notes import PipelineError, ProviderSummarizer, validate_note_data
from tkn_codex_chat_note.state_reconciliation import collect_state_items, reconcile_state


def test_offline_fallback_never_downloads_or_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path / "missing"))
    monkeypatch.setattr(
        offline_tokens.tiktoken, "get_encoding", Mock(side_effect=AssertionError("network/cache loader"))
    )
    before = list(tmp_path.rglob("*"))
    text = "日本語のテスト abc"
    count, method = offline_tokens.token_estimate(text, azure=True)
    assert count == len(text.encode("utf-8")) + 512 and method == "utf8-byte-upper-bound"
    assert list(tmp_path.rglob("*")) == before


def test_corrupt_tokenizer_cache_is_left_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
    path = offline_tokens.tokenizer_cache_path()
    path.write_bytes(b"corrupt")
    assert offline_tokens.token_estimate("x", azure=True)[1] == "utf8-byte-upper-bound"
    assert path.read_bytes() == b"corrupt"


def test_azure_plan_counts_full_request_output_and_unknown_merge(tmp_path, monkeypatch):
    cfg = replace(
        config(tmp_path),
        provider="azure-openai",
        model="example-model",
        inference_options=settings(max_calls=2).inference_options,
    )
    seen = []

    def estimate(client, prompt, schema):
        seen.append((prompt, schema))
        return 1000

    monkeypatch.setattr(ApiClient, "estimate", estimate)
    monkeypatch.setattr(ApiClient, "invoke", Mock(side_effect=AssertionError("inference")))
    before = list(tmp_path.rglob("*"))
    runner = ProviderSummarizer(cfg, chunk_characters=250, cache_root=tmp_path / "cache")
    value = runner.estimate(candidate(tmp_path, (event("one"), event("two"))))
    assert value["chunkCount"] == 2 and value["baseCalls"] == 3
    assert value["inputTokensEstimate"] == 62000
    assert value["outputTokensCeiling"] == 48000
    assert value["baseCostCeilingJpy"] == pytest.approx((62000 * 31.864 + 48000 * 191.184) / 1_000_000)
    assert value["mergeUsesInputCeiling"] and value["mayExceedCommandBudget"]
    assert all("BEGIN_INPUT_JSON" in p and "timeline" in s["properties"] for p, s in seen)
    assert list(tmp_path.rglob("*")) == before


def test_codex_plan_uses_characters_and_unknown_price(tmp_path):
    runner = ProviderSummarizer(config(tmp_path))
    result = runner.estimate(candidate(tmp_path, (event("one"),)))
    assert result["preparedTextCharacters"] == 7 and result["pendingPromptCharacters"] > 7
    assert result["inputTokensEstimate"] is None and result["baseCostCeilingJpy"] is None
    assert estimate_totals([result])["baseCostCeilingJpy"] is None


def test_usage_unknown_requests_keep_partial_known_cost():
    result = usage_totals(
        [
            {"inputTokens": 100, "outputTokens": 40, "estimatedCostJpy": 0.5},
            {"inputTokens": None, "outputTokens": None, "estimatedCostJpy": None},
        ]
    )
    assert result["inputTokens"] is None and result["knownInputTokens"] == 100
    assert result["estimatedCostJpy"] is None and result["knownEstimatedCostJpy"] == 0.5
    assert result["estimatedCostJpyMissingRequests"] == 1


def state_case(tmp_path):
    events = (event("first"), event("later", actor="assistant"))
    first = note_data(candidate(tmp_path, (events[0],)))
    first["lastKnownState"]["unverified"] = ["追加検証は実施されていない。"]
    last = note_data(candidate(tmp_path, (events[1],)))
    last.pop("timeline")
    return events, collect_state_items([first]), last


def review(item, disposition="retain", evidence=None):
    return {
        "itemId": item["itemId"],
        "disposition": disposition,
        "reason": "根拠に基づく判断。",
        "eventIds": evidence or [],
    }


def test_pending_checks_survive_done_overview(tmp_path):
    events, items, value = state_case(tmp_path)
    value["stateItemReviews"] = [review(items[0])]
    result = reconcile_state(value, items, events)
    assert result["lastKnownState"]["workState"] == "done"
    assert result["lastKnownState"]["unverified"] == [items[0]["text"]]
    assert "first" in result["lastKnownState"]["eventIds"]
    assert "stateItemReviews" not in result
    assert not value["lastKnownState"]["unverified"]


@pytest.mark.parametrize("reviews", [[], ["duplicate", "duplicate"], ["unknown"]])
def test_every_pending_item_requires_one_disposition(tmp_path, reviews):
    events, items, value = state_case(tmp_path)
    value["stateItemReviews"] = [{**review(items[0]), "itemId": item} for item in reviews]
    with pytest.raises(ValueError, match="every pending item"):
        reconcile_state(value, items, events)


@pytest.mark.parametrize("evidence", [[], ["first"], ["invented"]])
def test_removal_requires_later_known_evidence(tmp_path, evidence):
    events, items, value = state_case(tmp_path)
    value["stateItemReviews"] = [review(items[0], "resolved", evidence)]
    with pytest.raises(ValueError):
        reconcile_state(value, items, events)


def test_resolution_cannot_cross_history(tmp_path):
    events, items, value = state_case(tmp_path)
    events = (replace(events[0], branch_id="A"), replace(events[1], branch_id="B"))
    value["stateItemReviews"] = [review(items[0], "resolved", ["later"])]
    with pytest.raises(ValueError, match="different history"):
        reconcile_state(value, items, events)


@pytest.mark.parametrize("evidence", [["later"], ["first", "later"]])
def test_later_same_history_resolution_does_not_reintroduce_old_check(tmp_path, evidence):
    events, items, value = state_case(tmp_path)
    value["stateItemReviews"] = [review(items[0], "resolved", evidence)]
    assert reconcile_state(value, items, events)["lastKnownState"]["unverified"] == []


def test_retained_unresolved_cannot_be_silently_done(tmp_path):
    events, items, value = state_case(tmp_path)
    items[0]["kind"] = "unresolved"
    value["stateItemReviews"] = [review(items[0])]
    result = reconcile_state(value, items, events)
    with pytest.raises(PipelineError):
        validate_note_data(result, {e.id for e in events}, overview_only=True)


def test_merge_repair_keeps_state_context_and_resume_estimate(tmp_path, monkeypatch):
    events = (event("first"), event("later", actor="assistant"))
    case = candidate(tmp_path, events)
    runner = ProviderSummarizer(config(tmp_path), chunk_characters=250, cache_root=tmp_path / "cache")
    overview_calls = 0

    def invoke(prompt, *, overview_only=False):
        nonlocal overview_calls
        payload = json.loads(prompt.split("BEGIN_INPUT_JSON\n")[1].split("\nEND_INPUT_JSON")[0])
        if not overview_only:
            e = next(e for e in events if e.id == payload["events"][0]["id"])
            data = note_data(candidate(tmp_path, (e,)))
            if e.id == "first":
                data["lastKnownState"]["unverified"] = ["追加検証は実施されていない。"]
            return data
        overview_calls += 1
        data = note_data(candidate(tmp_path, (events[-1],)))
        data.pop("timeline")
        if overview_calls == 1:
            data["stateItemReviews"] = []
        else:
            assert payload["stateItems"] and payload["partials"]
            assert "allowedEventIds" not in payload  # Citations already supplied in partial records.
            data["stateItemReviews"] = [review(payload["stateItems"][0])]
        return data

    monkeypatch.setattr(runner, "_invoke", invoke)
    result = runner.generate(case)
    assert result["lastKnownState"]["unverified"] == ["追加検証は実施されていない。"]
    assert runner.last_metrics["semanticRetries"] == 1 and runner.last_metrics["stateItemCount"] == 1
    estimate = runner.estimate(case)
    assert estimate["cachedChunkCount"] == 2 and estimate["pendingChunkCount"] == 0
    assert estimate["pendingPromptCharacters"] == 0
    second = runner.generate(case)
    assert second == result and overview_calls == 2
    assert runner.last_metrics["reusedReductions"] == 1


def test_runtime_progress_and_saved_report_have_usage(tmp_path, monkeypatch, caplog):
    import httpx
    from test_api_inference import AZURE
    from test_pipeline_workflow import config_for
    from test_session_note_pipeline import write_chat

    from tkn_codex_chat_note import api_inference
    from tkn_codex_chat_note.cli import LOGGER, _progress
    from tkn_codex_chat_note.config import GenerationConfig
    from tkn_codex_chat_note.pipeline import run_pipeline

    cfg = config_for(tmp_path)
    write_chat(cfg.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path)
    cfg.generation = GenerationConfig(
        active_profile="azure-high",
        profiles={"azure-high": {"provider": "azure-openai", "model": "example-model", **AZURE, "limits": {}}},
    )
    actual_client = httpx.Client

    def handle(request):
        assert str(request.url) == AZURE["endpoint"] + "chat/completions"
        assert json.loads(request.content)["model"] == "example-model"
        prompt = json.loads(request.content)["messages"][0]["content"]
        payload = json.loads(prompt.split("BEGIN_INPUT_JSON\n")[1].split("\nEND_INPUT_JSON")[0])
        events = tuple(event(e["id"], actor=e["actor"]) for e in payload["events"])
        value = note_data(candidate(tmp_path, events))
        return httpx.Response(
            200,
            json={
                "model": "example-model-2026-07-09",
                "usage": {"prompt_tokens": 1000, "completion_tokens": 200},
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}],
            },
        )

    monkeypatch.setattr(
        api_inference.httpx, "Client", lambda **kw: actual_client(transport=httpx.MockTransport(handle), **kw)
    )
    monkeypatch.setattr(api_inference, "token_provider", lambda *a: lambda: "test-token")
    monkeypatch.setattr(ApiClient, "estimate", lambda *a: 1000)
    caplog.set_level("INFO", logger=LOGGER.name)
    report = run_pipeline(cfg, mode="clone", progress=_progress)
    assert report["ok"], report
    assert "input 1,000 tokens" in caplog.text and "base cost ceiling JPY" in caplog.text
    saved = json.loads(__import__("pathlib").Path(report["reportPath"]).read_text(encoding="utf-8"))
    assert saved["generationProfile"] == "azure-high"
    assert saved["generationProvider"] == "azure-openai"
    assert saved["threads"][0]["generationEstimate"]["generationProfile"] == "azure-high"
    assert saved["usageTotals"]["inputTokens"] == 1000
    assert saved["usageTotals"]["estimatedCostJpy"] == pytest.approx(0.0701008)
    assert saved["threads"][0]["generationMetrics"]["apiRequests"][0]["stage"] == "chunk"
    assert "test-token" not in json.dumps(saved)
    from tkn_codex_chat_note.frontmatter import parse_simple_frontmatter

    note = next((cfg.source_data_root / "session-notes").rglob("*.md"))
    metadata = parse_simple_frontmatter(note.read_text(encoding="utf-8"))
    assert metadata["generatorDeployment"] == "example-model"
    assert metadata["generatorModel"] == "example-model-2026-07-09"
    activities = [json.loads(p.read_text(encoding="utf-8"))
                  for p in (cfg.source_data_root / "provenance" / "activities").glob("*.json")]
    summary = next(a for a in activities if a["agent"].get("requestedDeployment"))
    assert summary["agent"]["model"] == "example-model-2026-07-09"
    assert summary["agent"]["requestedDeployment"] == "example-model"
    assert summary["agent"]["generationProfile"] == "azure-high"


def test_unavailable_estimate_marks_command_totals_incomplete():
    from tkn_codex_chat_note.cli import _estimate_summary

    totals = estimate_totals([{"status": "unavailable", "reason": "input cannot fit"}])
    assert totals["inputTokensEstimate"] is None
    assert "1 thread estimates unavailable (totals incomplete)" in _estimate_summary(totals)


def test_repair_json_compaction_preserves_all_source_text_and_citations():
    from tkn_codex_chat_note.api_inference import alias_prompt

    payload = {
        "draft": {"eventIds": ["origin"]},
        "partials": [{"text": "引用や文字列内の空白  は保持", "eventIds": ["origin"]}],
    }
    prompt = (
        "MODE: repair-invalid-draft\nBEGIN_INPUT_JSON\n"
        + json.dumps(payload, ensure_ascii=False)
        + "\nEND_INPUT_JSON\n"
    )
    compact = alias_prompt(prompt, {"origin": "E00001"})
    value = json.loads(compact.split("BEGIN_INPUT_JSON\n")[1].split("\nEND_INPUT_JSON")[0])
    assert value["partials"][0]["text"] == payload["partials"][0]["text"]
    assert value["draft"]["eventIds"] == value["partials"][0]["eventIds"] == ["E00001"]
    encoded = compact.split("BEGIN_INPUT_JSON\n")[1].split("\nEND_INPUT_JSON")[0]
    assert ": " not in encoded and ", " not in encoded
