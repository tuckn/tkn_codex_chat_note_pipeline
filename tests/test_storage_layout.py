from __future__ import annotations

from pathlib import Path

import pytest
from chat_fixtures import write_chat
from test_pipeline_workflow import config_for

from tkn_genai_chat_note.config import AppConfig
from tkn_genai_chat_note.pipeline import run_pipeline
from tkn_genai_chat_note.provenance import validate_provenance
from tkn_genai_chat_note.references import resolve_store_ref
from tkn_genai_chat_note.runtime import PipelineError


def snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None for path in root.rglob("*")
    }


def test_two_source_stores_are_independent_and_cannot_reuse_a_root(tmp_path: Path) -> None:
    first = config_for(tmp_path / "first")
    first.sources = {"pc-one": first.source_settings}
    write_chat(first.sessions_root / "one.jsonl", thread_id="same-thread", cwd=Path("C:/example/project"))
    report = run_pipeline(first, mode="clone")
    assert report["complete"]
    before = snapshot(first.data_root)
    second = config_for(tmp_path / "second")
    second.sources = {"pc-two": second.source_settings}
    write_chat(second.sessions_root / "one.jsonl", thread_id="same-thread", cwd=Path("C:/example/project"))
    assert run_pipeline(second, mode="clone")["complete"]
    assert snapshot(first.data_root) == before
    assert validate_provenance(first.data_root)["ok"] and validate_provenance(second.data_root)["ok"]
    second.source_settings.data_root = first.data_root
    with pytest.raises(PipelineError, match="different source identity"):
        run_pipeline(second, mode="clone", dry_run=True)


def test_explicit_paths_are_final_and_defaults_are_source_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    config = AppConfig()
    assert config.data_root == tmp_path / ".tkn/genai_chat_note/data/codex/windows"
    config.source_settings.raw_root = tmp_path / "arbitrary/location"
    assert config.raw_root == tmp_path / "arbitrary/location"
    assert config.source_cache_root == config.cache_root / "codex/windows"


def test_store_references_resolve_inside_their_own_store(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    write_chat(config.sessions_root / "one.jsonl", thread_id="one", cwd=Path("C:/example"))
    assert run_pipeline(config, mode="clone")["complete"]
    descriptor = {"rawRefPrefix": "raw:/codex/windows/", "rawRefAliases": [], "dataRefAliases": {}}
    raw = next((config.raw_root / "sessions").rglob("*.jsonl"))
    ref = "raw:/codex/windows/" + raw.relative_to(config.raw_root).as_posix()
    assert resolve_store_ref(config.raw_root, ref, descriptor, kind="raw") == raw.resolve()
    canonical = next((config.data_root / "source-aligned").rglob("*.json"))
    data_ref = "data:/" + canonical.relative_to(config.data_root).as_posix()
    assert resolve_store_ref(config.data_root, data_ref, descriptor) == canonical.resolve()
    with pytest.raises(PipelineError, match="another source"):
        resolve_store_ref(config.raw_root, ref.replace("windows", "other"), descriptor, kind="raw")
    with pytest.raises(PipelineError, match="invalid"):
        resolve_store_ref(config.data_root, "data:/source-aligned/../escape", descriptor)

    # Existing storage-5 descriptors may carry aliases; they translate once, longest prefix first.
    aliased = {**descriptor, "rawRefAliases": ["raw:/windows/"],
               "dataRefAliases": {"old/": "elsewhere/", "old/aligned/": "source-aligned/"}}
    old_raw = "raw:/windows/" + raw.relative_to(config.raw_root).as_posix()
    assert resolve_store_ref(config.raw_root, old_raw, aliased, kind="raw") == raw.resolve()
    old_data = "data:/old/aligned/" + canonical.relative_to(config.data_root / "source-aligned").as_posix()
    assert resolve_store_ref(config.data_root, old_data, aliased) == canonical.resolve()
    with pytest.raises(PipelineError, match="invalid"):
        resolve_store_ref(config.data_root, "data:/old/aligned/../escape", aliased)
    with pytest.raises(PipelineError, match="invalid Raw reference prefix"):
        resolve_store_ref(config.raw_root, old_raw, {**descriptor, "rawRefAliases": ["raw:/windows"]}, kind="raw")


def test_storage_roots_cannot_overlap_a_disabled_source(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    config.sources["disabled"] = config.source_settings.model_copy(
        update={"enabled": False, "data_root": config.raw_root}
    )
    with pytest.raises(PipelineError, match="overlap"):
        run_pipeline(config, mode="clone", dry_run=True)
