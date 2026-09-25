"""Exercise the application's contract through the real shared Runtime."""
from types import SimpleNamespace

import pytest
from tkn_genai_bridge import Profile, ProviderError, ResponseMetadata, Runtime, Usage
from tkn_genai_bridge.providers.base import ProviderResponse

from tkn_codex_chat_note import inference
from tkn_codex_chat_note.inference import InferenceExecutionError, invoke_structured

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}},
          "required": ["answer"], "additionalProperties": False}


def inference_config(provider, **overrides):
    return SimpleNamespace(**{
        **dict(provider=provider, codex_bin="codex", claude_bin="claude", copilot_bin="copilot",
               ollama_base_url="http://127.0.0.1:11434", model="test-model", reasoning_effort="high"),
        **overrides,
    })


@pytest.mark.parametrize("provider", [
    "codex", "claude-code", "github-copilot", "antigravity", "ollama", "azure-openai",
])
def test_all_providers_use_bridge_and_preserve_usage(tmp_path, monkeypatch, provider):
    calls, events = [], []

    class Backend:
        def generate(self, profile, request):
            calls.append((profile, request))
            assert events[0]["status"] == "started"
            return ProviderResponse(data={"answer": "ok"}, response_model="actual", usage=Usage(input_tokens=10))

    monkeypatch.setattr(inference, "Runtime", lambda profile, **kw: Runtime(profile, backend=Backend(), **kw))
    cfg = inference_config(provider, inference_options={"azure": {
        "endpoint": "https://example.openai.azure.com/openai/v1/"}})
    assert invoke_structured(cfg, "hello", SCHEMA, cwd=tmp_path, timeout=9, usage_observer=events.append)
    profile, request = calls[0]
    assert isinstance(profile, Profile) and profile.provider == provider and profile.timeout_seconds == 9
    assert request.output_schema == SCHEMA
    assert events[-1]["inputTokens"] == 10 and events[-1]["outputTokens"] is None
    assert events[-1]["bridgeVersion"] == "0.7.0"
    assert events[-1]["usageId"] == events[0]["usageId"]
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.parametrize("failure", ["invalid-schema", "provider-error"])
def test_failure_keeps_bridge_metadata(tmp_path, monkeypatch, failure):
    events = []

    class Backend:
        def generate(self, profile, request):
            if failure == "invalid-schema":
                return ProviderResponse(data={"answer": 42}, usage=Usage(input_tokens=10, output_tokens=2))
            error = ProviderError("failed", code="timeout", submission_unknown=True)
            error.metadata = ResponseMetadata(usage=Usage(input_tokens=10, output_tokens=2))
            raise error

    monkeypatch.setattr(inference, "Runtime", lambda profile, **kw: Runtime(profile, backend=Backend(), **kw))
    with pytest.raises(InferenceExecutionError):
        invoke_structured(inference_config("codex"), "hello", SCHEMA,
                          cwd=tmp_path, timeout=9, usage_observer=events.append)
    assert events[-1]["status"] == "failed" and events[-1]["inputTokens"] == 10
    assert events[-1]["outputTokens"] == 2
