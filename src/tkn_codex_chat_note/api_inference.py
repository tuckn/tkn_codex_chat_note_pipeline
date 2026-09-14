"""Bounded Azure/OpenAI v1 and local Ollama inference with per-attempt usage."""

from __future__ import annotations

import json
import math
import time
from copy import deepcopy
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from .api_settings import ApiLimits, AzureSettings
from .azure_auth import token_provider
from .inference import InferenceConfig, InferenceExecutionError, schema_grounded_prompt, validate_ollama_base_url
from .offline_tokens import token_estimate


class ApiError(InferenceExecutionError):
    def __init__(self, message: str, *, retryable: bool = False, retry_after: float = 0) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


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
    compact = "MODE: repair-invalid-draft\n" in before
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":") if compact else None)
    return before + "BEGIN_INPUT_JSON\n" + encoded + "\nEND_INPUT_JSON" + after


def retry_delay(headers: httpx.Headers) -> float:
    value = headers.get("retry-after")
    try:
        if value is not None:
            try:
                seconds = float(value)
            except ValueError:
                seconds = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        else:
            seconds = float(headers.get("retry-after-ms", headers.get("x-ms-retry-after-ms", "0"))) / 1000
        return max(0.0, seconds) if math.isfinite(seconds) else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _number(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


class ApiClient:
    """One sequential command's budget; reservations include failed/unknown attempts.

    Budget counters are intentionally not shared across processes or invocations.
    Resuming reuses generation checkpoints but starts a new command budget.
    """

    def __init__(self, config: InferenceConfig) -> None:
        self.config = config
        self.options = getattr(config, "inference_options", {})
        self.limits = ApiLimits.model_validate(self.options.get("limits", {}))
        self.azure = AzureSettings.model_validate(self.options["azure"]) if config.provider == "azure-openai" else None
        self.allowed_ids: list[str] = []
        self.stage = "unknown"
        self.records: list[dict[str, Any]] = []
        self.reserved_jpy = 0.0
        self.observer: Any = None
        self.response_model: str | None = None
        self.pricing = self.azure.pricing.get(config.model) if self.azure else None

    def observe_model(self, model: Any) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ApiError("Azure response/checkpoint has no model identity; no note accepted")
        if self.response_model is not None and self.response_model != model:
            raise ApiError("Azure deployment returned a different model from earlier stages; rerun with --force")
        self.response_model = model

    def aliases(self) -> dict[str, str]:
        return {identifier: f"E{index:05d}" for index, identifier in enumerate(self.allowed_ids, 1)}

    def body(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        mapping = self.aliases()
        prompt = alias_prompt(prompt, mapping)
        schema = strict_schema(schema, list(mapping.values()))
        if self.azure:
            return {
                "model": self.config.model,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "session_note", "strict": True, "schema": schema},
                },
                "max_completion_tokens": self.limits.output_tokens,
                "reasoning_effort": self.config.reasoning_effort,
                "store": False,
                "stream": False,
            }
        return {
            "model": self.config.model,
            "messages": [{"role": "user", "content": schema_grounded_prompt(prompt, schema)}],
            "format": schema,
            "stream": False,
            "think": self.config.reasoning_effort != "low",
            "options": {
                "temperature": 0,
                "num_ctx": self.limits.context_tokens,
                "num_predict": self.limits.output_tokens,
            },
        }

    def estimate(self, prompt: str, schema: dict[str, Any]) -> int:
        body = json.dumps(self.body(prompt, schema), ensure_ascii=False)
        count, self.estimator = token_estimate(body, azure=self.azure is not None)
        return count

    def invoke(self, prompt: str, schema: dict[str, Any], *, timeout: int) -> dict[str, Any]:
        estimated = self.estimate(prompt, schema)
        if estimated > self.limits.input_tokens:
            raise ApiError(
                f"API input estimate {estimated} exceeds input_tokens={self.limits.input_tokens}; "
                "reduce chunks or raise the explicitly configured limits (also applies to merge/repair)"
            )
        if len(self.records) >= self.limits.max_calls:
            raise ApiError("API command max_calls budget reached; checkpointed work can be resumed")
        reserve = self.pricing.cost(estimated, self.limits.output_tokens) if self.pricing else 0.0
        if self.pricing and self.reserved_jpy + reserve > self.limits.max_cost_jpy:
            raise ApiError("API command estimated cost budget reached; no request submitted")
        headers = {"Content-Type": "application/json"}
        if self.azure:
            try:
                headers["Authorization"] = (
                    "Bearer " + token_provider(self.azure.endpoint, self.azure.tenant_id)()
                )
            except ApiError:
                raise
            except Exception as exc:
                raise ApiError(
                    "Azure browser authentication failed or timed out before submission; complete sign-in and retry"
                ) from exc
            endpoint = self.azure.endpoint + "chat/completions"
        else:
            endpoint = validate_ollama_base_url(self.config.ollama_base_url) + "/api/chat"
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=min(timeout, 10)), follow_redirects=False) as client:
            if not self.azure and self.options.get("model_digest"):
                tags = client.get(validate_ollama_base_url(self.config.ollama_base_url) + "/api/tags").json()
                if not any(
                    m.get("name") == self.config.model and m.get("digest") == self.options["model_digest"]
                    for m in tags.get("models", [])
                ):
                    raise ApiError("Ollama model digest differs from configured model_digest")
            record: dict[str, Any] = {
                "provider": self.config.provider,
                "requestEncoding": "event-id-aliases-v1",
                "inputJsonFormat": "compact" if "MODE: repair-invalid-draft\n" in prompt else "default",
                "requestSequence": len(self.records) + 1,
                "stage": getattr(self, "stage", "unknown"),
                "outputTokenLimit": self.limits.output_tokens,
                **({"requestedDeployment": self.config.model,
                    "pricing": self.pricing.model_dump() if self.pricing else None,
                    "costBudgetEnforced": self.pricing is not None} if self.azure else {}),
                "promptCharacters": len(self.body(prompt, schema)["messages"][0]["content"]),
                "inputTokenEstimate": estimated,
                "estimator": getattr(self, "estimator", "test-estimate"),
                "reservedCostJpy": reserve if self.pricing else None,
                "inputTokens": None,
                "outputTokens": None,
                "reasoningTokens": None,
                "cachedInputTokens": None,
                "cacheWriteTokens": None,
                "estimatedCostJpy": None,
                "model": None,
                "status": "unknown",
                "durationSeconds": None,
            }
            self.records.append(record)
            self.reserved_jpy += reserve
            started = time.monotonic()
            if self.observer:
                self.observer({"type": "api-request-start", **record, "commandReservedCostJpy": self.reserved_jpy})
            try:
                response = client.post(endpoint, json=self.body(prompt, schema), headers=headers)
                record["httpStatus"] = response.status_code
                if response.status_code != 200:
                    retry_after = retry_delay(response.headers)
                    raise ApiError(
                        f"{self.config.provider} returned HTTP {response.status_code}",
                        retryable=response.status_code in {429, 500, 502, 503, 504},
                        retry_after=retry_after,
                    )
                payload = response.json()
                record["model"] = payload.get("model")
                if self.azure:
                    usage = payload.get("usage") or {}
                    record.update(
                        inputTokens=_number(usage.get("prompt_tokens")),
                        outputTokens=_number(usage.get("completion_tokens")),
                        reasoningTokens=_number((usage.get("completion_tokens_details") or {}).get("reasoning_tokens")),
                        cachedInputTokens=_number((usage.get("prompt_tokens_details") or {}).get("cached_tokens")),
                    )
                    if self.pricing and record["inputTokens"] is not None and record["outputTokens"] is not None:
                        record["estimatedCostJpy"] = self.pricing.cost(record["inputTokens"], record["outputTokens"])
                    self.observe_model(payload.get("model"))
                    choice = payload["choices"][0]
                    if choice.get("finish_reason") != "stop" or choice["message"].get("refusal"):
                        raise ApiError("Azure returned a refusal or incomplete response; no note accepted")
                    content = choice["message"]["content"]
                else:
                    record.update(
                        inputTokens=_number(payload.get("prompt_eval_count")),
                        outputTokens=_number(payload.get("eval_count")),
                    )
                    if not payload.get("done") or payload.get("done_reason") == "length":
                        raise ApiError("Ollama returned an incomplete response; no note accepted")
                    if record["inputTokens"] is not None and record["inputTokens"] > self.limits.input_tokens:
                        raise ApiError("Ollama reported input usage beyond the configured limit")
                    content = payload["message"]["content"]
                result = json.loads(content)
                if not isinstance(result, dict):
                    raise ValueError("expected object")
                record["status"] = "received"
                return dict(remap_event_ids(result, {v: k for k, v in self.aliases().items()}))
            except httpx.TransportError as exc:
                record["status"] = "transport-failed"
                raise ApiError(
                    "API transport failed or timed out; billing outcome may be unknown", retryable=True
                ) from exc
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                record["status"] = "invalid-response"
                raise ApiError("API returned invalid JSON or an unexpected response shape") from exc
            except ApiError:
                record["status"] = "rejected"
                raise
            finally:
                record["durationSeconds"] = round(time.monotonic() - started, 3)
                if self.observer:
                    self.observer({"type": "api-request-complete", **record})
