"""Caller-owned reservation assumptions and adapters; Bridge calculates every amount."""

from __future__ import annotations

from typing import Any

from tkn_genai_bridge import CostEstimate, TokenPricing, Usage, estimate_cost


def reservation_cost(input_tokens: int, output_tokens: int, pricing: TokenPricing | None,
                     *, model: str) -> CostEstimate:
    """Reserve the most expensive configured input category, without assuming cache savings."""
    scenarios = [Usage(input_tokens=input_tokens, output_tokens=output_tokens,
                       cached_input_tokens=0, cache_write_tokens=0)]
    if pricing is not None and pricing.cache_policy == "observed":
        scenarios.append(scenarios[0].model_copy(update={"cached_input_tokens": input_tokens}))
        if pricing.cache_write_per_million is not None:
            scenarios.append(scenarios[0].model_copy(update={"cache_write_tokens": input_tokens}))
    costs = [estimate_cost(usage, pricing, basis="planned", pricing_model=model) for usage in scenarios]
    # An unavailable category must not disappear when selecting a reservation.
    return next((cost for cost in costs if cost.amount is None),
                max(costs, key=lambda cost: cost.amount if cost.amount is not None else -1))


def record_usage(record: dict[str, Any]) -> Usage:
    """Read new raw Bridge usage, or the previous journal's token fields."""
    if record.get("bridgeUsage") is not None:
        return Usage.model_validate(record["bridgeUsage"])
    # Bridge 0.4 Claude counts excluded caches but did not retain their scope/writes here.
    legacy_claude = record.get("provider") == "claude-code" and record.get("usageSource") == "tkn-genai-bridge"
    return Usage(input_tokens=record.get("inputTokens"), output_tokens=record.get("outputTokens"),
                 cached_input_tokens=record.get("cachedInputTokens"),
                 cache_write_tokens=record.get("cacheWriteTokens"),
                 reasoning_tokens=record.get("reasoningTokens"),
                 input_tokens_scope="uncached" if legacy_claude else "total")


def total_input(usage: Usage) -> tuple[int | None, int]:
    """Return the full input total and its known subtotal without counting either twice."""
    names = ["input_tokens"]
    if usage.input_tokens_scope == "uncached":
        names += ["cached_input_tokens", "cache_write_tokens"]
    totals = [getattr(usage, name) for name in names]
    known = sum(value if value is not None else (getattr(usage.known_subtotal, name, None) or 0)
                for name, value in zip(names, totals, strict=True))
    return (sum(totals) if all(value is not None for value in totals) else None), known


def usage_fields(usage: Usage) -> dict[str, Any]:
    """Map full totals and partial observations to the journal and report contracts."""
    total, known = total_input(usage)
    result: dict[str, Any] = {
        "inputTokens": total, "knownInputTokens": known, "usageCompleteness": usage.completeness,
        "usageComplete": usage.completeness == "complete" and total is not None and usage.output_tokens is not None,
    }
    for field, attribute in (
        ("outputTokens", "output_tokens"), ("cachedInputTokens", "cached_input_tokens"),
        ("reasoningTokens", "reasoning_tokens"), ("cacheWriteTokens", "cache_write_tokens"),
    ):
        value = getattr(usage, attribute)
        result[field] = value
        result["known" + field[0].upper() + field[1:]] = (
            value if value is not None else (getattr(usage.known_subtotal, attribute, None) or 0)
        )
    return result
