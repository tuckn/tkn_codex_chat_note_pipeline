from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
import pytest
from pydantic import ValidationError
from tkn_genai_bridge.providers import litellm as bridge_backend

from tkn_codex_chat_note import api_inference
from tkn_codex_chat_note.api_inference import ApiClient, ApiError, strict_schema
from tkn_codex_chat_note.api_settings import ApiLimits, AzureSettings
from tkn_codex_chat_note.config import GenerationConfig

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

    bridge_backend.load_sdk()
    class FakeClient(actual_client):
        def __init__(self, **kwargs):
            super().__init__(**{**kwargs, "transport": httpx.MockTransport(handle)})
    monkeypatch.setattr(api_inference.httpx, "Client", FakeClient)
    monkeypatch.setattr(bridge_backend, "azure_headers", Mock(return_value={"Authorization": "Bearer test-token"}))
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


@pytest.mark.parametrize("status,retry", [(400, False), (401, False), (403, False), (429, False), (503, False)])
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
    bridge_backend.azure_headers.assert_not_called()
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


def test_complete_review_schema_transport_shape():
    from tkn_codex_chat_note.review_model import ReviewDraft

    schema = strict_schema(ReviewDraft.model_json_schema())

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
    value = client.invoke(prompt, {"type": "object"}, timeout=30)
    assert value == {"eventIds": [identifier], "text": "E00001"}
    assert client.records[0]["requestEncoding"] == "event-id-aliases-v1"


def test_schema_property_names_are_not_removed():
    schema = {"type": "object", "properties": {"pattern": {"type": "string", "minLength": 1}}}
    assert strict_schema(schema)["properties"] == {"pattern": {"type": "string"}}


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
          patch.object(bridge_backend, "azure_headers", side_effect=AssertionError("authentication"))):
        report = run_pipeline(cfg, mode="clone", dry_run=True)
    assert report["ok"] and report["threadCounts"] == {"current": 2}
    assert "generationEstimate" not in report
    assert report["normalizedThreadCount"] == 2
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
