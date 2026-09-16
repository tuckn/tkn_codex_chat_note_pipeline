from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from test_api_inference import fake_http as fake_http
from test_api_inference import response, settings
from test_session_note_pipeline import note_data
from test_thread_timeline import candidate, config, event

from tkn_codex_chat_note.api_inference import ApiBudgetExceeded, ApiClient, remap_event_ids
from tkn_codex_chat_note.prompting import compact_merge_partials, render_reduction_prompt
from tkn_codex_chat_note.session_notes import PipelineError, ProviderSummarizer
from tkn_codex_chat_note.state_reconciliation import REVIEW_SCHEMA, collect_state_items


def payload(prompt):
    return json.loads(prompt.split("BEGIN_INPUT_JSON\n", 1)[1].split("\nEND_INPUT_JSON", 1)[0])


def azure_runner(tmp_path, *, progress=None, **limits):
    cfg = replace(config(tmp_path), provider="azure-openai", model="example-model",
                  inference_options=settings(**limits).inference_options)
    return ProviderSummarizer(cfg, observer=progress, cache_root=tmp_path / "cache")


def reply(data):
    wire = remap_event_ids(data, {"known": "E00001"})
    return response(choices=[{"finish_reason": "stop", "message": {"content": json.dumps(wire)}}])


@pytest.mark.parametrize("feedback_capacity", ["detailed", "short", "none"])
def test_oversized_repair_regenerates_keeps_sources_and_reuses_checkpoint(
    tmp_path, monkeypatch, fake_http, feedback_capacity,
):
    # Reproduce the reported 59,451 -> 62,386 input estimates without private source data or live calls.
    calls, replies = fake_http
    case = candidate(tmp_path, (event("known"),))
    good = note_data(case)
    bad = deepcopy(good)
    bad["lastKnownState"]["unresolved"] = ["追加作業が未完了。"]
    replies.extend([reply(bad), reply(good)])
    progress = []
    runner = azure_runner(tmp_path, progress=progress.append)

    def estimate(_api, prompt, _schema):
        if "MODE: repair-invalid-draft\n" in prompt:
            return 62386
        feedback = payload(prompt).get("validationError")
        if feedback and (feedback_capacity == "none" or feedback_capacity == "short" and "done work" in feedback):
            return 60001
        return 59500 if feedback else 59451

    monkeypatch.setattr(ApiClient, "estimate", estimate)
    result = runner.generate(case)
    assert result == good
    assert len(calls) == 2
    bodies = [json.loads(request.content) for request in calls]
    inputs = [payload(body["messages"][0]["content"]) for body in bodies]
    assert inputs[1]["events"] == inputs[0]["events"]
    assert inputs[1]["part"] == inputs[0]["part"]
    assert inputs[1]["partCount"] == inputs[0]["partCount"]
    assert "draft" not in inputs[1]
    assert ("validationError" in inputs[1]) == (feedback_capacity != "none")
    if feedback_capacity == "detailed":
        assert "done work cannot contain unresolved" in inputs[1]["validationError"]
    elif feedback_capacity == "short":
        assert inputs[1]["validationError"].startswith("Previous output failed validation.")
    assert bodies[1]["response_format"] == bodies[0]["response_format"]
    assert runner.last_metrics["modelCalls"] == 2
    assert runner.last_metrics["semanticRetries"] == runner.last_metrics["repairFallbacks"] == 1
    assert runner.last_metrics["apiRequests"][1]["stage"] == "chunk-regenerate"
    diagnostic = runner.last_metrics["validationFailures"][0]
    assert "done work cannot contain unresolved" in diagnostic["reason"]
    assert any(item["type"] == "repair-input-fallback" for item in progress)
    resumed = azure_runner(tmp_path)
    assert resumed.generate(case) == good
    assert len(calls) == 2 and resumed.last_metrics["reusedChunks"] == 1


@pytest.mark.parametrize("limit,expected_calls", [({"max_calls": 1}, 1), ({"max_cost_jpy": 5.1}, 1), ({}, 3)])
def test_fallback_respects_budget_and_semantic_attempt_limit(tmp_path, monkeypatch, fake_http, limit, expected_calls):
    calls, replies = fake_http
    case = candidate(tmp_path, (event("known"),))
    bad = note_data(case)
    bad["lastKnownState"]["unresolved"] = ["追加作業が未完了。"]
    replies.extend(reply(bad) for _ in range(3))
    runner = azure_runner(tmp_path, **limit)
    monkeypatch.setattr(ApiClient, "estimate", lambda _api, prompt, _schema:
                        62386 if "MODE: repair-invalid-draft\n" in prompt else 59451)
    with pytest.raises(ApiBudgetExceeded if limit else PipelineError):
        runner.generate(case)
    assert len(calls) == expected_calls
    assert len(runner.last_metrics["validationFailures"]) == expected_calls
    assert not list((tmp_path / "cache").rglob("*.json"))  # Never accept an invalid draft as a checkpoint.


def test_merge_fallback_retains_partials_and_pending_state_reviews(tmp_path, monkeypatch, fake_http):
    calls, replies = fake_http
    case = candidate(tmp_path, (event("known"),))
    partial = note_data(case)
    partial["lastKnownState"]["unverified"] = ["追加検証は未実施。"]
    partial["pendingStateItems"] = [{"kind": "unverified", "itemIndex": 0, "eventIds": ["known"]}]
    runner = azure_runner(tmp_path)
    runner.state_events = case.events
    runner.state_items = collect_state_items([partial])
    runner.state_context = {"stateItems": runner.state_items, "partials": compact_merge_partials([partial])}
    runner.overview_schema["properties"]["stateItemReviews"] = deepcopy(REVIEW_SCHEMA)
    runner.overview_schema["required"].append("stateItemReviews")
    good = deepcopy(partial)
    good.pop("pendingStateItems")
    good.pop("timeline")
    good["stateItemReviews"] = [{"itemId": runner.state_items[0]["itemId"], "disposition": "retain",
                                 "reason": "検証の完了は確認できない。", "eventIds": []}]
    bad = deepcopy(good)
    bad["lastKnownState"]["unresolved"] = ["追加作業が未完了。"]
    replies.extend([reply(bad), reply(good)])
    monkeypatch.setattr(ApiClient, "estimate", lambda _api, prompt, _schema:
                        62386 if "MODE: repair-invalid-draft\n" in prompt else 59451)
    prompt = render_reduction_prompt(runner.prompt, thread_id=case.thread_id,
                                     partials=[partial], state_items=runner.state_items)
    result = runner._validated_invoke(prompt, {"known"}, case.thread_id, overview_only=True)
    assert result == good and len(calls) == 2
    regenerated = payload(json.loads(calls[1].content)["messages"][0]["content"])
    restored = remap_event_ids(regenerated, {"E00001": "known"})
    assert restored["partials"] == payload(prompt)["partials"]
    assert restored["stateItems"] == payload(prompt)["stateItems"]
    assert runner.last_metrics["apiRequests"][1]["stage"] == "merge-regenerate"


@pytest.mark.parametrize("overview_only", [False, True])
def test_english_phrases_do_not_trigger_warning_or_extra_api_calls(tmp_path, fake_http, overview_only):
    calls, replies = fake_http
    case = candidate(tmp_path, (event("known"),))
    data = note_data(case)
    data["summaryItems"][0]["text"] = "Actual execution は SUPPLIED EVENTS に記録されている。"
    data["sourceLimitations"] = [
        "CSVのファイル属性取得コマンドは呼び出されているが、結果は supplied events 内で確認できない。",
        "最終全件 apply の出力は sourceTruncated=true の抜粋で、全文は supplied events から確認できない。",
        "Obsidian起動中のapply結果やtimeout時のCSV出力は supplied events には記録されていない。",
    ]
    progress = []
    runner = azure_runner(tmp_path, progress=progress.append)
    if overview_only:
        data.pop("timeline")
    replies.append(reply(data))
    original = deepcopy(data)
    if overview_only:
        prompt = render_reduction_prompt(runner.prompt, thread_id=case.thread_id, partials=[note_data(case)])
        result = runner._validated_invoke(prompt, {"known"}, case.thread_id, overview_only=True)
    else:
        result = runner.generate(case)
        assert azure_runner(tmp_path).generate(case) == original  # Accepted output is reusable.
    assert result == original
    assert len(calls) == 1
    assert not runner.last_metrics.get("semanticRetries")
    assert not runner.last_metrics.get("validationFailures")
    assert not any(item["type"] in {"validation-repair", "repair-input-fallback"} for item in progress)


@pytest.mark.parametrize("recover", [True, False])
def test_pipeline_persists_validation_diagnostics_and_only_publishes_valid_notes(
    tmp_path, monkeypatch, fake_http, recover,
):
    from test_api_inference import AZURE
    from test_pipeline_workflow import config_for
    from test_session_note_pipeline import write_chat

    from tkn_codex_chat_note.chat_logs import read_thread_events
    from tkn_codex_chat_note.config import GenerationConfig
    from tkn_codex_chat_note.pipeline import run_pipeline

    calls, replies = fake_http
    cfg = config_for(tmp_path)
    cfg.generation = GenerationConfig(active_profile="azure", profiles={
        "azure": {"provider": "azure-openai", "model": "example-model", **AZURE},
    })
    source = cfg.sessions_root / "example.jsonl"
    write_chat(source, thread_id="example", cwd=tmp_path)
    events = read_thread_events(source)
    good = note_data(candidate(tmp_path, events))
    bad = deepcopy(good)
    bad["lastKnownState"]["unresolved"] = ["追加作業が未完了。"]
    mapping = {identifier: f"E{index:05d}" for index, identifier in enumerate(sorted(e.id for e in events), 1)}
    for value in ([bad, good] if recover else [bad, bad, bad]):
        replies.append(reply(remap_event_ids(value, mapping)))
    monkeypatch.setattr(ApiClient, "estimate", lambda _api, prompt, _schema:
                        62386 if "MODE: repair-invalid-draft\n" in prompt else 59451)
    report = run_pipeline(cfg, mode="clone")
    assert report["complete"] is recover
    assert len(calls) == (2 if recover else 3)
    persisted = json.loads(Path(report["reportPath"]).read_text(encoding="utf-8"))
    diagnostic = persisted["threads"][0]["generationMetrics"]["validationFailures"][0]
    assert "done work cannot contain unresolved" in diagnostic["reason"]
    assert bool(list(cfg.data_root.rglob("*.md"))) is recover
