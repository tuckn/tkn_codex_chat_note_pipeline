from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
import pytest
from pydantic import ValidationError
from test_thread_timeline import candidate, config, event

from tkn_codex_chat_note import api_inference
from tkn_codex_chat_note.api_inference import ApiClient, ApiError, strict_schema
from tkn_codex_chat_note.api_settings import ApiLimits, AzureSettings
from tkn_codex_chat_note.config import GenerationConfig
from tkn_codex_chat_note.session_notes import ProviderSummarizer, generator_fingerprint

AZURE = {
    "endpoint": "https://example.openai.azure.com/openai/v1/",
    "pricing": {"example-model": {
        "input_jpy_per_million": 31.864,
        "output_jpy_per_million": 191.184,
        "pricing_date": "2026-09-14",
    }},
}
SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "string", "minLength": 1}},
    "required": ["ok"],
    "additionalProperties": False,
}


def settings(**limits):
    return SimpleNamespace(
        provider="azure-openai",
        model="example-model",
        reasoning_effort="low",
        inference_options={"azure": AZURE, "limits": limits},
    )


def response(**overrides):
    return {
        "model": "example-model-2026-07-09",
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        "choices": [{"finish_reason": "stop", "message": {"content": '{"ok":"yes"}'}}],
        **overrides,
    }


@pytest.fixture
def fake_http(monkeypatch):
    actual_client = httpx.Client
    calls = []
    replies = []

    def handle(request):
        calls.append(request)
        item = replies.pop(0)
        return item if isinstance(item, httpx.Response) else httpx.Response(200, json=item)

    monkeypatch.setattr(
        api_inference.httpx, "Client", lambda **kw: actual_client(transport=httpx.MockTransport(handle), **kw)
    )
    monkeypatch.setattr(ApiClient, "estimate", lambda *args: 1000)
    monkeypatch.setattr(api_inference, "token_provider", Mock(return_value=lambda: "test-token"))
    return calls, replies


def test_azure_payload_and_unknown_usage_preserved(fake_http):
    calls, replies = fake_http
    replies.append(response())
    client = ApiClient(settings())
    assert client.invoke("hello", SCHEMA, timeout=30) == {"ok": "yes"}
    body = json.loads(calls[0].content)
    assert str(calls[0].url) == AZURE["endpoint"] + "chat/completions"
    assert body["model"] == "example-model" and body["store"] is False
    assert body["response_format"]["json_schema"]["strict"] is True
    assert "minLength" not in body["response_format"]["json_schema"]["schema"]["properties"]["ok"]
    assert SCHEMA["properties"]["ok"]["minLength"] == 1
    record = client.records[0]
    assert record["inputTokens"] == 100 and record["outputTokens"] == 20
    assert record["reasoningTokens"] is None and record["cachedInputTokens"] is None
    assert "test-token" not in json.dumps(record)


@pytest.mark.parametrize("status,retry", [(400, False), (401, False), (403, False), (429, True), (503, True)])
def test_http_failures_are_bounded_and_usage_unknown(fake_http, status, retry):
    calls, replies = fake_http
    replies.append(httpx.Response(status, headers={"Retry-After": "12"}, text="do not log private response"))
    client = ApiClient(settings())
    with pytest.raises(ApiError) as error:
        client.invoke("hello", SCHEMA, timeout=30)
    assert error.value.retryable is retry and error.value.retry_after == 12
    assert "private" not in str(error.value)
    assert len(calls) == 1 and client.records[0]["inputTokens"] is None
    assert client.reserved_jpy > 0


@pytest.mark.parametrize("kind", ["length", "refusal", "model-missing", "json", "missing"])
def test_incomplete_or_wrong_identity_never_returns_note(fake_http, kind):
    calls, replies = fake_http
    value = response()
    if kind == "length":
        value["choices"][0]["finish_reason"] = "length"
    if kind == "refusal":
        value["choices"][0]["message"]["refusal"] = "refused"
    if kind == "model-missing":
        value.pop("model")
    if kind == "json":
        value["choices"][0]["message"]["content"] = "not json"
    if kind == "missing":
        value["choices"] = []
    replies.append(value)
    client = ApiClient(settings())
    with pytest.raises(ApiError):
        client.invoke("hello", SCHEMA, timeout=30)
    assert len(calls) == 1 and client.records[0]["inputTokens"] == 100


def test_call_and_cost_limits_stop_before_sending(fake_http):
    calls, replies = fake_http
    replies.append(response())
    client = ApiClient(settings(max_calls=1))
    client.invoke("hello", SCHEMA, timeout=30)
    with pytest.raises(ApiError, match="max_calls"):
        client.invoke("hello", SCHEMA, timeout=30)
    client = ApiClient(settings(max_cost_jpy=0.001))
    with pytest.raises(ApiError, match="cost budget"):
        client.invoke("hello", SCHEMA, timeout=30)
    assert len(calls) == 1


def test_input_limit_blocks_chunks_merge_and_repair_before_auth(fake_http, monkeypatch):
    calls, _ = fake_http
    monkeypatch.setattr(ApiClient, "estimate", lambda *args: 5000)
    client = ApiClient(settings(input_tokens=1024))
    with pytest.raises(ApiError, match="input estimate"):
        client.invoke("merge", SCHEMA, timeout=30)
    api_inference.token_provider.assert_not_called()
    assert not calls and not client.records


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.openai.azure.com/openai/v1/",
        "https://example.com/openai/v1/",
        "https://user:pass@example.openai.azure.com/openai/v1/",
        "https://example.openai.azure.com/openai/v1/?key=secret",
        "https://example.openai.azure.com/",
    ],
)
def test_azure_endpoint_credentials_and_untrusted_hosts_rejected(endpoint):
    with pytest.raises(ValidationError):
        AzureSettings.model_validate({**AZURE, "endpoint": endpoint})


def test_limits_and_provider_config_validate_offline():
    with pytest.raises(ValidationError):
        ApiLimits(input_tokens=60000, output_tokens=16000, context_tokens=65536)
    cfg = GenerationConfig(
        active_provider="azure-openai",
        providers={"azure-openai": {"model": "example-model", "azure": AZURE, "limits": {}}},
    )
    assert cfg.profiles["azure-openai"].model == "example-model"
    with pytest.raises(ValidationError):
        GenerationConfig(
            active_provider="azure-openai", providers={"azure-openai": {"model": "example"}}
        )


def test_generation_identity_includes_endpoint_deployment_and_limits(tmp_path):
    cfg = replace(config(tmp_path), provider="azure-openai", inference_options=settings().inference_options)
    one = generator_fingerprint(cfg)
    changed = replace(cfg, model="new-deployment")
    assert generator_fingerprint(changed) != one
    assert (
        generator_fingerprint(
            replace(cfg, inference_options={**cfg.inference_options, "limits": {"output_tokens": 2048}})
        )
        != one
    )


def test_auth_failure_not_retried_by_summarizer(tmp_path, fake_http):
    calls, replies = fake_http
    replies.append(httpx.Response(401))
    runner = ProviderSummarizer(
        replace(
            config(tmp_path),
            provider="azure-openai",
            model="example-model",
            inference_options=settings().inference_options,
        )
    )
    with pytest.raises(Exception, match="HTTP 401"):
        runner.generate(candidate(tmp_path, (event("e"),)))
    assert len(calls) == 1 and runner.last_metrics["modelCalls"] == 1


def test_retry_after_applies_and_all_attempts_count(tmp_path, fake_http):
    calls, replies = fake_http
    replies.extend([httpx.Response(429, headers={"Retry-After": "9"}), response()])
    sleeper = Mock()
    runner = ProviderSummarizer(
        replace(
            config(tmp_path),
            provider="azure-openai",
            model="example-model",
            inference_options=settings().inference_options,
        ),
        sleeper=sleeper,
    )
    assert runner._invoke("hello") == {"ok": "yes"}
    sleeper.assert_called_once_with(9)
    assert len(calls) == 2 and runner.last_metrics["modelCalls"] == 2


def test_ollama_explicit_context_digest_and_usage(fake_http):
    calls, replies = fake_http
    replies.extend(
        [
            {"models": [{"name": "example:fixed", "digest": "fixed-digest"}]},
            {
                "done": True,
                "done_reason": "stop",
                "model": "example:fixed",
                "prompt_eval_count": 300,
                "eval_count": 40,
                "message": {"content": '{"ok":"yes"}'},
            },
        ]
    )
    cfg = SimpleNamespace(
        provider="ollama",
        model="example:fixed",
        reasoning_effort="low",
        ollama_base_url="http://127.0.0.1:11434",
        inference_options={
            "model_digest": "fixed-digest",
            "limits": {"input_tokens": 24000, "output_tokens": 8192, "context_tokens": 32768},
        },
    )
    client = ApiClient(cfg)
    assert client.invoke("hello", SCHEMA, timeout=30) == {"ok": "yes"}
    body = json.loads(calls[1].content)
    assert body["options"]["num_ctx"] == 32768 and body["options"]["num_predict"] == 8192
    assert body["think"] is False and client.records[0]["inputTokens"] == 300


def test_complete_session_schema_transport_shape():
    from tkn_codex_chat_note.summary_resources import load_summary_profile

    schema = strict_schema(load_summary_profile().schema.value)

    def check(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert set(node["required"]) == set(node["properties"])
                assert node["additionalProperties"] is False
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)

    check(schema)


def test_all_citations_are_bound_once_in_transport_schema():
    source = {
        "type": "object",
        "properties": {
            "eventIds": {"type": "array", "items": {"type": "string"}},
            "eventId": {"type": "string", "enum": ["original"]},
        },
        "required": ["eventIds", "eventId"],
    }
    schema = strict_schema(source, ["full-hash:L000001", "full-hash:L000002"])
    assert schema["properties"]["eventIds"]["items"] == {"$ref": "#/$defs/sourceEventId"}
    assert schema["properties"]["eventId"] == {"$ref": "#/$defs/sourceEventId"}
    assert schema["$defs"]["sourceEventId"]["enum"] == ["full-hash:L000001", "full-hash:L000002"]
    assert "enum" not in source["properties"]["eventIds"]["items"]


def test_merge_repair_receives_authorized_source_ids():
    from tkn_codex_chat_note.prompting import render_repair_prompt
    from tkn_codex_chat_note.summary_resources import load_summary_profile

    text = render_repair_prompt(
        load_summary_profile().prompt,
        thread_id="example",
        validation_error="unknown source",
        draft={},
        allowed_event_ids=["valid"],
    )
    payload = json.loads(text.split("BEGIN_INPUT_JSON\n")[1].split("\nEND_INPUT_JSON")[0])
    assert payload["allowedEventIds"] == ["valid"] and "events" not in payload


def test_short_wire_ids_round_trip_without_rewriting_narrative(fake_http):
    from tkn_codex_chat_note.api_inference import alias_prompt, remap_event_ids

    calls, replies = fake_http
    identifier = "long-source-hash:L000001"
    payload = {"events": [{"id": identifier, "text": identifier}], "partials": [{"eventIds": [identifier]}]}
    prompt = "BEGIN_INPUT_JSON\n" + json.dumps(payload) + "\nEND_INPUT_JSON"
    shortened = alias_prompt(prompt, {identifier: "E00001"})
    transformed = json.loads(shortened.split("\n")[1])
    assert transformed["events"][0] == {"id": "E00001", "text": identifier}
    assert transformed["partials"][0]["eventIds"] == ["E00001"]
    assert remap_event_ids(transformed, {"E00001": identifier}) == payload
    reply = response()
    reply["choices"][0]["message"]["content"] = '{"eventIds":["E00001"],"text":"E00001"}'
    replies.append(reply)
    client = ApiClient(settings())
    client.allowed_ids = [identifier]
    value = client.invoke(prompt, SCHEMA, timeout=30)
    assert value == {"eventIds": [identifier], "text": "E00001"}
    assert client.records[0]["requestEncoding"] == "event-id-aliases-v1"


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({"Retry-After": "12"}, 12),
        ({"retry-after-ms": "1250"}, 1.25),
        ({"Retry-After": "nan"}, 0),
        ({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}, 0),
    ],
)
def test_retry_delay_formats(headers, expected):
    assert api_inference.retry_delay(httpx.Headers(headers)) == expected


def test_schema_property_names_are_not_removed():
    schema = {"type": "object", "properties": {"pattern": {"type": "string", "minLength": 1}}}
    assert strict_schema(schema)["properties"] == {"pattern": {"type": "string"}}


def test_multi_source_command_shares_api_budget(tmp_path, monkeypatch):
    from test_multi_sources import multi_config, source_chats

    from tkn_codex_chat_note import pipeline

    cfg = multi_config(tmp_path)
    source_chats(cfg)
    cfg.generation = GenerationConfig(
        active_provider="azure-openai",
        providers={"azure-openai": {"model": "example-model", "azure": AZURE, "limits": {}}},
    )
    clients, source_ids = [], []

    def run(source, **kwargs):
        runner = kwargs["summarizer"]
        clients.append(runner.api_client)
        source_ids.append(runner.config.source_id)
        return {
            "ok": True,
            "complete": True,
            "threadCounts": {},
            "generatedSessionNoteCount": 0,
            "attemptedSessionNoteCount": 0,
            "failed": [],
            "sourceProvider": "codex",
            "sourceId": source.source_id,
        }

    monkeypatch.setattr(pipeline, "_run_source_pipeline", run)
    assert pipeline.run_pipeline(cfg, mode="clone")["ok"]
    assert clients[0] is clients[1] and source_ids == list(cfg.sources)


def test_azure_dry_run_never_authenticates_or_writes(tmp_path, monkeypatch):
    from test_multi_sources import multi_config, source_chats

    from tkn_codex_chat_note.pipeline import run_pipeline

    cfg = multi_config(tmp_path)
    source_chats(cfg)
    cfg.generation = GenerationConfig(
        active_provider="azure-openai",
        providers={"azure-openai": {"model": "example-model", "azure": AZURE, "limits": {}}},
    )
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with (patch.object(ApiClient, "invoke", side_effect=AssertionError("API invocation")),
          patch.object(api_inference, "token_provider", side_effect=AssertionError("authentication"))):
        report = run_pipeline(cfg, mode="clone", dry_run=True)
    assert report["ok"] and report["threadCounts"] == {"planned": 2}
    assert report["generationEstimate"]["baseCostCeilingJpy"] > 0
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_azure_note_can_pass_strict_staged_validation(tmp_path):
    from test_session_note_pipeline import note_data

    from tkn_codex_chat_note.session_notes import render_note, validate_staged_session_notes

    case = replace(candidate(tmp_path, (event("user"),)), source_ref="codex/thread")
    cfg = replace(config(tmp_path), provider="azure-openai", inference_options=settings().inference_options)
    data = note_data(case)
    data.update(
        _generator="Azure OpenAI",
        _generatorProvider=cfg.provider,
        _generatorModel="response-model-2026-07-09",
        _generatorDeployment=cfg.model,
        _generatorReasoningEffort=cfg.reasoning_effort,
    )
    rendered = render_note(case, data, {}, profile=cfg.summary_profile)
    note = tmp_path / "20260101T090000+0900-automated-session-note.md"
    note.write_text(rendered, encoding="utf-8")
    notes, hashes = validate_staged_session_notes(tmp_path, [case], cfg, strict_threads={case.thread_id})
    assert notes[case.thread_id] == note.name and hashes[case.thread_id]
    # Provider options belong to the generation identity/provenance, not scalar note metadata.
    assert "generatorOptions:" not in rendered
