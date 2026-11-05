"""Read-only use of a tkn-genai-chat-archiver Raw archive."""

from __future__ import annotations

import json
import shutil
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from archive_fixtures import ArchiveRun, adopt_legacy, later, tree, write_marker
from chat_fixtures import write_chat

from tkn_genai_chat_note.archive_reader import (
    ArchiveContent,
    ArchiveError,
    chat_source_inputs,
    load_archive_index,
)
from tkn_genai_chat_note.config import AppConfig
from tkn_genai_chat_note.pipeline import run_pipeline
from tkn_genai_chat_note.provenance import validate_provenance
from tkn_genai_chat_note.raw_capture import RawCaptureError, ingest_raw_sources
from tkn_genai_chat_note.runtime import PipelineError
from tkn_genai_chat_note.storage_migration import migrate_storage

SOURCE = "windows"


def config_for(tmp_path: Path, **settings: Any) -> AppConfig:
    return AppConfig(
        sources={
            SOURCE: {
                "source_root": tmp_path / "codex",
                "raw_root": tmp_path / "raw",
                "data_root": tmp_path / "data",
                "state_root": tmp_path / "state",
                **settings,
            }
        },
        cache_root=tmp_path / "cache",
        genai_profiles={"codex": {"provider": "codex", "model": "test"}}, generation={"genai_profile": "codex"},
    )


def chat(tmp_path: Path, name: str, thread_id: str, **kwargs: Any) -> bytes:
    path = tmp_path / "fixtures" / f"{name}.jsonl"
    write_chat(path, thread_id=thread_id, cwd=tmp_path / "repo", **kwargs)
    return path.read_bytes()


def app_state(project_root: Path, assignments: dict[str, str]) -> bytes:
    return json.dumps({
        "local-projects": {
            "proj": {"id": "proj", "name": "proj", "rootPaths": [str(project_root)],
                     "createdAt": "2026-01-01T00:00:00Z"},
        },
        "thread-project-assignments": {
            thread: {"projectKind": "local", "projectId": project} for thread, project in assignments.items()
        },
        "projectless-thread-ids": [],
    }).encode("utf-8")


def threads(config: AppConfig) -> dict[str, dict[str, Any]]:
    catalog = json.loads((config.data_root / "catalog" / "threads.json").read_text(encoding="utf-8"))
    return {entry["threadId"]: entry for entry in catalog["threads"]}


def blob(config: AppConfig, digest: str) -> bytes:
    return (config.data_root / "provenance" / "blobs" / digest[:2] / digest).read_bytes()


def test_pull_reads_archive_only_and_never_writes_raw_root(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    raw = config.raw_root
    write_marker(raw, SOURCE)
    first = ArchiveRun(raw, SOURCE, "20261102T030000+0900", "2026-11-02T03:00:00+09:00")
    compressed = chat(tmp_path, "a", "thread-a", request="compressed request")
    first.version("sessions/2026/11/02/a.jsonl.zst", compressed, encoding="zstd")
    first.version("archived_sessions/b.jsonl", chat(tmp_path, "b", "thread-b", request="archived request"))
    first.version(".codex-global-state.json", app_state(tmp_path / "repo", {"thread-a": "proj"}))
    first.version("session_index.jsonl", b'{"id":"thread-a","thread_name":"Archived title"}\n')
    first.commit()
    before = tree(raw)

    report = run_pipeline(config, mode="clone")

    assert report["complete"], report
    assert tree(raw) == before
    assert not (raw / ".pipeline.lock").exists()
    assert not (raw / ".tkn-genai-chat-note-root.json").exists()
    assert not config.codex_home.exists()
    entries = threads(config)
    assert set(entries) == {"thread-a", "thread-b"}
    a = entries["thread-a"]
    assert a["title"] == "Archived title" and a["titleSource"] == "session-index"
    assert a["membership"]["status"] == "explicit" and a["membership"]["sourceProjectId"] == "proj"
    assert a["sourceRefs"] == ["sessions/2026/11/02/a.jsonl.zst"]
    assert a["sourceCaptureRef"] == (
        "raw:/codex/windows/snapshots/20261102T030000+0900/sessions/2026/11/02/a.jsonl.zst"
    )
    # Review Raw excerpts read this snapshot, so it holds the decoded lines.
    assert a["sourceCaptureSha256"] == sha256(compressed).hexdigest()
    assert blob(config, a["sourceCaptureSha256"]) == compressed
    assert a["membershipHistory"][-1]["metadataRef"] == (
        "raw:/codex/windows/snapshots/20261102T030000+0900/.codex-global-state.json"
    )
    assert report["rawIngest"]["mode"] == "archive-read"
    assert report["rawIngest"]["archive"]["manifestCount"] == 1
    assert report["rawIngest"]["capturedCount"] == 0
    assert validate_provenance(config.data_root)["ok"]

    again = run_pipeline(config, mode="pull")

    assert again["complete"], again
    assert tree(raw) == before
    assert threads(config)["thread-a"]["canonicalSha256"] == a["canonicalSha256"]


def test_adopted_store_keeps_old_references_after_codex_compresses_a_log(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    write_chat(config.sessions_root / "x.jsonl", thread_id="thread-x", cwd=tmp_path / "repo", request="kept")
    write_chat(config.codex_home / "archived_sessions" / "z.jsonl", thread_id="thread-z", cwd=tmp_path / "repo")
    config.app_state_path.write_bytes(app_state(tmp_path / "repo", {"thread-x": "proj"}))
    assert run_pipeline(config, mode="clone")["complete"]
    old = threads(config)
    old_canonical = sorted((config.data_root / "source-aligned").rglob("*.json"))

    adopt_legacy(config.raw_root, SOURCE)
    capture = ArchiveRun.at(config.raw_root, SOURCE, later())
    capture.version("sessions/x.jsonl.zst", (config.sessions_root / "x.jsonl").read_bytes(), encoding="zstd")
    capture.missing("sessions/x.jsonl")
    new_chat = chat(tmp_path, "y", "thread-y", request="only compressed")
    capture.version("sessions/y.jsonl.zst", new_chat, encoding="zstd")
    capture.commit()
    shutil.rmtree(config.codex_home)
    before = tree(config.raw_root)

    report = run_pipeline(config, mode="pull")

    assert report["complete"], report
    assert tree(config.raw_root) == before
    current = threads(config)
    for thread_id in ("thread-x", "thread-z"):
        for field in ("canonicalSha256", "sourceCaptureRef", "sourceRefs", "captureRefs", "membership"):
            assert current[thread_id][field] == old[thread_id][field], (thread_id, field)
    assert current["thread-x"]["sourceCaptureRef"] == "raw:/codex/windows/sessions/x.jsonl"
    assert current["thread-x"]["membershipHistory"] == old["thread-x"]["membershipHistory"]
    assert "/snapshots/" in current["thread-y"]["sourceCaptureRef"]
    assert current["thread-y"]["sourceCaptureRef"].endswith("/sessions/y.jsonl.zst")
    assert blob(config, current["thread-y"]["sourceCaptureSha256"]) == new_chat
    new_canonical = sorted((config.data_root / "source-aligned").rglob("*.json"))
    assert set(old_canonical) <= set(new_canonical) and len(new_canonical) == len(old_canonical) + 1
    sources = {item["sourceRef"]: item for item in report["rawIngest"]["sources"]}
    assert sources["sessions/x.jsonl"]["aliasRefs"] == ["sessions/x.jsonl.zst"]
    assert sources["sessions/x.jsonl"]["originalPresent"] is True
    assert report["rawIngest"]["archive"]["aliasCount"] == 1
    assert validate_provenance(config.data_root)["ok"]


def test_dry_run_decodes_outside_every_configured_root(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    write_marker(config.raw_root, SOURCE)
    run = ArchiveRun(config.raw_root, SOURCE, "20261102T030000+0900", "2026-11-02T03:00:00+09:00")
    run.version("sessions/a.jsonl.zst", chat(tmp_path, "a", "thread-a"), encoding="zstd")
    run.commit()
    before = tree(tmp_path)

    report = run_pipeline(config, mode="clone", dry_run=True)

    assert report["ok"] and report["normalizedThreadCount"] == 1, report
    assert tree(tmp_path) == before
    assert not config.data_root.exists() and not config.state_root.exists() and not config.cache_root.exists()


def test_scheduled_run_reads_archive_without_generating_reviews(tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    import tkn_genai_chat_note.reviews as reviews
    from tkn_genai_chat_note.pipeline import run_scheduled
    from tkn_genai_chat_note.review_generation import ReviewGenerator

    config = config_for(tmp_path, reviews={"start_date": date(2026, 5, 18)})
    write_marker(config.raw_root, SOURCE)
    run = ArchiveRun(config.raw_root, SOURCE, "20260525T030000+0900", "2026-05-25T03:00:00+09:00")
    run.version("sessions/one.jsonl.zst", chat(tmp_path, "one", "one", request="Scheduled review",
                                                 started_at="2026-05-19T00:00:00Z"), encoding="zstd")
    run.commit()
    before = tree(config.raw_root)

    def forbidden(*_: Any, **__: Any) -> None:
        raise AssertionError("scheduled-run must not generate reviews")

    monkeypatch.setattr(ReviewGenerator, "__init__", forbidden)
    monkeypatch.setattr(reviews, "review_today", lambda _: date(2026, 5, 25))

    result = run_scheduled(config)

    assert result["ok"] and result["normalizedThreadCount"] == 1, result
    assert result["reviewsScheduled"] is False and "reviews" not in result
    assert not config.review_data_root.exists()
    assert tree(config.raw_root) == before


def test_confirmed_manifests_decide_the_latest_version(tmp_path: Path) -> None:
    root = tmp_path / "raw"
    write_marker(root, SOURCE)
    first = ArchiveRun(root, SOURCE, "20261101T000000+0900", "2026-11-01T10:00:00+09:00")
    first.version("sessions/a.jsonl", b'{"v":1}\n')
    first.version("sessions/b.jsonl", b'{"b":1}\n', captured_at="2026-11-01T12:00:00+09:00")
    first.commit()
    second = ArchiveRun(root, SOURCE, "20261102T000000+0900", "2026-11-01T09:00:00+09:00")
    second.version("sessions/a.jsonl", b'{"v":2}\n')  # Earlier capturedAt loses despite a later run.
    second.version("sessions/b.jsonl", b'{"b":2}\n', captured_at="2026-11-01T12:00:00+09:00")
    second.commit()
    unconfirmed = ArchiveRun(root, SOURCE, "20261103T000000+0900", "2026-11-03T00:00:00+09:00")
    unconfirmed.version("sessions/a.jsonl", b'{"v":3}\n')
    unconfirmed.commit(confirmed=False)
    partial = ArchiveRun(root, SOURCE, "20261104T000000+0900", "2026-11-04T00:00:00+09:00")
    partial.version("sessions/a.jsonl", b'{"v":4}\n')
    partial.commit(confirmed=False, partial=True)

    archive = ArchiveContent(load_archive_index(root, SOURCE), tmp_path / "decoded")
    inputs, report = chat_source_inputs(archive, include_archived=True, dry_run=False)

    digests = {item.source_ref: item.capture_sha256 for item in inputs}
    assert digests == {
        "sessions/a.jsonl": sha256(b'{"v":1}\n').hexdigest(),
        "sessions/b.jsonl": sha256(b'{"b":2}\n').hexdigest(),
    }
    assert report["archive"]["manifestCount"] == 2
    assert report["archive"]["ignoredManifests"] == [
        {"manifest": "20261104T000000+0900.jsonl.partial", "reason": "partial"},
        {"manifest": "20261103T000000+0900.jsonl", "reason": "unconfirmed"},
    ]
    assert not (tmp_path / "decoded").exists()


def test_damaged_version_falls_back_to_same_content_then_fails(tmp_path: Path) -> None:
    root = tmp_path / "raw"
    write_marker(root, SOURCE)
    content = b'{"type":"event_msg","payload":{}}\n'
    (root / "sessions").mkdir()
    (root / "sessions" / "x.jsonl").write_bytes(content)
    legacy = ArchiveRun(root, SOURCE, "20261101T000000+0900", "2026-09-01T00:00:00+09:00")
    legacy.version("sessions/x.jsonl", content, stored_path="sessions/x.jsonl", method="legacy-import", write=False)
    legacy.commit()
    capture = ArchiveRun(root, SOURCE, "20261102T000000+0900", "2026-11-02T00:00:00+09:00")
    compressed = capture.version("sessions/x.jsonl.zst", content, encoding="zstd")
    capture.commit()
    (root / "sessions" / "x.jsonl").write_bytes(content + b"tampered\n")

    archive = ArchiveContent(load_archive_index(root, SOURCE), tmp_path / "decoded")
    inputs, report = chat_source_inputs(archive, include_archived=True, dry_run=False)

    assert [item.capture_ref for item in inputs] == [f"raw:/codex/windows/{compressed['storedPath']}"]
    assert inputs[0].source_path.read_bytes() == content and not report["failed"]

    (root / compressed["storedPath"]).write_bytes(b"not zstd")
    inputs, report = chat_source_inputs(
        ArchiveContent(load_archive_index(root, SOURCE), tmp_path / "decoded"), include_archived=True, dry_run=False
    )

    assert inputs == []
    assert [failure["sourceRef"] for failure in report["failed"]] == ["sessions/x.jsonl"]
    assert "does not match its manifest" in report["failed"][0]["error"]


def test_divergent_compressed_copy_keeps_both_histories(tmp_path: Path) -> None:
    root = tmp_path / "raw"
    write_marker(root, SOURCE)
    run = ArchiveRun(root, SOURCE, "20261102T000000+0900", "2026-11-02T00:00:00+09:00")
    run.version("sessions/x.jsonl", chat(tmp_path, "x1", "same", request="first branch"))
    run.version("sessions/x.jsonl.zst", chat(tmp_path, "x2", "same", request="second branch"), encoding="zstd")
    run.commit()
    config = config_for(tmp_path)

    report = run_pipeline(config, mode="clone")

    assert report["complete"], report
    entry = threads(config)["same"]
    assert entry["sourceRefs"] == ["sessions/x.jsonl", "sessions/x.jsonl.zst"]
    assert len(entry["historyBranches"]) == 2


def test_decoded_copies_are_reused_and_unused_ones_pruned(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    write_marker(config.raw_root, SOURCE)
    run = ArchiveRun(config.raw_root, SOURCE, "20261102T000000+0900", "2026-11-02T00:00:00+09:00")
    content = chat(tmp_path, "a", "thread-a")
    run.version("sessions/a.jsonl.zst", content, encoding="zstd")
    run.commit()
    assert run_pipeline(config, mode="clone")["complete"]
    decoded_root = config.source_cache_root / "archive-content"
    digest = sha256(content).hexdigest()
    decoded = decoded_root / digest[:2] / f"{digest}.jsonl"
    assert decoded.read_bytes() == content
    stale = decoded_root / "ab" / ("ab" + "0" * 62 + ".jsonl")
    stale.parent.mkdir()
    stale.write_bytes(b"old")
    modified = decoded.stat().st_mtime_ns

    report = run_pipeline(config, mode="pull")

    assert report["complete"], report
    assert report["rawIngest"]["archive"]["prunedContentCount"] == 1
    assert not stale.exists() and decoded.stat().st_mtime_ns == modified


def test_archived_sessions_respect_include_archived(tmp_path: Path) -> None:
    root = tmp_path / "raw"
    write_marker(root, SOURCE)
    run = ArchiveRun(root, SOURCE, "20261102T000000+0900", "2026-11-02T00:00:00+09:00")
    run.version("sessions/a.jsonl", b"{}\n")
    run.version("archived_sessions/b.jsonl.zst", b"{}\n{}\n", encoding="zstd")
    run.version("history.jsonl", b"{}\n")
    run.commit()

    archive = ArchiveContent(load_archive_index(root, SOURCE), tmp_path / "decoded")
    included, _ = chat_source_inputs(archive, include_archived=True, dry_run=False)
    excluded, _ = chat_source_inputs(archive, include_archived=False, dry_run=False)

    assert [item.source_ref for item in included] == ["archived_sessions/b.jsonl.zst", "sessions/a.jsonl"]
    assert [item.source_ref for item in excluded] == ["sessions/a.jsonl"]


@pytest.mark.parametrize("damage", ["invalid-line", "other-source", "unsafe-path", "content-error"])
def test_untrustworthy_archive_records_are_not_used(tmp_path: Path, damage: str) -> None:
    root = tmp_path / "raw"
    write_marker(root, SOURCE)
    run = ArchiveRun(root, SOURCE, "20261102T000000+0900", "2026-11-02T00:00:00+09:00")
    record = run.version("sessions/a.jsonl.zst", b"{}\n", encoding="zstd",
                         content_error="zstd: corrupt frame" if damage == "content-error" else None)
    if damage == "other-source":
        record["sourceId"] = "other"
    if damage == "unsafe-path":
        record["storedPath"] = "../outside.jsonl"
    path = run.commit()
    if damage == "invalid-line":
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text("\n".join([lines[0], "{not json", lines[-1]]) + "\n", encoding="utf-8")

    if damage == "content-error":
        inputs, report = chat_source_inputs(
            ArchiveContent(load_archive_index(root, SOURCE), tmp_path / "decoded"),
            include_archived=True, dry_run=False,
        )
        assert inputs == [] and "could not decode" in report["failed"][0]["error"]
        return
    with pytest.raises(ArchiveError):
        load_archive_index(root, SOURCE)


def test_marker_of_another_source_stops_before_any_write(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    write_marker(config.raw_root, "other-pc")
    before = tree(tmp_path)

    with pytest.raises(PipelineError, match="sourceId"):
        run_pipeline(config, mode="clone")

    assert tree(tmp_path) == before


def test_raw_writers_refuse_an_archive_managed_root(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    write_marker(config.raw_root, SOURCE)
    write_chat(config.sessions_root / "a.jsonl", thread_id="a", cwd=tmp_path)
    before = tree(tmp_path)

    with pytest.raises(PipelineError, match="tkn-genai-chat-archiver capture"):
        run_pipeline(config, mode="raw")
    with pytest.raises(RawCaptureError, match="read-only"):
        ingest_raw_sources(config.sessions_root, config.raw_root, SOURCE, dry_run=False, captured_at="now")
    with pytest.raises(PipelineError, match="storage migrate"):
        migrate_storage(config, from_config=tmp_path / "old.yaml", dry_run=False)

    assert tree(tmp_path) == before
