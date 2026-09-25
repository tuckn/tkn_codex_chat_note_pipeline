"""Bridge costs must preserve the caller's budgets, history and missing-data semantics."""

import json
from copy import deepcopy

import pytest
import yaml
from test_bridge_settings import application, fingerprint, shared
from test_usage_report import record, saved
from tkn_genai_bridge import ProviderError, ResponseMetadata, TokenCounts, TokenPricing, Usage
from tkn_genai_bridge.providers.base import ProviderResponse

from tkn_codex_chat_note.api_inference import ApiClient, ApiError
from tkn_codex_chat_note.config import load_app_config
from tkn_codex_chat_note.cost_policy import reservation_cost
from tkn_codex_chat_note.report_settings import BridgePriceScenario
from tkn_codex_chat_note.session_notes import PipelineError
from tkn_codex_chat_note.usage_report import _normalize, build_usage_report, reference_cost

PRICE = {"currency": "JPY", "pricing_date": "2026-01-01", "input_per_million": 30,
         "output_per_million": 180}
PROFILE = {"provider": "azure-openai", "model": "deployment", "max_output_tokens": 512,
           "azure": {"endpoint": "https://example.openai.azure.com/openai/v1"},
           "pricing": {"deployment": PRICE}}


def configured(tmp_path, **price_updates):
    profile = deepcopy(PROFILE)
    profile["pricing"]["deployment"].update(price_updates)
    shared({"shared-notes": profile})
    cfg = load_app_config(explicit_path=application(tmp_path), cwd=tmp_path)
    return cfg, ApiClient(cfg.session_note_pipeline_config(allow_missing_watermark=True))


def backend(client, usage, *, fail=False):
    class Backend:
        def generate(self, profile, request):
            if fail:
                error = ProviderError("synthetic failure")
                error.metadata = ResponseMetadata(response_model="actual-model", usage=usage)
                raise error
            return ProviderResponse({"ok": "yes"}, response_model="actual-model", usage=usage)
    client.runtime.backend = Backend()
    client.estimate = lambda *args: 1000


@pytest.mark.parametrize("fail", [False, True])
def test_shared_prices_drive_reservation_and_reported_cost_including_failures(tmp_path, fail):
    _, client = configured(tmp_path)
    backend(client, Usage(input_tokens=100, output_tokens=20), fail=fail)
    if fail:
        with pytest.raises(ApiError):
            client.invoke("test", {"type": "object"}, timeout=10)
    else:
        client.invoke("test", {"type": "object"}, timeout=10)
    row = client.records[0]
    assert row["reservedCostJpy"] == pytest.approx(0.12216)  # Shared output cap 512, not app's 16000.
    assert row["estimatedCostJpy"] == pytest.approx(0.0066)
    assert row["costEstimate"]["pricing"] == client.pricing.model_dump()
    assert row["costEstimate"]["basis"] == "reported"
    assert row["plannedCostEstimate"]["usage"]["output_tokens"] == 512


def test_cache_usage_and_unknowns_survive_into_report(tmp_path):
    _, client = configured(tmp_path, cache_policy="observed", cached_input_per_million=3,
                           cache_write_per_million=60)
    backend(client, Usage(input_tokens=1000, cached_input_tokens=600, cache_write_tokens=100, output_tokens=200))
    client.invoke("test", {"type": "object"}, timeout=10)
    row = client.records[-1]
    assert row["estimatedCostJpy"] == pytest.approx(0.0528)
    assert row["cacheWriteTokens"] == 100
    assert row["plannedCostEstimate"]["usage"]["cache_write_tokens"] == 1000
    backend(client, Usage(input_tokens=1000, output_tokens=200))
    client.invoke("test", {"type": "object"}, timeout=10)
    assert client.records[-1]["estimatedCostJpy"] is None
    assert client.records[-1]["costEstimate"]["unavailable_reason"] == "cache_usage_missing"


def test_currency_cannot_silently_disable_jpy_budget(tmp_path):
    with pytest.raises(ApiError, match="requires JPY"):
        configured(tmp_path, currency="USD")


def test_explicit_zero_rate_remains_known_and_budgeted(tmp_path):
    _, client = configured(tmp_path, input_per_million=0, output_per_million=0)
    backend(client, Usage(input_tokens=100, output_tokens=20))
    client.invoke("test", {"type": "object"}, timeout=10)
    assert client.records[0]["reservedCostJpy"] == client.records[0]["estimatedCostJpy"] == 0
    assert client.records[0]["costBudgetEnforced"] is True


def test_price_update_changes_amount_without_regenerating_notes_or_input_estimate(tmp_path):
    cfg, client = configured(tmp_path)
    identity = fingerprint(cfg)
    tokens = client.estimate("test", {"type": "object"})
    changed, changed_client = configured(tmp_path, input_per_million=90, pricing_date="2026-02-01")
    assert fingerprint(changed) == identity
    assert changed_client.estimate("test", {"type": "object"}) == tokens
    assert changed_client.pricing.input_per_million == 90
    override = load_app_config(explicit_path=tmp_path / "app.yaml", cwd=tmp_path,
                               overrides={"model": "unpriced"})
    assert ApiClient(override.session_note_pipeline_config(allow_missing_watermark=True)).pricing is None


def test_conflicting_legacy_and_shared_prices_rejected(tmp_path):
    configured(tmp_path)
    app = application(tmp_path, pricing={"deployment": {"input_jpy_per_million": 1,
                      "output_jpy_per_million": 2, "pricing_date": "2026-01-01"}})
    with pytest.raises(PipelineError, match="conflicting"):
        load_app_config(explicit_path=app, cwd=tmp_path)


def test_reservation_also_covers_expensive_cache_reads_and_overflow():
    pricing = TokenPricing(**{**PRICE, "cache_policy": "observed", "cached_input_per_million": 90})
    assert reservation_cost(1000, 200, pricing, model="deployment").amount == pytest.approx(0.126)
    overflow = pricing.model_copy(update={"input_per_million": 1e308})
    assert reservation_cost(10**20, 200, overflow, model="deployment").amount is None


def test_claude_uncached_counts_preserve_raw_and_total_without_double_counting():
    raw = Usage(input_tokens=300, cached_input_tokens=600, cache_write_tokens=100,
                output_tokens=200, input_tokens_scope="uncached")
    row = record(provider="claude-code", usageSource="tkn-genai-bridge", inputTokens=1000,
                 cacheWriteTokens=100, bridgeUsage=raw.model_dump())
    normalized = _normalize(row, 0)
    assert normalized["inputTokens"] == 1000 and normalized["bridgeUsage"]["input_tokens"] == 300
    assert reference_cost(normalized, TokenPricing(**PRICE)) == pytest.approx(0.066)
    del row["bridgeUsage"]
    row.update(inputTokens=300, cacheWriteTokens=None)
    old = _normalize(row, 0)
    assert old["inputTokens"] is None and old["knownInputTokens"] == 900
    assert reference_cost(old, TokenPricing(**PRICE)) is None


def test_report_shared_scenario_resolves_and_preserves_price_snapshot(tmp_path):
    configured(tmp_path)
    cfg, _, _ = saved(tmp_path)
    cfg.usage_report.price_scenarios = {
        "shared": BridgePriceScenario(bridge_profile="shared-notes", model="deployment")}
    result = build_usage_report(cfg, no_open=True)
    with open(result["jsonPath"], encoding="utf-8") as stream:
        data = json.load(stream)
    assert data["records"][0]["referenceCosts"]["shared"] == pytest.approx(0.066)
    assert data["records"][0]["referenceCostDetails"]["shared"]["basis"] == "scenario"
    assert data["priceScenarios"]["shared"]["currency"] == "JPY"
    with pytest.raises(ValueError, match="no pricing"):
        BridgePriceScenario(bridge_profile="shared-notes", model="unknown")


def test_report_scenarios_load_shared_references_and_legacy_yaml_dates(tmp_path):
    configured(tmp_path)
    path = tmp_path / "app.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["usage_report"] = {"price_scenarios": {
        "shared": {"bridge_profile": "shared-notes", "model": "deployment"},
        "legacy": yaml.safe_load("pricing_date: 2026-01-01\ninput_per_million: 30\noutput_per_million: 180\n"),
    }}
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    cfg = load_app_config(explicit_path=path, cwd=tmp_path)
    assert cfg.usage_report.price_scenarios["shared"].pricing.currency == "JPY"
    assert cfg.usage_report.price_scenarios["legacy"].pricing_date == "2026-01-01"


@pytest.mark.parametrize("scope, expected_input", [("total", 300), ("uncached", 1000)])
def test_partial_usage_survives_failure_journal_and_report(tmp_path, scope, expected_input):
    from tkn_codex_chat_note.generation_usage import usage_totals

    _, client = configured(tmp_path)
    usage = Usage(completeness="partial", input_tokens_scope=scope, known_subtotal=TokenCounts(
        input_tokens=300, output_tokens=200, cached_input_tokens=600 if scope == "uncached" else 100,
        cache_write_tokens=100, reasoning_tokens=50, input_tokens_scope=scope))
    backend(client, usage, fail=True)
    with pytest.raises(ApiError):
        client.invoke("test", {"type": "object"}, timeout=10)
    row = client.records[-1]
    assert row["usageCompleteness"] == "partial" and not row["usageComplete"]
    assert row["inputTokens"] is row["outputTokens"] is row["estimatedCostJpy"] is None
    assert row["knownInputTokens"] == expected_input and row["knownOutputTokens"] == 200
    assert row["costEstimate"]["unavailable_reason"] == "usage_incomplete"
    cfg, _, path = saved(tmp_path, **row)
    original = path.read_bytes()
    result = build_usage_report(cfg, no_open=True)
    with open(result["jsonPath"], encoding="utf-8") as stream:
        payload = json.load(stream)
    normalized = payload["records"][0]
    assert normalized["bridgeUsage"] == usage.model_dump(mode="json")
    assert normalized["knownReasoningTokens"] == 50 and normalized["knownCacheWriteTokens"] == 100
    assert normalized["knownInputTokens"] == expected_input and normalized["knownOutputTokens"] == 200
    assert normalized["usageCompleteness"] == "partial" and not normalized["usageComplete"]
    assert reference_cost(normalized, TokenPricing(**PRICE)) is None
    totals = usage_totals([normalized, record()])
    assert totals["inputTokens"] is None and totals["knownInputTokens"] == expected_input + 1000
    assert totals["outputTokens"] is None and totals["knownOutputTokens"] == 400
    assert path.read_bytes() == original


def test_complete_usage_and_known_subtotal_are_not_added_twice():
    from tkn_codex_chat_note.cost_policy import usage_fields

    usage = Usage(input_tokens=100, output_tokens=20, known_subtotal=TokenCounts(input_tokens=60, output_tokens=10))
    values = usage_fields(usage)
    assert values["knownInputTokens"] == values["inputTokens"] == 100
    assert values["knownOutputTokens"] == values["outputTokens"] == 20
    assert values["usageComplete"] and values["usageCompleteness"] == "complete"
    old = _normalize(record(bridgeUsage={"input_tokens": 100, "output_tokens": 20}), 0)
    assert old["inputTokens"] == 100 and old["usageCompleteness"] == "complete"
    unknown = _normalize(record(bridgeUsage=Usage().model_dump()), 0)
    assert unknown["inputTokens"] is None and unknown["knownInputTokens"] == 0
    assert unknown["outputTokens"] is None and unknown["knownOutputTokens"] == 0
    assert unknown["usageCompleteness"] == "unknown" and not unknown["usageComplete"]
