"""Shared configuration changes must reach generation without changing application ownership."""
import json
from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from tkn_genai_bridge import Runtime

from tkn_codex_chat_note.api_inference import ApiClient
from tkn_codex_chat_note.bridge_settings import BridgeProfileConfig
from tkn_codex_chat_note.cli import main
from tkn_codex_chat_note.config import config_document, load_app_config
from tkn_codex_chat_note.pipeline import run_pipeline
from tkn_codex_chat_note.session_notes import PipelineError, generator_fingerprint


def shared(profiles):
    path = Path.home() / ".tkn/genai_bridge/config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"schema_version": "1.0.0", "profiles": profiles}), encoding="utf-8")
    return path


def application(tmp_path, **policy):
    path = tmp_path / "app.yaml"
    path.write_text(yaml.safe_dump({
        "schema_version": "8.2.0",
        "generation": {"active_profile": "notes", "profiles": {
            "notes": {"bridge_profile": "shared-notes", **policy}}},
    }), encoding="utf-8")
    return path


def fingerprint(cfg):
    return generator_fingerprint(cfg.session_note_pipeline_config(allow_missing_watermark=True))


def test_shared_changes_and_model_override_reach_generation(tmp_path):
    path = shared({"shared-notes": {"provider": "codex", "model": "one", "reasoning_effort": "low"}})
    app = application(tmp_path)
    before = path.read_bytes()
    first = load_app_config(explicit_path=app, cwd=tmp_path)
    assert first.model == "one" and first.reasoning_effort == "low"
    override = load_app_config(explicit_path=app, cwd=tmp_path,
                               overrides={"model": "two", "reasoning_effort": "high"})
    assert override.model == "two" and override.reasoning_effort == "high"
    assert first.model == "one" and path.read_bytes() == before
    assert fingerprint(first) != fingerprint(override)
    shared({"shared-notes": {"provider": "codex", "model": "three", "reasoning_effort": "low"}})
    assert fingerprint(load_app_config(explicit_path=app, cwd=tmp_path)) != fingerprint(first)
    assert config_document(first)["generation"]["profiles"]["notes"]["bridge_profile"] == "shared-notes"
    assert "provider" not in config_document(first)["generation"]["profiles"]["notes"]


def test_shared_rename_keeps_identity_and_runtime_keeps_local_boundary(tmp_path):
    settings = {"provider": "ollama", "model": "local", "local_only": True,
                "ollama": {"think": False, "context_tokens": 32768}, "max_output_tokens": 2048}
    shared({"shared-notes": settings, "same": deepcopy(settings)})
    cfg = load_app_config(explicit_path=application(tmp_path), cwd=tmp_path)
    original = fingerprint(cfg)
    cfg.generation.profiles["notes"] = BridgeProfileConfig(bridge_profile="same")
    assert fingerprint(cfg) == original
    client = ApiClient(cfg.session_note_pipeline_config(allow_missing_watermark=True))
    assert client.profile.local_only and client.profile.ollama.think is False
    assert client.profile.max_output_tokens == 2048
    assert client.limits.input_tokens + client.limits.output_tokens <= 32768
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
    envelope = client.body("question", schema)
    assert "answer" in envelope["messages"][0]["content"]
    assert envelope["output_schema"] == schema  # Both schema copies consume input.
    client.close()
    assert client.runtime._closed


@pytest.mark.parametrize("profile", [
    {"bridge_profile": "missing"},
    {"bridge_profile": "shared-notes", "provider": "codex", "model": "hidden"},
    {"bridge_profile": "shared-notes", "overrides": {"unknown": True}},
])
def test_invalid_reference_or_mixed_transport_fails_offline(tmp_path, profile):
    shared({"shared-notes": {"provider": "codex", "model": "one"}})
    path = tmp_path / "app.yaml"
    path.write_text(yaml.safe_dump({"schema_version": "8.2.0", "generation": {
        "active_profile": "notes", "profiles": {"notes": profile}}}), encoding="utf-8")
    with pytest.raises((PipelineError, ValueError)):
        load_app_config(explicit_path=path, cwd=tmp_path)


def test_shared_dry_run_reads_without_generating_or_writing(tmp_path, monkeypatch):
    from test_multi_sources import multi_config, source_chats

    shared({"shared-notes": {"provider": "azure-openai", "model": "deployment",
                            "azure": {"endpoint": "https://example.openai.azure.com/openai/v1",
                                      "auth": "interactive_browser"}}})
    cfg = multi_config(tmp_path)
    cfg.generation.profiles = {"codex": BridgeProfileConfig(bridge_profile="shared-notes")}
    source_chats(cfg)
    monkeypatch.setattr(Runtime, "generate", lambda *a, **k: pytest.fail("generation"))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    report = run_pipeline(cfg, mode="clone", dry_run=True)
    assert report["ok"] and report["threadCounts"] == {"planned": 2}
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_config_show_exposes_resolved_shared_identity(tmp_path, capsys):
    import json

    shared({"shared-notes": {"provider": "codex", "model": "one"}})
    path = application(tmp_path)
    assert main(["--config", str(path), "config", "show"]) == 0
    details = json.loads(capsys.readouterr().out)["generationResolved"]
    assert details["model"] == "one"
    assert details["inferenceOptions"]["bridge_profile"] == "shared-notes"


def test_application_cwd_is_not_loaded_as_a_bridge_config(tmp_path):
    shared({"shared-notes": {"provider": "codex", "model": "shared"}})
    app = application(tmp_path)
    (tmp_path / ".tkn").mkdir()
    (tmp_path / ".tkn/config.yaml").write_bytes(app.read_bytes())
    assert load_app_config(cwd=tmp_path).model == "shared"


def test_invalid_override_in_lower_layer_cannot_be_hidden(tmp_path):
    shared({"shared-notes": {"provider": "codex", "model": "one"}})
    app = application(tmp_path, overrides={"unknown": True})
    (tmp_path / ".tkn").mkdir()
    (tmp_path / ".tkn/config.yaml").write_bytes(app.read_bytes())
    application(tmp_path, overrides={"unknown": False})
    with pytest.raises(PipelineError, match="unknown configuration key"):
        load_app_config(explicit_path=app, cwd=tmp_path)


def test_explicit_legacy_profile_does_not_read_unused_shared_config(tmp_path):
    shared_path = shared({"codex-default": {"provider": "codex"}})
    shared_path.write_text("invalid yaml", encoding="utf-8")
    path = tmp_path / "legacy.yaml"
    path.write_text(yaml.safe_dump({"schema_version": "8.1.0", "generation": {
        "active_profile": "legacy", "profiles": {"legacy": {"provider": "codex", "model": "one"}}}}),
        encoding="utf-8")
    assert load_app_config(explicit_path=path, cwd=tmp_path).model == "one"


@pytest.mark.parametrize("shared_profile", [True, False])
def test_antigravity_configuration_selector_and_runtime(tmp_path, monkeypatch, capsys, shared_profile):
    from tkn_genai_bridge import Usage
    from tkn_genai_bridge.providers.base import ProviderResponse

    from tkn_codex_chat_note import inference

    profile = {"provider": "antigravity", "model": "test-model", "reasoning_effort": "medium",
               "cli": {"executable": "custom-agy"}}
    shared({"shared-notes": profile})
    app = application(tmp_path)
    if not shared_profile:
        value = yaml.safe_load(app.read_text(encoding="utf-8"))
        value["generation"]["profiles"]["notes"] = {
            "provider": "antigravity", "model": "test-model", "reasoning_effort": "medium",
            "executable": "custom-agy"}
        app.write_text(yaml.safe_dump(value), encoding="utf-8")
    before = app.read_bytes()
    assert main(["--config", str(app), "--provider", "antigravity", "config", "show"]) == 0
    assert json.loads(capsys.readouterr().out)["generationResolved"]["provider"] == "antigravity"
    cfg = load_app_config(explicit_path=app, cwd=tmp_path)
    observed = []

    class Backend:
        def generate(self, actual, request):
            assert actual.provider == "antigravity" and actual.cli.executable == "custom-agy"
            assert actual.reasoning_effort == "medium"
            return ProviderResponse({"ok": True}, usage=Usage(input_tokens=10, output_tokens=2))

    monkeypatch.setattr(inference, "Runtime", lambda profile, **kw: Runtime(profile, backend=Backend(), **kw))
    assert inference.invoke_structured(cfg.session_note_pipeline_config(allow_missing_watermark=True),
                                       "test", {"type": "object"}, cwd=tmp_path, timeout=10,
                                       usage_observer=observed.append) == {"ok": True}
    assert observed[-1]["model"] is None  # Requested identity is not a reported model.
    assert observed[-1]["inputTokens"] == 10 and inference.provider_name("antigravity") == "Google Antigravity"
    assert inference.is_supported_generator("Google Antigravity")
    assert app.read_bytes() == before

