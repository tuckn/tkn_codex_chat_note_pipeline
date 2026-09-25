from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_api_inference import AZURE, SCHEMA, response, settings
from test_api_inference import fake_http as fake_http
from test_session_note_pipeline import note_data
from test_thread_timeline import candidate, config, event

from tkn_codex_chat_note.api_inference import ApiClient, ApiError
from tkn_codex_chat_note.config import GenerationConfig, resolve_app_config
from tkn_codex_chat_note.session_notes import ProviderSummarizer


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


def test_legacy_config_migrates_in_memory_before_runtime_model_override(tmp_path):
    import yaml

    legacy = {
        "schema_version": "7.1.0",
        "generation": {
            "active_provider": "azure-openai",
            "providers": {
                "azure-openai": {
                    "model": "real-model",
                    "reasoning_effort": "high",
                    "azure": {
                        "endpoint": AZURE["endpoint"],
                        "deployment": "notes",
                        "model_version": "old",
                        "subscription_id": "22222222-2222-4222-8222-222222222222",
                        **AZURE["pricing"]["example-model"],
                    },
                }
            },
        },
    }
    path = tmp_path / "legacy.yaml"
    path.write_text(yaml.safe_dump(legacy))
    before = path.read_bytes()
    cfg = resolve_app_config(explicit_path=path, cwd=tmp_path).config
    azure = cfg.generation.profiles["azure-openai"]
    assert azure.model == "notes" and "notes" in azure.azure.pricing
    assert "subscription_id" not in azure.azure.model_dump()
    cfg = resolve_app_config(explicit_path=path, cwd=tmp_path, overrides={"model": "other"}).config
    assert cfg.generation.profiles["azure-openai"].model == "other"
    assert "other" not in cfg.generation.profiles["azure-openai"].azure.pricing
    assert path.read_bytes() == before


def test_checkpoint_reuse_keeps_response_identity_and_detects_later_change(tmp_path, monkeypatch):
    cfg = replace(
        config(tmp_path),
        provider="azure-openai",
        model="notes",
        inference_options={"azure": {"endpoint": AZURE["endpoint"]}},
    )
    case = candidate(tmp_path, (event("one"),))
    runner = ProviderSummarizer(cfg, cache_root=tmp_path / "cache")
    monkeypatch.setattr(ApiClient, "estimate", lambda *args: 1000)

    def invoke(*args, **kwargs):
        runner._api().observe_model("actual-version-A")
        return note_data(case)

    monkeypatch.setattr(runner, "_invoke", invoke)
    runner.generate(case)
    second = ProviderSummarizer(cfg, cache_root=tmp_path / "cache")
    monkeypatch.setattr(second, "_invoke", Mock(side_effect=AssertionError("must reuse")))
    second.generate(case)
    assert second.last_metrics["responseModel"] == "actual-version-A"
    assert second.last_metrics["reusedChunks"] == 1
    with pytest.raises(ApiError, match="earlier stages"):
        second._api().observe_model("actual-version-B")
