"""Validated API limits and Azure connection metadata (never credentials)."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AzurePricing(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_jpy_per_million: float = Field(gt=0, allow_inf_nan=False)
    output_jpy_per_million: float = Field(gt=0, allow_inf_nan=False)
    pricing_date: str = Field(min_length=1)

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.input_jpy_per_million + output_tokens * self.output_jpy_per_million) / 1_000_000


class AzureSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: str
    tenant_id: str | None = None
    pricing: dict[str, AzurePricing] = Field(default_factory=dict)

    @field_validator("tenant_id")
    @classmethod
    def guid(cls, value: str | None) -> str | None:
        return str(UUID(value)) if value is not None else None

    @field_validator("endpoint")
    @classmethod
    def endpoint_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.endswith(".openai.azure.com")
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.port not in (None, 443)
            or parsed.path.rstrip("/") != "/openai/v1"
        ):
            raise ValueError("Azure endpoint must be https://<resource>.openai.azure.com/openai/v1/")
        return value.rstrip("/") + "/"


class ApiLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_tokens: int = Field(default=60000, ge=1024)
    output_tokens: int = Field(default=16000, ge=256)
    context_tokens: int = Field(default=100000, ge=2048)
    max_calls: int = Field(default=30, ge=1)
    max_cost_jpy: float = Field(default=100, gt=0, allow_inf_nan=False)
    chunk_characters: int = Field(default=120000, ge=1000)

    @model_validator(mode="after")
    def fits(self) -> Self:
        if self.input_tokens + self.output_tokens > self.context_tokens:
            raise ValueError("input_tokens + output_tokens must fit context_tokens")
        return self


def normalize_azure_generation(value: Any) -> Any:
    """Read legacy schema-7 Azure settings without rewriting the user's file."""
    if not isinstance(value, dict):
        return value
    result = deepcopy(value)
    providers = result.get("providers")
    provider = providers.get("azure-openai") if isinstance(providers, dict) else None
    if not isinstance(provider, dict):
        return result
    azure = provider.get("azure")
    if not isinstance(azure, dict):
        return result
    deployment = azure.pop("deployment", None)
    if deployment is not None:
        provider["model"] = deployment
    azure.pop("model_version", None)
    azure.pop("subscription_id", None)
    keys = ("input_jpy_per_million", "output_jpy_per_million", "pricing_date")
    rates = {key: azure.pop(key) for key in keys if key in azure}
    if rates:
        if not provider.get("model"):
            raise ValueError("legacy Azure prices require the deployment/model in the same configuration layer")
        azure.setdefault("pricing", {}).setdefault(provider["model"], rates)
    return result
