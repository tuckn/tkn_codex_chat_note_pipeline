from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from chat_fixtures import write_chat

from tkn_codex_chat_note.catalog import thread_key
from tkn_codex_chat_note.config import AppConfig
from tkn_codex_chat_note.pipeline import pipeline_status, run_pipeline
from tkn_codex_chat_note.provenance import validate_provenance
from tkn_codex_chat_note.runtime import PipelineError
from tkn_codex_chat_note.storage import pipeline_storage


def config_for(tmp_path: Path) -> AppConfig:
    return AppConfig(
        sources={
            "windows": {
                "source_root": tmp_path / "codex",
                "raw_root": tmp_path / "raw",
                "data_root": tmp_path / "data",
                "state_root": tmp_path / "state",
            }
        },
        cache_root=tmp_path / "cache",
    )


def app_state(
    config: AppConfig, *, projects: dict[str, Path], assignments: dict[str, str], projectless: list[str]
) -> None:
    config.codex_home.mkdir(parents=True, exist_ok=True)
    config.app_state_path.write_text(
        json.dumps(
            {
                "local-projects": {
                    key: {"id": key, "name": key, "rootPaths": [str(path)], "createdAt": "2026-01-01T00:00:00Z"}
                    for key, path in projects.items()
                },
                "thread-project-assignments": {
                    thread: {"projectKind": "local", "projectId": project} for thread, project in assignments.items()
                },
                "projectless-thread-ids": projectless,
            }
        ),
        encoding="utf-8",
    )


def execute(config: AppConfig, mode: str = "clone", **kwargs: Any) -> dict[str, Any]:
    return run_pipeline(config, mode=mode, **kwargs)


def test_clone_projectless_archived_and_multiple_projects(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    roots = {"a": tmp_path / "repo-a", "b": tmp_path / "repo-b"}
    app_state(config, projects=roots, assignments={"one": "a", "two": "b"}, projectless=["three"])
    for thread, folder in [("one", "sessions"), ("two", "sessions"), ("three", "archived_sessions")]:
        write_chat(config.codex_home / folder / f"{thread}.jsonl", thread_id=thread, cwd=tmp_path / "unknown")
    report = execute(config)
    assert report["complete"], report
    assert report["threadCounts"] == {"current": 3}
    assert len(list((config.source_data_root / "session-notes").rglob("*.md"))) == 0
    assert report["normalizedThreadCount"] == 3
    index = json.loads((config.data_root / "provenance" / "index.json").read_text())
    assert len({item["id"] for item in index["artifacts"]}) == len(index["artifacts"])
    for activity_path in (config.data_root / "provenance" / "activities").glob("*.json"):
        activity = json.loads(activity_path.read_text())
        for entity in activity["used"] + activity["generated"]:
            snapshot = config.data_root / entity["snapshotRef"].removeprefix("data:/")
            assert sha256(snapshot.read_bytes()).hexdigest() == entity["sha256"]


def test_repeat_clone_is_noop_and_late_old_history_is_ingested(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    write_chat(config.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path)
    first = execute(config)
    assert first["complete"], first
    ids = {item["threadId"]: item["threadKey"] for item in first["threads"]}
    second = execute(config)
    assert second["complete"], second
    write_chat(config.sessions_root / "old.jsonl", thread_id="old", cwd=tmp_path, started_at="2020-01-01T00:00:00Z")
    third = execute(config, "pull")
    assert third["complete"], third
    assert third["normalizedThreadCount"] == 2
    assert next(item for item in third["threads"] if item["threadId"] == "one")["threadKey"] == ids["one"]


def test_dry_run_has_no_writes_or_inference(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    write_chat(config.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    report = execute(config, dry_run=True)
    assert not report["complete"] and report["reportPath"] is None
    assert before == {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert not config.raw_root.exists() and not config.state_root.exists() and not config.data_root.exists()


def test_reassignment_keeps_identity_and_whole_thread(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    roots = {"a": tmp_path / "repo-a", "b": tmp_path / "repo-b"}
    app_state(config, projects=roots, assignments={"one": "a"}, projectless=[])
    write_chat(config.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path / "outside")
    first = execute(config)
    assert first["complete"], first
    note_id = first["threads"][0]["threadKey"]
    app_state(config, projects=roots, assignments={"one": "b"}, projectless=[])
    second = execute(config, "pull")
    assert second["threads"][0]["threadKey"] == note_id
    assert second["threads"][0]["membership"]["sourceProjectId"] == "b"


def test_divergent_logs_preserve_every_history(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    write_chat(config.sessions_root / "a.jsonl", thread_id="same", cwd=tmp_path, request="first")
    write_chat(config.sessions_root / "b.jsonl", thread_id="same", cwd=tmp_path, request="second")
    report = execute(config)
    assert report["complete"], report
    assert report["rawIngest"]["availableCaptureCount"] == 2
    entry = report["threads"][0]
    assert len(entry["historyBranches"]) == 2
    canonical = json.loads((config.data_root / entry["canonicalRef"].removeprefix("data:/")).read_text())
    assert len({event["localId"] for event in canonical["events"]}) == len(canonical["events"])
    assert {event["text"] for event in canonical["events"] if event["actor"] == "user"} == {"first", "second"}
    for event in canonical["events"]:
        assert event["rawRef"].split("#")[1].startswith("L")
        assert event["localId"].startswith(event["branchId"])
    assert validate_provenance(config.data_root)["ok"]
    again = execute(config, "pull")
    assert again["complete"]
    assert again["threads"][0]["threadKey"] == entry["threadKey"]
    assert not list(config.data_root.rglob("*.md"))


def test_archiving_identical_log_does_not_regenerate_and_raw_survives_removal(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    original = config.sessions_root / "one.jsonl"
    write_chat(original, thread_id="one", cwd=tmp_path)
    assert execute(config)["complete"]
    archived = config.codex_home / "archived_sessions" / "one.jsonl"
    archived.parent.mkdir()
    archived.write_bytes(original.read_bytes())
    original.unlink()
    report = execute(config, "pull")
    assert report["complete"], report
    archived.unlink()
    assert execute(config, "pull")["complete"]


def test_missing_app_state_and_ambiguous_membership_do_not_exclude_evidence(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    app_state(config, projects={"a": tmp_path, "b": tmp_path}, assignments={}, projectless=[])
    write_chat(config.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path)
    first = execute(config)
    assert first["complete"], first
    assert first["threads"][0]["membership"]["status"] == "ambiguous"
    config.app_state_path.unlink()
    second = execute(config, "pull")
    assert second["threads"][0]["status"] == "current"


def test_os_lock_is_released_and_pull_requires_clone(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    with pytest.raises(PipelineError, match="clone"):
        execute(config, "pull")
    with pipeline_storage(config, initialize=True, dry_run=False):
        with pytest.raises(PipelineError, match="lock"):
            with pipeline_storage(config, initialize=False, dry_run=False):
                pass
    with pipeline_storage(config, initialize=False, dry_run=False):
        pass
    assert pipeline_status(config)["initialized"]
    assert thread_key("one") != thread_key("two")


def append_turn(path: Path) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-08-04T10:00:00Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "Retain earlier evidence too."},
                }
            )
            + "\n"
        )


def test_raw_capture_does_not_require_scope_or_parser_success(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    path = config.sessions_root / "unknown.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("unrecognized format\n", encoding="utf-8")
    captured = execute(config, "raw")
    assert captured["ok"] and captured["complete"]
    assert not (config.source_data_root / "source-aligned").exists()
    assert captured["rawIngest"]["availableCaptureCount"] == 1
    normalized = execute(config, "pull")
    assert not normalized["ok"]


@pytest.mark.parametrize("damage", ["snapshot", "relation", "artifact", "structure"])
def test_provenance_validation_detects_damage(tmp_path: Path, damage: str) -> None:
    config = config_for(tmp_path)
    write_chat(config.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path)
    assert execute(config)["complete"]
    assert validate_provenance(config.data_root)["ok"]
    index_path = config.data_root / "provenance" / "index.json"
    index = json.loads(index_path.read_text())
    if damage in {"snapshot", "artifact"}:
        ref = index["artifacts"][0]["snapshotRef" if damage == "snapshot" else "ref"]
        (config.data_root / ref.removeprefix("data:/")).write_bytes(b"damaged")
    else:
        path = next((config.data_root / "provenance" / "activities").glob("*.json"))
        activity = json.loads(path.read_text())
        if damage == "relation":
            activity["relations"][0]["to"]["id"] = "missing-entity"
        else:
            activity["used"] = None
        path.write_text(json.dumps(activity), encoding="utf-8")
    with pytest.raises(PipelineError):
        validate_provenance(config.data_root)


def test_corrupted_raw_is_repaired_from_available_original(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    write_chat(config.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path)
    first = execute(config)
    capture = config.raw_root / first["threads"][0]["sourceCaptureRef"].removeprefix(f"raw:/codex/{config.source_id}/")
    capture.write_bytes(b"corrupted")
    second = execute(config, "pull")
    assert second["complete"] and second["ok"]
    assert capture.read_bytes() == (config.sessions_root / "one.jsonl").read_bytes()


def test_changing_optional_app_metadata_does_not_stop_ingestion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tkn_codex_chat_note.catalog as catalog
    from tkn_codex_chat_note.raw_capture import RawCaptureError

    config = config_for(tmp_path)
    app_state(config, projects={}, assignments={}, projectless=[])
    write_chat(config.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path)

    def changing_metadata(path: Path) -> bytes:
        raise RawCaptureError("source changed during raw capture")

    monkeypatch.setattr(catalog, "_stable_source_bytes", changing_metadata)
    report = execute(config)
    assert report["complete"], report
    assert report["warnings"]
    assert report["threads"][0]["membership"]["status"] == "state-unavailable"


def test_non_utf8_source_is_captured_before_normalization_fails(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    path = config.sessions_root / "unreadable.jsonl"
    path.parent.mkdir(parents=True)
    content = b"not utf8: \xff\xfe\x80\n"
    path.write_bytes(content)
    captured = execute(config, "raw")
    assert captured["ok"] and captured["rawIngest"]["availableCaptureCount"] == 1
    assert next(config.raw_root.rglob("*.jsonl")).exists()
    captures = list((config.source_raw_root / "sessions").rglob("*.jsonl"))
    assert len(captures) == 1 and captures[0].read_bytes() == content
    path.unlink()
    report = execute(config, "pull")
    assert not report["complete"] and report["failed"]
    assert report["rawIngest"]["availableCaptureCount"] == 1


def test_clone_completion_and_catalog_describe_normalized_evidence(tmp_path: Path) -> None:
    from hashlib import sha256

    config = config_for(tmp_path)
    write_chat(config.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path)
    result = execute(config)
    assert result["complete"] and "scopeResults" not in result
    assert not (config.data_root / "catalog/scopes.json").exists()
    assert not list(config.data_root.rglob("DR-*.md"))
    assert not list(config.data_root.rglob("working-context.md"))
    entry = json.loads((config.data_root / "catalog/threads.json").read_text())["threads"][0]
    canonical = config.data_root / entry["canonicalRef"].removeprefix("data:/")
    assert entry["canonicalSha256"] == sha256(canonical.read_bytes()).hexdigest()
    assert "noteRef" not in entry


def test_explicit_legacy_store_requires_migration_without_modifying_files(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    for kind, root in (
        ("data", config.data_root),
        ("state", config.state_root),
        ("cache", config.cache_root),
        ("raw", config.raw_root),
    ):
        root.mkdir()
        (root / ".tkn-codex-context-root.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "applicationId": "tkn-codex-context-pipeline",
                    "rootKind": kind,
                }
            ),
            encoding="utf-8",
        )
    (config.state_root / "pipeline.json").write_text(
        json.dumps(
            {
                "storageVersion": 2,
                "sourceId": config.source_id,
            }
        ),
        encoding="utf-8",
    )
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    with pytest.raises(PipelineError, match="storage migrate"):
        with pipeline_storage(config, initialize=False, dry_run=True):
            pass
    assert before == {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}


@pytest.mark.parametrize("mode", ["clone", "pull", "raw"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_unavailable_chat_source_stops_before_any_writes(tmp_path: Path, mode: str, dry_run: bool) -> None:
    config = config_for(tmp_path)
    config.sources["windows"].enabled = False
    message = "no enabled Codex source"
    before = {str(path): path.read_bytes() if path.is_file() else None for path in tmp_path.rglob("*")}
    with pytest.raises(PipelineError, match=message):
        run_pipeline(config, mode=mode, dry_run=dry_run)
    after = {str(path): path.read_bytes() if path.is_file() else None for path in tmp_path.rglob("*")}
    assert after == before


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_unicode_separators_are_not_jsonl_record_boundaries(tmp_path: Path, separator: str) -> None:
    config = config_for(tmp_path)
    original = config.sessions_root / "one.jsonl"
    write_chat(original, thread_id="one", cwd=tmp_path, request="before" + separator + "after")
    before = original.read_bytes()
    report = execute(config)
    assert report["complete"], report
    assert report["threads"][0]["diagnostics"]["invalidLines"] == []
    assert (config.raw_root / "sessions/one.jsonl").read_bytes() == before


def test_legacy_logs_preserve_unknown_event_times(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    original = config.sessions_root / "legacy.jsonl"
    original.parent.mkdir(parents=True)
    original.write_bytes((Path(__file__).parent / "fixtures/chat-legacy-thread.jsonl").read_bytes())
    report = execute(config)
    assert report["complete"], report
    entry = report["threads"][0]
    canonical = json.loads((config.data_root / entry["canonicalRef"].removeprefix("data:/")).read_text())
    assert any(not event["timestamp"] for event in canonical["events"])
    assert entry["lastEventAt"] == "2025-08-24T10:45:58.916Z"
    assert not entry["diagnostics"]["unknownRecordTypes"]


def test_invalid_json_is_still_rejected_inside_a_branch(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    for name in ["a", "b"]:
        write_chat(config.sessions_root / f"{name}.jsonl", thread_id="same", cwd=tmp_path, request=name)
    with (config.sessions_root / "b.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"broken":\n')
    report = execute(config)
    assert report["threads"][0]["reason"] == "invalid-jsonl"
    assert report["rawIngest"]["availableCaptureCount"] == 2


def test_branch_append_updates_canonical_and_archived_alias_preserves_identity(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    for name in ["a", "b"]:
        write_chat(config.sessions_root / f"{name}.jsonl", thread_id="same", cwd=tmp_path, request=name)
    first = execute(config)
    archived = config.codex_home / "archived_sessions/b.jsonl"
    archived.parent.mkdir()
    archived.write_bytes((config.sessions_root / "b.jsonl").read_bytes())
    alias = execute(config, "pull")
    assert alias["complete"]
    with (config.sessions_root / "b.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"timestamp": "2026-07-02T00:00:00Z", "type": "event_msg",
                                 "payload": {"type": "user_message", "message": "additional request"}}) + "\n")
    second = execute(config, "pull")
    assert second["complete"], second
    assert second["threads"][0]["threadKey"] == first["threads"][0]["threadKey"]
    assert second["threads"][0]["sourceSetSha256"] != first["threads"][0]["sourceSetSha256"]
    assert len(second["threads"][0]["historyBranches"]) == 2
    assert validate_provenance(config.data_root)["ok"]


@pytest.mark.parametrize("trigger_turn", [True, False])
def test_agent_communication_metadata_is_control_data_retained_in_raw(tmp_path: Path, trigger_turn: bool) -> None:
    from tkn_codex_chat_note.chat_logs import read_thread_source

    config = config_for(tmp_path)
    original = config.sessions_root / "one.jsonl"
    write_chat(original, thread_id="one", cwd=tmp_path)
    events = read_thread_source(original).events
    metadata = {
        "timestamp": "2025-01-01T00:00:00Z",
        "type": "inter_agent_communication_metadata",
        "payload": {"trigger_turn": trigger_turn},
    }
    with original.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(metadata) + "\n")
    before = original.read_bytes()
    assert read_thread_source(original).events == events
    report = execute(config)
    assert report["complete"], report
    assert not report["threads"][0]["diagnostics"]["unknownRecordTypes"]
    assert (config.raw_root / "sessions/one.jsonl").read_bytes() == before
    saved = json.loads(Path(report["reportPath"]).read_text(encoding="utf-8"))
    assert saved["threads"] == report["threads"]
