"""Named profiles select transports explicitly and migrate without changing inference."""
from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from tkn_codex_chat_note.cli import build_parser
from tkn_codex_chat_note.config import (
    AppConfig,
    GenerationConfig,
    config_document,
    load_app_config,
    normalize_generation,
    resolve_app_config,
)
from tkn_codex_chat_note.session_notes import PipelineError, generator_fingerprint

ENDPOINT = "https://example.openai.azure.com/openai/v1/"
RATES = {"notes": {"input_jpy_per_million": 10, "output_jpy_per_million": 20, "pricing_date": "2026-09-14"}}


def generation():
    return {"active_profile": "azure-high", "profiles": {
        "azure-high": {"provider": "azure-openai", "endpoint": ENDPOINT, "model": "notes", "pricing": RATES},
        "azure-low": {"provider": "azure-openai", "endpoint": "https://second.openai.azure.com/openai/v1/",
                      "model": "other", "reasoning_effort": "low"},
        "local-gemma": {"provider": "ollama", "model": "local", "endpoint": "http://localhost:11500"},
        "codex-high": {"provider": "codex", "model": "remote", "executable": "custom-codex"},
    }}


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"schema_version": "8.0.0", "generation": generation()}), encoding="utf-8")
    return path


def test_profile_switch_routes_endpoint_model_and_overrides_only_selected(config_path):
    original = config_path.read_bytes()
    high = load_app_config(explicit_path=config_path, cwd=config_path.parent)
    result = resolve_app_config(explicit_path=config_path, cwd=config_path.parent,
                                overrides={"profile": "azure-low", "model": "replacement"})
    low = result.config
    assert set(low.generation.profiles) == set(generation()["profiles"])
    assert high.provider == low.provider == "azure-openai"
    assert low.model == "replacement" and low.reasoning_effort == "low"
    assert low.active_provider_config.endpoint.startswith("https://second.")
    assert low.active_provider_config.azure.pricing.get(low.model) is None
    assert low.generation.profiles["azure-high"].model == "notes"
    assert result.sources["generation.active_profile"] == "CLI option"
    assert result.sources["generation.profiles.azure-low.model"] == "CLI option"
    assert config_path.read_bytes() == original
    local = load_app_config(explicit_path=config_path, cwd=config_path.parent, overrides={"profile": "local-gemma"})
    assert local.provider == "ollama" and local.ollama_base_url == "http://localhost:11500"
    codex = load_app_config(explicit_path=config_path, cwd=config_path.parent, overrides={"profile": "codex-high"})
    assert codex.codex_executable == "custom-codex"
    assert codex.session_note_pipeline_config(allow_missing_watermark=True).codex_bin == "custom-codex"


def test_compatibility_provider_rejects_ambiguity_and_preserves_unique_match(config_path):
    with pytest.raises(PipelineError, match="multiple profiles.*--profile"):
        load_app_config(explicit_path=config_path, cwd=config_path.parent, overrides={"provider": "azure-openai"})
    cfg = load_app_config(explicit_path=config_path, cwd=config_path.parent, overrides={"provider": "codex"})
    assert cfg.generation.active_profile == "codex-high"
    with pytest.raises(PipelineError, match="unknown generation profile"):
        load_app_config(explicit_path=config_path, cwd=config_path.parent, overrides={"profile": "missing"})
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--profile", "azure-high", "--provider", "azure-openai", "config", "show"])


def test_provider_is_explicit_even_when_profile_name_matches_another_provider():
    cfg = AppConfig(generation={"active_profile": "codex", "profiles": {
        "codex": {"provider": "ollama", "model": "local"}}})
    assert cfg.provider == "ollama"
    assert cfg.session_note_pipeline_config(allow_missing_watermark=True).provider == "ollama"


@pytest.mark.parametrize("profile", [
    {"model": "notes", "endpoint": ENDPOINT},
    {"provider": "unknown", "model": "notes"},
    {"provider": "codex", "model": "notes", "endpoint": ENDPOINT},
    {"provider": "azure-openai", "model": "notes", "endpoint": ENDPOINT, "executable": "azure"},
    {"provider": "azure-openai", "model": "notes"},
    {"provider": "ollama", "model": "local", "endpoint": ""},
    {"provider": "ollama", "model": "local", "endpoint": "http://remote.example"},
    {"provider": "ollama", "model": "local", "pricing": RATES},
    {"provider": "azure-openai", "model": "notes", "azure": {"endpoint": ENDPOINT}},
])
def test_invalid_transports_fail_without_authentication(profile):
    with pytest.raises(ValidationError):
        GenerationConfig(active_profile="chosen", profiles={"chosen": profile})


def test_legacy_layers_migrate_without_modifying_cache_identity_or_files(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    legacy = {"active_provider": "azure-openai", "providers": {
        "azure-openai": {"model": "notes", "azure": {"endpoint": ENDPOINT, "pricing": RATES}}}}
    old = tmp_path / "old.yaml"
    old.write_text(yaml.safe_dump({"schema_version": "7.2.0", "generation": legacy}), encoding="utf-8")
    original = old.read_bytes()
    resolution = resolve_app_config(explicit_path=old, cwd=tmp_path)
    assert resolution.has_in_memory_migrations
    assert resolution.layers[-1]["migration"]["kind"] == "named-generation-profiles"
    canonical = normalize_generation(legacy)
    canonical["profiles"]["renamed"] = canonical["profiles"].pop("azure-openai")
    canonical["active_profile"] = "renamed"
    new = AppConfig(generation=canonical)
    old_pipeline = resolution.config.session_note_pipeline_config(allow_missing_watermark=True)
    new_pipeline = new.session_note_pipeline_config(allow_missing_watermark=True)
    assert old_pipeline.inference_options == new_pipeline.inference_options
    assert generator_fingerprint(old_pipeline) == generator_fingerprint(new_pipeline)
    assert old_pipeline.generation_profile != new_pipeline.generation_profile
    assert old.read_bytes() == original
    document = config_document(new)
    assert "providers" not in document["generation"]
    assert "azure" not in document["generation"]["profiles"]["renamed"]


def test_layers_merge_by_profile_and_provider_change_discards_previous_transport(config_path):
    project = config_path.parent / ".tkn/config.yaml"
    project.parent.mkdir()
    project.write_text(yaml.safe_dump({"schema_version": "8.0.0", "generation": generation()}), encoding="utf-8")
    override = {"schema_version": "8.0.0", "generation": {"profiles": {"azure-high": {"reasoning_effort": "medium"}}}}
    config_path.write_text(yaml.safe_dump(override), encoding="utf-8")
    cfg = load_app_config(explicit_path=config_path, cwd=config_path.parent)
    assert cfg.reasoning_effort == "medium" and cfg.model == "notes"
    assert cfg.active_provider_config.endpoint == ENDPOINT
    override["generation"]["profiles"]["azure-high"] = {"provider": "codex", "model": "remote"}
    config_path.write_text(yaml.safe_dump(override), encoding="utf-8")
    cfg = load_app_config(explicit_path=config_path, cwd=config_path.parent)
    assert cfg.provider == "codex" and cfg.codex_executable == "codex"
    assert cfg.active_provider_config.endpoint is None and not cfg.active_provider_config.pricing
    del override["generation"]["profiles"]["azure-high"]["model"]
    config_path.write_text(yaml.safe_dump(override), encoding="utf-8")
    with pytest.raises(PipelineError, match="model"):
        load_app_config(explicit_path=config_path, cwd=config_path.parent)


def test_conflicting_legacy_and_new_fields_are_rejected():
    mixed = deepcopy(generation())
    mixed["providers"] = {"codex": {"model": "remote"}}
    with pytest.raises(ValueError, match="both"):
        normalize_generation(mixed)



@pytest.mark.parametrize("old,new", [("active_provider", "profiles"), ("providers", "active_profile")])
def test_crossed_legacy_and_new_fields_are_rejected(old, new):
    with pytest.raises(ValueError, match="both"):
        normalize_generation({old: "codex" if old == "active_provider" else {},
                              new: "codex" if new == "active_profile" else {}})
