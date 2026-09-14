"""Stop command-wide generation once, retain checkpoints, and resume with a fresh budget."""
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from test_api_inference import AZURE, SCHEMA, settings
from test_multi_sources import multi_config, source_chats
from test_session_note_pipeline import note_data, write_chat
from test_thread_timeline import candidate, config, event

from tkn_codex_chat_note import api_inference
from tkn_codex_chat_note.api_inference import ApiBudgetExceeded, ApiClient
from tkn_codex_chat_note.cli import main
from tkn_codex_chat_note.config import GenerationConfig, write_config
from tkn_codex_chat_note.pipeline import run_pipeline
from tkn_codex_chat_note.session_notes import ProviderSummarizer


def mock_responses(monkeypatch, tmp_path):
    calls = []
    real_client = httpx.Client

    def handle(request):
        calls.append(request)
        prompt = json.loads(request.content)["messages"][0]["content"]
        payload = json.loads(prompt.split("BEGIN_INPUT_JSON\n")[1].split("\nEND_INPUT_JSON")[0])
        if "events" in payload:
            events = tuple(event(e["id"], actor=e["actor"]) for e in payload["events"])
            value = note_data(candidate(tmp_path, events))
        else:
            value = note_data(candidate(tmp_path, (event(payload["partials"][-1]["lastKnownState"]["eventIds"][-1]),)))
            value.pop("timeline")
        return httpx.Response(200, json={"model": "response-model", "usage": {
            "prompt_tokens": 100, "completion_tokens": 20},
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}]})

    monkeypatch.setattr(api_inference.httpx, "Client", lambda **kw: real_client(
        transport=httpx.MockTransport(handle), **kw))
    monkeypatch.setattr(api_inference, "token_provider", Mock(return_value=lambda: "test-token"))
    monkeypatch.setattr(ApiClient, "estimate", lambda *a: 1000)
    return calls


@pytest.mark.parametrize("kind", ["cost", "calls"])
def test_stop_sources(tmp_path, monkeypatch, kind):
    cfg = multi_config(tmp_path)
    source_chats(cfg)
    first = cfg.enabled_source_configs()[0]
    write_chat(first.sessions_root / "extra.jsonl", thread_id="extra", cwd=tmp_path)
    limits = {"max_calls": 1} if kind == "calls" else {"max_cost_jpy": 0.001}
    cfg.generation = GenerationConfig(active_profile="azure", profiles={"azure": {
        "provider": "azure-openai", "model": "example-model", **AZURE, "limits": limits}})
    calls = mock_responses(monkeypatch, tmp_path)
    events = []
    report = run_pipeline(cfg, mode="clone", progress=events.append)
    expected_calls = 1 if kind == "calls" else 0
    assert len(calls) == expected_calls
    assert report["ok"] and not report["complete"] and report["failed"] == []
    assert report["generationStop"]["reason"] == ("api-call-budget" if kind == "calls" else "api-cost-budget")
    assert report["threadCounts"].get("failed", 0) == 0
    assert report["threadCounts"]["deferred"] == 3 - expected_calls
    assert sum(e["type"] == "generation-budget-stop" for e in events) == 1
    assert sum(e["type"] == "thread-start" for e in events) == expected_calls + 1
    assert sum(e["type"] == "generation-estimate" for e in events) == expected_calls + 1
    assert not any(e["type"] == "thread-failed" for e in events)
    for source_report in report["sourceResults"]:
        saved = json.loads(Path(source_report["reportPath"]).read_text(encoding="utf-8"))
        assert saved["generationStop"] == report["generationStop"]
    if kind == "calls":
        resumed = run_pipeline(cfg, mode="pull")
        assert len(calls) == 2 and resumed["generatedSessionNoteCount"] == 1
        assert resumed["threadCounts"]["current"] == 2
        final = run_pipeline(cfg, mode="pull")
        assert final["complete"] and len(calls) == 3


def test_budget_stop_remains_sticky_even_if_a_cheaper_request_would_fit(monkeypatch):
    client = ApiClient(settings(max_cost_jpy=1))
    client.reserved_jpy = 0.99
    monkeypatch.setattr(client, "estimate", lambda *a: 1000)
    auth = Mock(side_effect=AssertionError("authentication"))
    monkeypatch.setattr(api_inference, "token_provider", auth)
    with pytest.raises(ApiBudgetExceeded):
        client.invoke("first", SCHEMA, timeout=10)
    details = dict(client.budget_stop)
    client.reserved_jpy = 0  # Even sufficient funds do not implicitly resume this command.
    with pytest.raises(ApiBudgetExceeded) as exc:
        client.invoke("second", SCHEMA, timeout=10)
    assert exc.value.details == details and client.records == []
    auth.assert_not_called()


def test_resume_chunks(tmp_path, monkeypatch):
    calls = mock_responses(monkeypatch, tmp_path)
    cfg = replace(config(tmp_path), provider="azure-openai", model="example-model",
                  inference_options=settings(max_calls=1).inference_options)
    case = candidate(tmp_path, (event("one"), event("two", actor="assistant")))
    cache = tmp_path / "checkpoints"
    for index in range(2):
        runner = ProviderSummarizer(cfg, chunk_characters=250, cache_root=cache)
        with pytest.raises(ApiBudgetExceeded):
            runner.generate(case)
        assert runner.last_metrics["reusedChunks"] == index
        assert len(calls) == index + 1
    runner = ProviderSummarizer(cfg, chunk_characters=250, cache_root=cache)
    runner.generate(case)
    assert runner.last_metrics["reusedChunks"] == 2
    assert runner.last_metrics["modelCalls"] == 1 and len(calls) == 3


@pytest.mark.parametrize("command", ["clone", "session-notes"])
def test_cli_pause(tmp_path, monkeypatch, capsys, command):
    cfg = multi_config(tmp_path)
    source_chats(cfg)
    cfg.generation = GenerationConfig(active_profile="azure", profiles={"azure": {
        "provider": "azure-openai", "model": "example-model", **AZURE, "limits": {"max_cost_jpy": 0.001}}})
    mock_responses(monkeypatch, tmp_path)
    if command == "session-notes":
        run_pipeline(cfg, mode="raw")
    path = tmp_path / "config.yaml"
    write_config(cfg, path)
    args = ["--config", str(path), command]
    if command == "session-notes":
        args.append("build")
    assert main(args) == 2
    out = capsys.readouterr()
    report = json.loads(out.out)
    assert report["generationStop"]["reason"] == "api-cost-budget"
    assert "Generation paused" in out.err and "Failed thread" not in out.err
