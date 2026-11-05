"""GenAI connection profiles defined in this application's own config.yaml.

Each entry under the root ``genai_profiles`` map is a tkn_genai_bridge ``Profile``
(provider, model, reasoning effort, transport and pricing) plus this application's
own API budget (``limits``) and Ollama ``model_digest``. The Bridge library is used
directly; no shared Bridge configuration file is read.
"""

from __future__ import annotations

from typing import Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializationInfo,
    ValidationError,
    model_serializer,
    model_validator,
)
from tkn_genai_bridge import AzureSettings as BridgeAzureSettings
from tkn_genai_bridge import GenAIError, GenerationRequest, Profile, Runtime
from tkn_genai_bridge.models import Provider

from .api_settings import ApiLimits

DEFAULT_PROVIDERS: tuple[tuple[str, Provider], ...] = (
    ("codex-default", "codex"),
    ("claude-default", "claude-code"),
    ("copilot-default", "github-copilot"),
    ("antigravity-default", "antigravity"),
)

APPLICATION_KEYS = ("limits", "model_digest")
API_PROVIDERS = frozenset({"ollama", "azure-openai"})


def default_genai_profiles() -> dict[str, GenAIProfileConfig]:
    """CLI providers that reuse each tool's existing login; no API keys are involved."""
    return {name: GenAIProfileConfig(bridge=Profile(provider=provider)) for name, provider in DEFAULT_PROVIDERS}


def split_profile(value: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Separate Bridge Profile fields from this application's budget fields."""
    application = {key: value[key] for key in APPLICATION_KEYS if key in value}
    bridge = {key: item for key, item in value.items() if key not in APPLICATION_KEYS}
    return bridge, application


def validate_profile_fragment(value: Any, location: str) -> None:
    """Check one configuration layer's fragment before layers are merged."""
    from .config_validation import validate_config_layer

    if not isinstance(value, dict):
        raise ValueError(f"{location} must be a mapping")
    bridge, application = split_profile(value)
    validate_config_layer(Profile, bridge, location)
    if application.get("limits") is not None:
        if not isinstance(application["limits"], dict):
            raise ValueError(f"{location}.limits must be a mapping")
        validate_config_layer(ApiLimits, application["limits"], f"{location}.limits")
    digest = application.get("model_digest")
    if digest is not None and (not isinstance(digest, str) or not digest.strip()):
        raise ValueError(f"{location}.model_digest must be a nonempty string")


class GenAIProfileConfig(BaseModel):
    """One named connection: a Bridge Profile and this application's API budget."""

    model_config = ConfigDict(extra="forbid")

    bridge: Profile
    limits: ApiLimits | None = None
    model_digest: str | None = Field(default=None, min_length=1)

    @model_validator(mode="before")
    @classmethod
    def split_fields(cls, value: Any) -> Any:
        if isinstance(value, Profile):
            return {"bridge": value}
        if not isinstance(value, dict) or "bridge" in value:
            return value
        bridge, application = split_profile(value)
        try:
            profile = Profile.model_validate(bridge)
        except ValidationError as exc:
            details = "; ".join(
                f"{'.'.join(map(str, error['loc'])) or 'profile'}: {error['msg']}"
                for error in exc.errors(include_input=False, include_url=False)
            )
            raise ValueError(f"invalid GenAI profile: {details}") from None
        return {"bridge": profile, **application}

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        provider = self.bridge.provider
        if self.model_digest is not None and provider != "ollama":
            raise ValueError("model_digest requires provider ollama")
        if self.limits is not None and provider not in API_PROVIDERS:
            raise ValueError("limits require an API provider (ollama or azure-openai)")
        if provider in API_PROVIDERS and self.limits is None:
            self.limits = ApiLimits()
        return self

    @model_serializer(mode="plain")
    def flatten(self, info: SerializationInfo) -> dict[str, Any]:
        mode = "json" if info.mode_is_json() else "python"
        result: dict[str, Any] = self.bridge.model_dump(mode=mode)
        if self.limits is not None:
            result["limits"] = self.limits.model_dump(mode=mode)
        if self.model_digest is not None:
            result["model_digest"] = self.model_digest
        return result

    @property
    def provider(self) -> Provider:
        return self.bridge.provider

    @property
    def model(self) -> str:
        return self.bridge.model or "provider-default"

    @property
    def reasoning_effort(self) -> str:
        return self.bridge.reasoning_effort or "provider-default"

    @property
    def executable(self) -> str | None:
        return self.bridge.cli.executable if self.bridge.cli else None

    @property
    def endpoint(self) -> str | None:
        if self.bridge.azure:
            return self.bridge.azure.endpoint
        return self.bridge.ollama.base_url if self.bridge.ollama else None

    @property
    def azure(self) -> BridgeAzureSettings | None:
        return self.bridge.azure

    def inference_options(self, name: str) -> dict[str, Any]:
        """Runtime options for the application's inference adapters; offline."""
        try:
            with Runtime(self.bridge, profile_name=name) as runtime:
                plan = runtime.plan(GenerationRequest(prompt="identity", output_schema={"type": "object"},
                                                      schema_name="review_note"))
        except GenAIError as exc:
            raise ValueError(f"GenAI profile {name!r}: {exc}") from None
        result: dict[str, Any] = {
            "bridge": self.bridge.model_dump(mode="json"),
            "genai_profile": name,
            "bridge_version": plan.bridge_version,
            "generation_settings_sha256": plan.generation_settings_sha256,
        }
        if self.azure is not None:
            result["azure"] = {"endpoint": self.azure.endpoint}
        if self.limits is not None:
            result["limits"] = self.limits.model_dump(mode="json")
        if self.model_digest is not None:
            result["model_digest"] = self.model_digest
        return result
