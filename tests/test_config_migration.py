"""`config migrate` converts schema 9 and moves the previous location without touching old files."""
import json
from pathlib import Path

import pytest
import yaml

from tkn_genai_chat_note.cli import main
from tkn_genai_chat_note.config import global_config_path, legacy_global_config_path, load_app_config
from tkn_genai_chat_note.config_migration import migrate_config
from tkn_genai_chat_note.runtime import PipelineError

ENDPOINT = "https://example.openai.azure.com/openai/v1"


def bridge_config(tmp_path):
    path = tmp_path / "bridge.yaml"
    path.write_text(yaml.safe_dump({"schema_version": "1.1.0", "default_profile": "codex-high", "profiles": {
        "codex-high": {"provider": "codex", "model": "remote", "reasoning_effort": "high",
                       "timeout_seconds": 1800.0},
        "azure-high": {"provider": "azure-openai", "model": "deployment", "reasoning_effort": "high",
                       "azure": {"endpoint": ENDPOINT, "auth": "interactive_browser"},
                       "pricing": {"deployment": {"currency": "JPY", "pricing_date": "2026-01-01",
                                                  "input_per_million": 30, "output_per_million": 180}}},
    }}), encoding="utf-8")
    return path


def schema9(path, **sources):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({
        "schema_version": "9.0.0",
        "sources": sources or {"pc": {"source_root": "C:/path/to/codex", "raw_root": "C:/path/to/raw",
                                      "data_root": "C:/path/to/data", "state_root": "C:/path/to/state"}},
        "generation": {"active_profile": "codex-high", "profiles": {
            "codex-high": {"bridge_profile": "codex-high"},
            "azure-high": {"bridge_profile": "azure-high", "limits": {"max_calls": 3, "max_cost_jpy": 10}},
            "inline-local": {"provider": "ollama", "model": "local", "reasoning_effort": "low"},
        }},
        "usage_report": {"price_scenarios": {"reference": {"bridge_profile": "azure-high", "model": "deployment"}}},
    }), encoding="utf-8")
    return path


def test_dry_run_converts_bridge_references_without_writing(tmp_path):
    old = schema9(tmp_path / "old/config.yaml")
    before = old.read_bytes()
    target = tmp_path / "new/config.yaml"
    result = migrate_config(from_config=old, target=target, bridge_config=bridge_config(tmp_path), dry_run=True)
    assert result["status"] == "planned" and not target.exists() and old.read_bytes() == before
    document = yaml.safe_load(result["document"])
    assert document["schema_version"] == "10.0.0" and "profiles" not in document["generation"]
    assert document["generation"] == {"genai_profile": "codex-high"}
    assert document["genai_profiles"]["codex-high"] == {
        "provider": "codex", "model": "remote", "reasoning_effort": "high", "timeout_seconds": 1800.0}
    azure = document["genai_profiles"]["azure-high"]
    assert azure["limits"] == {"max_calls": 3, "max_cost_jpy": 10}
    assert azure["pricing"]["deployment"]["input_per_million"] == 30
    assert document["genai_profiles"]["inline-local"]["ollama"]["think"] is False
    assert document["usage_report"]["price_scenarios"]["reference"] == {
        "genai_profile": "azure-high", "model": "deployment"}


def test_written_config_loads_and_keeps_the_generation_identity(tmp_path):
    old = schema9(tmp_path / "old/config.yaml")
    target = tmp_path / "new/config.yaml"
    result = migrate_config(from_config=old, target=target, bridge_config=bridge_config(tmp_path))
    assert result["status"] == "created" and result["sourceModified"] is False
    cfg = load_app_config(explicit_path=target, cwd=tmp_path)
    assert cfg.model == "remote" and cfg.reasoning_effort == "high"
    assert cfg.genai_profiles["codex-high"].bridge.timeout_seconds == 1800.0
    assert cfg.price_scenario_pricing()["reference"].input_per_million == 30
    assert migrate_config(from_config=old, target=target,
                          bridge_config=bridge_config(tmp_path))["status"] == "unchanged"
    target.write_text(target.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
    with pytest.raises(PipelineError, match="already exists"):
        migrate_config(from_config=old, target=target, bridge_config=bridge_config(tmp_path))
    replaced = migrate_config(from_config=old, target=target, bridge_config=bridge_config(tmp_path), force=True)
    assert replaced["status"] == "replaced" and Path(replaced["backupPath"]).read_text(encoding="utf-8").endswith(
        "# edited\n")


def test_missing_bridge_config_is_reported_for_references(tmp_path):
    old = schema9(tmp_path / "old/config.yaml")
    with pytest.raises(PipelineError, match="--bridge-config"):
        migrate_config(from_config=old, target=tmp_path / "new.yaml", bridge_config=tmp_path / "missing.yaml")


def test_previous_location_is_detected_and_moved_by_default(tmp_path, capsys):
    legacy = schema9(legacy_global_config_path())
    with pytest.raises(PipelineError, match="previous application name"):
        load_app_config(cwd=tmp_path)
    bridge = bridge_config(tmp_path)
    assert main(["config", "migrate", "--bridge-config", str(bridge), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["sourcePath"] == str(legacy) and result["configPath"] == str(global_config_path())
    assert legacy.is_file() and global_config_path().is_file()
    assert load_app_config(cwd=tmp_path).model == "remote"


def test_omitted_roots_are_pinned_to_the_previous_default_location(tmp_path):
    legacy_raw = Path.home() / ".tkn/codex_chat_note_pipeline/raw/codex/pc"
    legacy_raw.mkdir(parents=True)
    old = schema9(tmp_path / "old.yaml", pc={"source_root": str(tmp_path / "codex")})
    result = migrate_config(from_config=old, target=tmp_path / "new.yaml", bridge_config=bridge_config(tmp_path),
                            dry_run=True)
    source = yaml.safe_load(result["document"])["sources"]["pc"]
    assert source["raw_root"] == str(legacy_raw)
    assert "data_root" not in source  # Nothing exists there to keep.


def test_stranded_previous_default_store_is_never_replaced_by_an_empty_one(tmp_path):
    from tkn_genai_chat_note.pipeline import run_pipeline

    legacy_data = Path.home() / ".tkn/codex_chat_note_pipeline/data/codex/pc"
    legacy_data.mkdir(parents=True)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"schema_version": "10.0.0", "sources": {
        "pc": {"source_root": str(tmp_path / "codex")}}}), encoding="utf-8")
    (tmp_path / "codex/sessions").mkdir(parents=True)
    cfg = load_app_config(explicit_path=path, cwd=tmp_path)
    with pytest.raises(PipelineError, match="previous default location"):
        run_pipeline(cfg, mode="clone", dry_run=True)


def test_schema10_file_is_copied_unchanged(tmp_path):
    source = tmp_path / "current.yaml"
    source.write_text('schema_version: "10.0.0"\n# keep this comment\n', encoding="utf-8")
    target = tmp_path / "moved/config.yaml"
    assert migrate_config(from_config=source, target=target)["status"] == "created"
    assert target.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")


def test_older_schemas_need_schema9_first(tmp_path):
    source = tmp_path / "old.yaml"
    source.write_text('schema_version: "8.0.0"\n', encoding="utf-8")
    with pytest.raises(PipelineError, match="schema 9 only"):
        migrate_config(from_config=source, target=tmp_path / "new.yaml")


def write_old(path: Path, document: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"schema_version": "9.0.0", **document}), encoding="utf-8")
    return path


def converted(tmp_path: Path, document: dict, bridge: Path | None = None) -> dict:
    old = write_old(tmp_path / "old/config.yaml", document)
    result = migrate_config(from_config=old, target=tmp_path / "old/new.yaml",
                            bridge_config=bridge or tmp_path / "absent.yaml", dry_run=True)
    return yaml.safe_load(result["document"])


def test_schema9_defaults_and_built_in_bridge_profiles_need_no_bridge_file(tmp_path):
    document = converted(tmp_path, {"sources": {"pc": {"source_root": str(tmp_path)}}})
    assert document["generation"] == {"genai_profile": "codex"}
    assert document["genai_profiles"]["codex"]["provider"] == "codex"
    inline = converted(tmp_path, {"generation": {"profiles": {"codex": {"provider": "codex", "model": "m"}}}})
    assert inline["generation"] == {"genai_profile": "codex"}  # Schema 9's default active profile.
    assert inline["genai_profiles"]["codex"]["timeout_seconds"] == 1800.0  # Schema 9's default timeout.


def test_legacy_provider_keys_are_normalized_before_translation(tmp_path):
    document = converted(tmp_path, {"generation": {"active_provider": "azure-openai", "providers": {
        "azure-openai": {"azure": {"endpoint": ENDPOINT, "deployment": "dep", "tenant_id":
                                   "00000000-0000-0000-0000-000000000001", "input_jpy_per_million": 1,
                                   "output_jpy_per_million": 2, "pricing_date": "2026-01-01"}},
        "ollama": {"model": "local", "base_url": "http://localhost:11500"},
    }}})
    azure = document["genai_profiles"]["azure-openai"]
    assert azure["model"] == "dep" and azure["azure"]["tenant_id"].endswith("1")
    assert azure["pricing"]["dep"]["input_per_million"] == 1
    assert document["genai_profiles"]["ollama"]["ollama"]["base_url"] == "http://localhost:11500"
    assert document["generation"] == {"genai_profile": "azure-openai"}


def test_price_scenarios_keep_shared_rates_and_unique_names(tmp_path):
    bridge = bridge_config(tmp_path)
    document = converted(tmp_path, {
        "generation": {"active_profile": "azure-high", "profiles": {
            "azure-high": {"bridge_profile": "azure-high", "pricing": {"deployment": {
                "input_jpy_per_million": 99, "output_jpy_per_million": 99, "pricing_date": "2026-02-01"}}},
            "azure-high-pricing": {"bridge_profile": "codex-high"},
        }},
        "usage_report": {"price_scenarios": {"ref": {"bridge_profile": "azure-high", "model": "deployment"}}},
    }, bridge)
    assert document["usage_report"]["price_scenarios"]["ref"]["genai_profile"] == "azure-high-pricing-2"
    assert document["genai_profiles"]["azure-high-pricing-2"]["pricing"]["deployment"]["input_per_million"] == 30
    assert document["genai_profiles"]["azure-high-pricing"]["provider"] == "codex"


def test_implicit_default_source_roots_are_pinned(tmp_path):
    from tkn_genai_chat_note.runtime import DEFAULT_SOURCE_ID

    legacy = Path.home() / ".tkn/codex_chat_note_pipeline/state/codex" / DEFAULT_SOURCE_ID
    legacy.mkdir(parents=True)
    document = converted(tmp_path, {"runtime_minutes": 10})
    assert document["sources"] == {DEFAULT_SOURCE_ID: {"state_root": str(legacy)}}


def test_relative_paths_keep_their_target_when_the_file_moves(tmp_path):
    old = write_old(tmp_path / "old/config.yaml", {"sources": {"pc": {"source_root": "codex", "data_root": "data"}},
                                                   "cache_root": "~/.cache/x"})
    result = migrate_config(from_config=old, target=tmp_path / "new/config.yaml",
                            bridge_config=tmp_path / "absent.yaml", dry_run=True)
    source = yaml.safe_load(result["document"])["sources"]["pc"]
    assert source == {"source_root": str(tmp_path / "old/codex"), "data_root": str(tmp_path / "old/data")}
    assert yaml.safe_load(result["document"])["cache_root"] == "~/.cache/x"
    current = tmp_path / "current.yaml"
    current.write_text('schema_version: "10.0.0"\nprocesses:\n  state_root: work/state\n', encoding="utf-8")
    moved = migrate_config(from_config=current, target=tmp_path / "moved/config.yaml", dry_run=True)
    assert yaml.safe_load(moved["document"])["processes"]["state_root"] == str(tmp_path / "work/state")


def test_provider_keyed_files_keep_the_built_in_codex_profile(tmp_path):
    document = converted(tmp_path, {"generation": {"providers": {"ollama": {"model": "local"}}}})
    assert document["generation"] == {"genai_profile": "codex"}
    assert document["genai_profiles"]["codex"]["provider"] == "codex"
    assert document["genai_profiles"]["ollama"]["model"] == "local"

