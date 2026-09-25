"""Application adapter for tkn_genai_bridge; no provider execution lives here."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from tkn_genai_bridge import (
    AzureSettings,
    CliSettings,
    GenAIError,
    GenerationRecord,
    GenerationRequest,
    OllamaSettings,
    Profile,
    ProviderError,
    Runtime,
)
from tkn_genai_bridge.models import PROVIDER_NAMES, Provider
from tkn_genai_bridge.providers.cli import resolve_executable

from .api_settings import AzurePricing
from .cost_policy import usage_fields
from .usage_records import Observer, new_record, timestamp

InferenceProvider = Provider


class InferenceConfig(Protocol):
    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def reasoning_effort(self) -> str: ...


class InferenceExecutionError(RuntimeError):
    """A content-free execution failure presented by this application."""


def provider_name(provider: str) -> str:
    try:
        return PROVIDER_NAMES[provider]
    except KeyError as exc:
        raise InferenceExecutionError(f"unsupported inference provider: {provider}") from exc


def is_supported_generator(value: str) -> bool:
    return value in PROVIDER_NAMES.values()


def validate_ollama_base_url(value: str) -> str:
    return OllamaSettings(base_url=value).base_url


def resolve_provider_executable(value: str, *, provider: str) -> str:
    """Legacy initialization adapter; Bridge owns executable discovery and checks."""
    try:
        return resolve_executable(Profile.model_validate({
            "provider": {"claude": "claude-code", "copilot": "github-copilot"}.get(provider, provider),
            "cli": {"executable": value},
        }))
    except GenAIError as exc:
        raise InferenceExecutionError(str(exc)) from exc


def bridge_profile(config: InferenceConfig) -> Profile:
    """Read a resolved shared profile, or translate a legacy application's config."""
    options = getattr(config, "inference_options", {})
    if "bridge" in options:
        return Profile.model_validate(options["bridge"])
    settings: dict[str, Any] = {
        "provider": config.provider, "model": config.model,
        "timeout_seconds": getattr(config, "model_timeout_seconds", 1800),
    }
    if config.provider == "ollama":
        think = (config.reasoning_effort if config.model.casefold().split(":", 1)[0] == "gpt-oss"
                 else config.reasoning_effort != "low")
        settings["ollama"] = {
            "base_url": getattr(config, "ollama_base_url", "http://127.0.0.1:11434"), "think": think,
        }
    elif config.provider == "azure-openai":
        azure = options["azure"]
        settings["azure"] = AzureSettings(
            endpoint=azure["endpoint"], auth="interactive_browser", tenant_id=azure.get("tenant_id"),
            token_scope="https://ai.azure.com/.default",
        ).model_dump()
        settings["reasoning_effort"] = config.reasoning_effort
        settings["pricing"] = {model: AzurePricing.model_validate(price).bridge_pricing().model_dump()
                               for model, price in azure.get("pricing", {}).items()}
    else:
        attribute = {"codex": "codex_bin", "claude-code": "claude_bin", "github-copilot": "copilot_bin"}
        executable = (options.get("cli_executable") if config.provider == "antigravity"
                      else getattr(config, attribute[config.provider], None))
        settings["cli"] = CliSettings(executable=executable).model_dump()
        settings["reasoning_effort"] = config.reasoning_effort
    return Profile.model_validate(settings)


def update_usage(record: dict[str, Any], value: GenerationRecord) -> None:
    """Keep the existing journal/report contract, including unknown token counts."""
    record.update(usage_fields(value.usage))
    cost = value.cost_estimate
    record.update(
        model=value.response_model,
        bridgeUsage=value.usage.model_dump(mode="json"),
        costEstimate=cost.model_dump(mode="json") if cost else None,
        estimatedCostJpy=cost.amount if cost and cost.currency == "JPY" else None,
        usageSource="tkn-genai-bridge", usageScope="invocation",
        durationSeconds=value.duration_seconds, bridgeVersion=value.bridge_version,
        bridgeProfile=value.profile_name, generationSettingsSha256=value.generation_settings_sha256,
        promptSha256=value.prompt_sha256, schemaSha256=value.schema_sha256, errorCode=value.error_code,
    )


def invoke_structured(
    config: InferenceConfig, prompt: str, schema: dict[str, Any], *, cwd: Path, timeout: int,
    usage_observer: Observer | None = None,
) -> dict[str, Any]:
    """One structured call; Bridge owns temporary directories, parsing and validation."""
    profile = bridge_profile(config)
    profile = profile.model_copy(update={"timeout_seconds": min(profile.timeout_seconds, timeout)})
    record = new_record(config.provider, config.model, config.reasoning_effort)
    if usage_observer:
        usage_observer({"type": "usage-start", **record})
    try:
        with Runtime(profile, profile_name=getattr(config, "inference_options", {}).get("bridge_profile")) as runtime:
            request = GenerationRequest(prompt=prompt, output_schema=schema, schema_name="session_note")
            result = runtime.generate(request)
        update_usage(record, result.record)
        record["status"] = "received"
        return result.data
    except GenAIError as exc:
        if exc.record is not None:
            update_usage(record, exc.record)
        if isinstance(exc, ProviderError):
            record.update(httpStatus=exc.http_status, retryAfterSeconds=exc.retry_after_seconds,
                          submissionUnknown=exc.submission_unknown)
        record["status"] = "failed"
        raise InferenceExecutionError(str(exc)) from exc
    except BaseException:
        record["status"] = "failed"
        raise
    finally:
        record["finishedAt"] = timestamp()
        if usage_observer:
            usage_observer({"type": "usage-complete", **record})
