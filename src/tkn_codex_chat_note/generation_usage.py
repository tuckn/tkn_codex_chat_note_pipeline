"""Analysis-friendly totals that preserve missing usage rather than treating it as zero."""

from __future__ import annotations

from typing import Any


def usage_totals(records: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {"requestCount": len(records)}
    for key in (
        "inputTokens", "outputTokens", "reasoningTokens", "cachedInputTokens", "cacheWriteTokens", "estimatedCostJpy",
    ):
        known = [r[key] for r in records if r.get(key) is not None]
        result[key] = sum(known) if len(known) == len(records) else None
        known_key = "known" + key[0].upper() + key[1:]
        result[known_key] = sum(known) + sum(r.get(known_key, 0) for r in records if r.get(key) is None)
        result[key + "MissingRequests"] = len(records) - len(known)
    known_reserved = [r["reservedCostJpy"] for r in records if r.get("reservedCostJpy") is not None]
    result["reservedCostJpy"] = sum(known_reserved) if len(known_reserved) == len(records) else None
    return result


def metric_records(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    """New unified records supersede their API-only compatibility view."""
    return list(metrics.get("usageRecords", metrics.get("apiRequests", [])))


def estimate_totals(estimates: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {"threadCount": len(estimates)}
    for key in (
        "preparedTextCharacters",
        "compactedCharacters",
        "deduplicatedCharacters",
        "pendingPromptCharacters",
        "pendingChunkCount",
        "cachedChunkCount",
        "baseCalls",
    ):
        result[key] = sum(e.get(key, 0) for e in estimates)
    for key in ("inputTokensEstimate", "outputTokensCeiling", "baseCostCeilingJpy"):
        known = [e[key] for e in estimates if e.get(key) is not None]
        result[key] = sum(known) if len(known) == len(estimates) else None
    for key in ("commandMaxCalls", "commandMaxCostJpy"):
        values = {e.get(key) for e in estimates}
        result[key] = next(iter(values)) if len(values) == 1 else None
    result["mayExceedCommandBudget"] = bool(
        result.get("commandMaxCalls") is not None
        and result["baseCalls"] > result["commandMaxCalls"]
        or result.get("commandMaxCostJpy") is not None
        and result.get("baseCostCeilingJpy") is not None
        and result["baseCostCeilingJpy"] > result["commandMaxCostJpy"]
    )
    result["excludesRetries"] = True
    result["mergeUsesInputCeiling"] = any(e.get("mergeUsesInputCeiling") for e in estimates)
    result["unavailableCount"] = sum(e.get("status") == "unavailable" for e in estimates)
    return result
