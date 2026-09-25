"""Application budgets and event aliases around the shared generation bridge."""

from __future__ import annotations

import json
import time
from copy import deepcopy
from typing import Any

import httpx
from tkn_genai_bridge import GenAIError, GenerationRequest, OutputValidationError, Profile, ProviderError, Runtime
from tkn_genai_bridge.providers.cli import schema_prompt

from .api_settings import ApiLimits
from .cost_policy import reservation_cost
from .inference import InferenceConfig, InferenceExecutionError, bridge_profile, update_usage
from .offline_tokens import token_estimate
from .usage_records import new_record, timestamp


class ApiError(InferenceExecutionError):
    def __init__(self, message: str, *, retryable: bool = False, retry_after: float = 0) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class ApiBudgetExceeded(ApiError):
    """A command-wide stop, not a provider failure or a retryable request."""

    def __init__(self, details: dict[str, Any]) -> None:
        super().__init__(str(details["message"]))
        self.details = deepcopy(details)


def strict_schema(schema: dict[str, Any], allowed_ids: list[str] | None = None) -> dict[str, Any]:
    # These constraints remain enforced by the application validator.
    unsupported = {
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "minimum",
        "maximum",
        "minItems",
        "maxItems",
        "uniqueItems",
    }

    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: ({name: clean(node) for name, node in item.items()} if key == "properties" else clean(item))
                for key, item in value.items()
                if key not in unsupported
            }
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    result = dict(clean(deepcopy(schema)))
    if allowed_ids:
        # One shared enum avoids repeating hundreds of long provenance identifiers.
        def bind(node: Any) -> None:
            if isinstance(node, dict):
                properties = node.get("properties", {})
                if "eventIds" in properties:
                    properties["eventIds"]["items"] = {"$ref": "#/$defs/sourceEventId"}
                if "eventId" in properties:
                    properties["eventId"] = {"$ref": "#/$defs/sourceEventId"}
                for value in node.values():
                    bind(value)
            elif isinstance(node, list):
                for value in node:
                    bind(value)

        bind(result)
        result["$defs"] = {"sourceEventId": {"type": "string", "enum": allowed_ids}}
    return result


def remap_event_ids(value: Any, mapping: dict[str, str], *, field: str = "") -> Any:
    fields = {"id", "eventId", "eventIds", "allowedEventIds", "startEventId", "endEventId", "duplicateOfEventId"}
    if isinstance(value, dict):
        return {key: remap_event_ids(item, mapping, field=key) for key, item in value.items()}
    if isinstance(value, list):
        return [remap_event_ids(item, mapping, field=field) for item in value]
    if isinstance(value, str) and field in fields:
        return mapping.get(value, value)
    return value


def alias_prompt(prompt: str, mapping: dict[str, str]) -> str:
    if not mapping or "BEGIN_INPUT_JSON\n" not in prompt:
        return prompt
    before, rest = prompt.split("BEGIN_INPUT_JSON\n", 1)
    payload, after = rest.split("\nEND_INPUT_JSON", 1)
    value = remap_event_ids(json.loads(payload), mapping)
    # Repair carries the draft and source context; remove JSON whitespace only.
    compact = any(f"MODE: {mode}\n" in before for mode in ("repair-invalid-draft", "regenerate-invalid-output"))
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":") if compact else None)
    return before + "BEGIN_INPUT_JSON\n" + encoded + "\nEND_INPUT_JSON" + after


class ApiClient:
    """One sequential command's budget; reservations include failed/unknown attempts.

    Budget counters are intentionally not shared across processes or invocations.
    Resuming reuses generation checkpoints but starts a new command budget.
    """

    def __init__(self, config: InferenceConfig) -> None:
        self.config = config
        self.options = getattr(config, "inference_options", {})
        self.limits = ApiLimits.model_validate(self.options.get("limits", {}))
        self.azure = self.options["azure"] if config.provider == "azure-openai" else None
        self.profile = self._profile()
        self.runtime = Runtime(self.profile, profile_name=self.options.get("bridge_profile"))
        self.allowed_ids: list[str] = []
        self.stage = "unknown"
        self.records: list[dict[str, Any]] = []
        self.reserved_jpy = 0.0
        self.budget_stop: dict[str, Any] | None = None
        self.observer: Any = None
        self.response_model: str | None = None
        self.pricing = self.profile.pricing.get(config.model)
        if self.pricing is not None and self.pricing.currency != "JPY":
            raise ApiError("max_cost_jpy requires JPY pricing; select a Bridge profile with JPY rates "
                           "(no FX conversion)")

    def observe_model(self, model: Any) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ApiError("Azure response/checkpoint has no model identity; no note accepted")
        if self.response_model is not None and self.response_model != model:
            raise ApiError("Azure deployment returned a different model from earlier stages; rerun with --force")
        self.response_model = model

    def aliases(self) -> dict[str, str]:
        return {identifier: f"E{index:05d}" for index, identifier in enumerate(self.allowed_ids, 1)}

    def _profile(self) -> Profile:
        profile = bridge_profile(self.config)
        values = profile.model_dump()
        # Application ceilings never increase a shared limit.
        values["max_output_tokens"] = min(profile.max_output_tokens or self.limits.output_tokens,
                                          self.limits.output_tokens)
        if profile.provider == "ollama":
            options = dict(values.get("ollama") or {})
            options["context_tokens"] = min(options.get("context_tokens") or self.limits.context_tokens,
                                             self.limits.context_tokens)
            values["ollama"] = options
            self.limits = ApiLimits.model_validate({
                **self.limits.model_dump(),
                "context_tokens": options["context_tokens"],
                "output_tokens": values["max_output_tokens"],
                "input_tokens": min(self.limits.input_tokens,
                                    options["context_tokens"] - values["max_output_tokens"]),
            })
        return Profile.model_validate(values)

    def close(self) -> None:
        self.runtime.close()

    def request(self, prompt: str, schema: dict[str, Any]) -> GenerationRequest:
        mapping = self.aliases()
        return GenerationRequest(
            prompt=alias_prompt(prompt, mapping),
            output_schema=strict_schema(schema, list(mapping.values())),
            schema_name="session_note",
        )

    def body(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        """Offline estimate envelope, not a provider-specific HTTP payload."""
        request = self.request(prompt, schema)
        return {
            "messages": [{"role": "user", "content": schema_prompt(request) if not self.azure else request.prompt}],
            "output_schema": request.output_schema,
            "profile": self.profile.model_dump(exclude={"azure", "cli", "pricing"}),
        }

    def estimate(self, prompt: str, schema: dict[str, Any]) -> int:
        count, self.estimator = token_estimate(json.dumps(self.body(prompt, schema), ensure_ascii=False),
                                               azure=self.azure is not None)
        # Reserve overhead for Bridge's JSON instruction and provider framing.
        return count + 256

    def _check_model_digest(self, timeout: int) -> None:
        if self.config.provider != "ollama" or not self.options.get("model_digest"):
            return
        assert self.profile.ollama is not None
        try:
            with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
                response = client.get(self.profile.ollama.base_url + "/api/tags")
                response.raise_for_status()
                tags = response.json()
            matched = any(m.get("name") == self.config.model and m.get("digest") == self.options["model_digest"]
                          for m in tags.get("models", []))
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            raise ApiError("cannot verify the configured Ollama model digest") from None
        if not matched:
            raise ApiError("Ollama model digest differs from configured model_digest")

    def _stop_budget(self, reason: str, message: str, reserve: float | None) -> None:
        self.budget_stop = {
            "reason": reason, "message": message,
            "requestCount": len(self.records), "maxCalls": self.limits.max_calls,
            "reservedCostJpy": self.reserved_jpy if self.pricing else None,
            "nextCallReserveJpy": reserve,
            "maxCostJpy": self.limits.max_cost_jpy if self.pricing else None,
        }
        raise ApiBudgetExceeded(self.budget_stop)

    def invoke(self, prompt: str, schema: dict[str, Any], *, timeout: int) -> dict[str, Any]:
        if self.budget_stop is not None:
            raise ApiBudgetExceeded(self.budget_stop)
        estimated = self.estimate(prompt, schema)
        if estimated > self.limits.input_tokens:
            raise ApiError(
                f"API input estimate {estimated} exceeds input_tokens={self.limits.input_tokens}; "
                "reduce chunks or raise the explicitly configured limits (also applies to merge/repair)"
            )
        if len(self.records) >= self.limits.max_calls:
            self._stop_budget("api-call-budget",
                              "API command max_calls budget reached; checkpointed work can be resumed", None)
        planned = reservation_cost(estimated, self.profile.max_output_tokens or self.limits.output_tokens,
                                   self.pricing, model=self.config.model)
        if self.pricing is not None and planned.amount is None:
            raise ApiError(f"cannot reserve configured cost: {planned.unavailable_reason}; no request submitted")
        reserve = planned.amount or 0.0
        if self.pricing and self.reserved_jpy + reserve > self.limits.max_cost_jpy:
            self._stop_budget("api-cost-budget",
                              "API command estimated cost budget reached; no request submitted", reserve)
        self._check_model_digest(timeout)
        record: dict[str, Any] = {
            **new_record(self.config.provider, self.config.model, self.config.reasoning_effort),
            "requestEncoding": "event-id-aliases-v1",
            "inputJsonFormat": "compact" if any(f"MODE: {mode}\n" in prompt for mode in (
                "repair-invalid-draft", "regenerate-invalid-output")) else "default",
            "requestSequence": len(self.records) + 1,
            "stage": self.stage,
            "outputTokenLimit": self.profile.max_output_tokens,
            **({"requestedDeployment": self.config.model,
                "pricing": self.pricing.model_dump() if self.pricing else None,
                "costBudgetEnforced": self.pricing is not None} if self.azure else {}),
            "promptCharacters": len(self.body(prompt, schema)["messages"][0]["content"]),
            "inputTokenEstimate": estimated,
            "estimator": getattr(self, "estimator", "test-estimate"),
            "reservedCostJpy": reserve if self.pricing else None,
            "plannedCostEstimate": planned.model_dump(mode="json"),
            "estimatedCostJpy": None,
        }
        self.records.append(record)
        self.reserved_jpy += reserve
        started = time.monotonic()
        if self.observer:
            self.observer({"type": "api-request-start", **record, "commandReservedCostJpy": self.reserved_jpy})
        try:
            # Reuse authentication while respecting a decreasing command deadline.
            self.runtime.profile = self.profile.model_copy(update={
                "timeout_seconds": min(self.profile.timeout_seconds, timeout)})
            result = self.runtime.generate(self.request(prompt, schema))
            update_usage(record, result.record)
            if self.azure:
                self.observe_model(result.record.response_model)
            elif record["inputTokens"] is not None and record["inputTokens"] > self.limits.input_tokens:
                raise ApiError("Ollama reported input usage beyond the configured limit")
            record["status"] = "received"
            return dict(remap_event_ids(result.data, {v: k for k, v in self.aliases().items()}))
        except GenAIError as exc:
            if exc.record is not None:
                update_usage(record, exc.record)
            record["status"] = "failed"
            record["submissionUnknown"] = isinstance(exc, ProviderError) and exc.submission_unknown
            retry_after = 0.0
            if isinstance(exc, ProviderError):
                record.update(httpStatus=exc.http_status, retryAfterSeconds=exc.retry_after_seconds)
                retry_after = exc.retry_after_seconds or 0.0
            # Preserve transport diagnostics; retries remain limited to invalid output.
            raise ApiError(str(exc), retryable=isinstance(exc, OutputValidationError),
                           retry_after=retry_after) from exc
        except BaseException:
            record["status"] = "failed"
            raise
        finally:
            record["finishedAt"] = timestamp()
            record["durationSeconds"] = round(time.monotonic() - started, 3)
            if self.observer:
                self.observer({"type": "api-request-complete", **record})
