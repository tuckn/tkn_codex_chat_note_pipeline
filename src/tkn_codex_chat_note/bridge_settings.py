"""Application policy associated with a named shared Bridge profile."""

from __future__ import annotations

from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator
from tkn_genai_bridge import AzureSettings as BridgeAzureSettings
from tkn_genai_bridge import GenAIError, GenerationRequest, Profile, Runtime, load_profile
from tkn_genai_bridge.models import Provider

from .api_settings import ApiLimits, AzurePricing


class BridgeProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bridge_profile: str = Field(min_length=1)
    pricing: dict[str, AzurePricing] = Field(default_factory=dict)
    limits: ApiLimits | None = None
    model_digest: str | None = None
    overrides: dict[str, Any] = Field(default_factory=dict)
    _profile: Profile | None = PrivateAttr(default=None)

    @property
    def profile(self) -> Profile:
        if self._profile is None:
            try:
                self._profile = load_profile(self.bridge_profile, overrides=self.overrides)
                prices = dict(self._profile.pricing)
                for model, legacy in self.pricing.items():
                    price = legacy.bridge_pricing()
                    if model in prices and prices[model] != price:
                        raise ValueError("conflicting application/Bridge pricing; keep rates in the Bridge profile")
                    prices[model] = price
                self._profile = self._profile.model_copy(update={"pricing": prices})
            except GenAIError as exc:
                raise ValueError(f"Bridge profile {self.bridge_profile!r}: {exc}") from None
        return self._profile

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        profile = self.profile
        if self.pricing and profile.provider != "azure-openai":
            raise ValueError("pricing requires azure-openai")
        if self.model_digest is not None and profile.provider != "ollama":
            raise ValueError("model_digest requires ollama")
        if self.limits is not None and profile.provider not in {"ollama", "azure-openai"}:
            raise ValueError("limits require an API provider")
        if profile.provider in {"azure-openai", "ollama"} and self.limits is None:
            self.limits = ApiLimits()
        return self

    @property
    def provider(self) -> Provider:
        return self.profile.provider

    @property
    def model(self) -> str:
        return self.profile.model or "provider-default"

    @property
    def reasoning_effort(self) -> str:
        return self.profile.reasoning_effort or "provider-default"

    @property
    def executable(self) -> str | None:
        return self.profile.cli.executable if self.profile.cli else None

    @property
    def endpoint(self) -> str | None:
        if self.profile.azure:
            return self.profile.azure.endpoint
        return self.profile.ollama.base_url if self.profile.ollama else None

    @property
    def azure(self) -> BridgeAzureSettings | None:
        return self.profile.azure

    def inference_options(self) -> dict[str, Any]:
        with Runtime(self.profile) as runtime:
            plan = runtime.plan(GenerationRequest(prompt="identity", output_schema={"type": "object"},
                                                  schema_name="session_note"))
        result: dict[str, Any] = {
            "bridge": self.profile.model_dump(mode="json"),
            "bridge_profile": self.bridge_profile,
            "bridge_version": plan.bridge_version,
            "generation_settings_sha256": plan.generation_settings_sha256,
        }
        if self.azure is not None:
            result["azure"] = {
                "endpoint": self.azure.endpoint,
            }
        if self.limits is not None:
            result["limits"] = self.limits.model_dump(mode="json")
        if self.model_digest is not None:
            result["model_digest"] = self.model_digest
        return result
