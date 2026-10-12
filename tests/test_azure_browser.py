from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from test_api_inference import AZURE, SCHEMA, response, settings
from test_api_inference import fake_http as fake_http

from tkn_codex_chat_note.api_inference import ApiClient, ApiError
from tkn_codex_chat_note.config import GenerationConfig, resolve_app_config


def test_minimal_configuration_and_price_lookup_follow_deployment(fake_http):
    cfg = GenerationConfig(
        active_provider="azure-openai",
        providers={
            "azure-openai": {
                "model": "new-deployment",
                "reasoning_effort": "high",
                "azure": {"endpoint": AZURE["endpoint"]},
            }
        },
    )
    assert cfg.profiles["azure-openai"].limits is not None
    calls, replies = fake_http
    replies.append(response(model="actual-model-2026-08-01"))
    client = ApiClient(SimpleNamespace(**{**vars(settings()), "model": "new-deployment"}))
    assert client.pricing is None  # Old deployment's rates cannot apply.
    client.invoke("hello", SCHEMA, timeout=30)
    assert json.loads(calls[0].content)["model"] == "new-deployment"
    assert client.records[0]["model"] == "actual-model-2026-08-01"
    assert client.records[0]["estimatedCostJpy"] is None
    assert client.records[0]["reservedCostJpy"] is None
    assert client.records[0]["costBudgetEnforced"] is False


def test_model_change_during_generation_rejected_but_actual_usage_retained(fake_http):
    _, replies = fake_http
    replies.extend([response(), response(model="new-actual-version")])
    client = ApiClient(settings())
    client.invoke("first", SCHEMA, timeout=30)
    with pytest.raises(ApiError, match="earlier stages"):
        client.invoke("second", SCHEMA, timeout=30)
    assert client.records[-1]["model"] == "new-actual-version"
    assert client.records[-1]["inputTokens"] == 100


def test_legacy_schema_requires_explicit_migration_without_writes(tmp_path):
    from tkn_codex_chat_note.runtime import PipelineError

    path = tmp_path / "legacy.yaml"
    path.write_text('schema_version: "7.1.0"\n', encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(PipelineError, match="requires schema 9"):
        resolve_app_config(explicit_path=path, cwd=tmp_path, overrides={"model": "other"})
    assert path.read_bytes() == before
