from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch

import pytest
from test_session_note_pipeline import note_data
from test_thread_timeline import candidate, config, event

from tkn_codex_chat_note.generation_cache import content_hash
from tkn_codex_chat_note.inference_inputs import compact_duplicate_outputs
from tkn_codex_chat_note.session_notes import ProviderSummarizer, prepare_events
from tkn_codex_chat_note.summary_resources import validate_summary_output_schema


def duplicate_events(*, escaped: bool = False):
    output = 'Read C:\\example\\file.py; keep literal \\n.\r\n' * 60
    stored = json.dumps(output, ensure_ascii=False)[1:-1] if escaped else output
    result = replace(event("result", actor="tool"), kind="tool_result", name="call-1",
                     text="Exit code: 0\nWall time: 1 seconds\nOutput:\n" + output)
    payload = {"item": {"type": "CommandExecution", "id": "call-1", "status": "completed",
                        "exit_code": 0, "aggregated_output": stored}}
    completed = replace(result, id="completed", kind="event", name="item_completed", text=json.dumps(payload))
    return result, completed


@pytest.mark.parametrize("escaped", [False, True])
def test_duplicate_output_is_referenced_without_losing_events_or_completion_metadata(escaped):
    sources = duplicate_events(escaped=escaped)
    originals = deepcopy(sources)
    prepared = prepare_events(sources)
    payload = json.loads(prepared[1].text)
    assert payload["item"]["aggregated_output"]["duplicateOfEventId"] == "result"
    assert payload["item"]["exit_code"] == 0
    assert [e.id for e in prepared] == ["result", "completed"]
    assert prepared[0].text == sources[0].text
    assert prepared[1].duplicate_characters_removed > 1000
    assert sources == originals
    assert prepare_events(sources, deduplicate=False)[1].text == sources[1].text


@pytest.mark.parametrize("difference", ["body", "literal-escape", "call", "turn", "branch", "user", "ambiguous"])
def test_distinct_work_and_ambiguous_matches_keep_both_bodies(difference):
    result, completed = duplicate_events()
    if difference == "body":
        result = replace(result, text=result.text + "failure")
    if difference == "literal-escape":
        result = replace(result, text=result.text.replace("literal \\n", "literal \n"))
    if difference == "call":
        result = replace(result, name="another-call")
    if difference == "turn":
        result = replace(result, turn_id="another-turn")
    if difference == "branch":
        result = replace(result, branch_id="another-history")
    sources = [result, completed]
    if difference == "user":
        sources.insert(1, event("intervening-user"))
    if difference == "ambiguous":
        sources.append(replace(result, id="another-result"))
    assert compact_duplicate_outputs(sources) == {}


def fake_inference(case):
    def invoke(_config, prompt, schema, **_kwargs):
        payload = json.loads(prompt.split("BEGIN_INPUT_JSON\n")[1].split("\nEND_INPUT_JSON")[0])
        if "partials" in payload:
            data = note_data(case)
            data.pop("timeline")
        else:
            ids = {item["id"] for item in payload["events"]}
            selected = tuple(item for item in case.events if item.id in ids)
            data = note_data(replace(case, events=selected))
            for item in data["timeline"]:
                anchor = item.pop("startEventId")
                item.pop("endEventId")
                item["eventId"] = anchor
            assert schema["properties"]["timeline"]["items"]["properties"]["eventId"]["enum"] == sorted(ids)
        if "pendingStateItems" in schema["properties"]:
            data["pendingStateItems"] = []
        validate_summary_output_schema(data, schema)
        return data
    return invoke


def test_anchor_schema_eliminates_range_generation_but_keeps_public_contract(tmp_path):
    case = candidate(tmp_path, (event("user"), event("assistant", actor="assistant")))
    runner = ProviderSummarizer(config(tmp_path))
    with patch("tkn_codex_chat_note.session_notes.invoke_structured", side_effect=fake_inference(case)) as model:
        result = runner.generate(case)
    assert model.call_count == 1 and runner.last_metrics["semanticRetries"] == 0
    assert all(i["startEventId"] == i["endEventId"] for i in result["timeline"])
    assert all("eventId" not in i for i in result["timeline"])
    assert runner.last_metrics["submittedPromptCharacters"] > 0


def test_interruption_reuses_validated_chunks_and_merge_across_process_instances(tmp_path):
    case = candidate(tmp_path, tuple(event(f"e{i}") for i in range(3)))
    cache = tmp_path / "cache"
    first = ProviderSummarizer(config(tmp_path), chunk_characters=250, cache_root=cache)
    invoke = fake_inference(case)
    attempts = 0

    def interrupted(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise KeyboardInterrupt
        return invoke(*args, **kwargs)

    with patch("tkn_codex_chat_note.session_notes.invoke_structured", side_effect=interrupted):
        with pytest.raises(KeyboardInterrupt):
            first.generate(case)
    assert len(list(cache.rglob("*.json"))) == 1
    resumed = ProviderSummarizer(config(tmp_path), chunk_characters=250, cache_root=cache)
    with patch("tkn_codex_chat_note.session_notes.invoke_structured", side_effect=invoke) as model:
        result = resumed.generate(case)
    assert model.call_count == 3  # Two remaining chunks and one merge.
    assert resumed.last_metrics["reusedChunks"] == 1
    assert resumed.last_metrics["modelCalls"] == 3
    completed = ProviderSummarizer(config(tmp_path), chunk_characters=250, cache_root=cache)
    with patch("tkn_codex_chat_note.session_notes.invoke_structured", side_effect=AssertionError("unexpected call")):
        assert completed.generate(case) == result
    assert completed.last_metrics["reusedChunks"] == 3
    assert completed.last_metrics["reusedReductions"] == 1
    assert completed.last_metrics["modelCalls"] == 0


@pytest.mark.parametrize("change", ["text", "model", "effort", "source", "thread", "chunk", "force"])
def test_checkpoint_identity_and_force_prevent_inappropriate_reuse(tmp_path, change):
    case = candidate(tmp_path, (event("e"),))
    settings = config(tmp_path)
    cache = tmp_path / "cache"
    runner = ProviderSummarizer(settings, cache_root=cache)
    with patch("tkn_codex_chat_note.session_notes.invoke_structured", side_effect=fake_inference(case)):
        runner.generate(case)
    if change == "text":
        case = replace(case, events=(replace(case.events[0], text="request  changed"),))
    if change == "model":
        settings = replace(settings, model="another-model")
    if change == "effort":
        settings = replace(settings, reasoning_effort="low")
    if change == "source":
        settings = replace(settings, source_id="another-source")
    if change == "thread":
        case = replace(case, thread_id="another-thread")
    runner = ProviderSummarizer(settings, chunk_characters=99999 if change == "chunk" else 120000,
                                cache_root=cache)
    if change == "force":
        runner.set_cache_root(cache, reuse=False)
    with patch("tkn_codex_chat_note.session_notes.invoke_structured", side_effect=fake_inference(case)) as model:
        runner.generate(case)
    assert model.call_count == 1
    assert runner.last_metrics["reusedChunks"] == 0


@pytest.mark.parametrize("corruption", ["json", "hash", "semantics"])
def test_corrupt_or_invalid_checkpoint_is_regenerated(tmp_path, corruption):
    case = candidate(tmp_path, (event("e"),))
    cache = tmp_path / "cache"
    runner = ProviderSummarizer(config(tmp_path), cache_root=cache)
    with patch("tkn_codex_chat_note.session_notes.invoke_structured", side_effect=fake_inference(case)):
        runner.generate(case)
    path = next(cache.rglob("*.json"))
    envelope = json.loads(path.read_text(encoding="utf-8"))
    if corruption == "semantics":
        envelope["value"]["lastKnownState"]["eventIds"] = ["invented"]
        envelope["sha256"] = content_hash(envelope["value"])
    else:
        envelope["sha256"] = "bad"
    path.write_text("{" if corruption == "json" else json.dumps(envelope), encoding="utf-8")
    with patch("tkn_codex_chat_note.session_notes.invoke_structured", side_effect=fake_inference(case)) as model:
        runner.generate(case)
    assert model.call_count == 1
    assert runner.last_metrics["rejectedCheckpoints"] == (1 if corruption == "semantics" else 0)


def test_dry_run_never_creates_generation_checkpoint(tmp_path):
    from test_pipeline_workflow import config_for
    from test_session_note_pipeline import write_chat

    from tkn_codex_chat_note.pipeline import run_pipeline

    settings = config_for(tmp_path)
    write_chat(settings.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path)
    runner = ProviderSummarizer(config(tmp_path), cache_root=tmp_path / "checkpoints")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with patch("tkn_codex_chat_note.session_notes.invoke_structured", side_effect=AssertionError("inference")):
        run_pipeline(settings, mode="clone", dry_run=True, summarizer=runner)
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_pipeline_pull_resumes_chunks_and_publishes_metrics_without_rewriting_raw(tmp_path):
    from test_pipeline_workflow import config_for
    from test_session_note_pipeline import write_chat

    from tkn_codex_chat_note.chat_logs import read_thread_events
    from tkn_codex_chat_note.pipeline import run_pipeline

    settings = config_for(tmp_path)
    source = settings.sessions_root / "one.jsonl"
    write_chat(source, thread_id="one", cwd=tmp_path)
    case = replace(candidate(tmp_path, read_thread_events(source)), thread_id="one")
    invoke = fake_inference(case)
    first = ProviderSummarizer(config(tmp_path), chunk_characters=400)
    attempts = 0

    def interrupted(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise KeyboardInterrupt
        return invoke(*args, **kwargs)

    with patch("tkn_codex_chat_note.session_notes.invoke_structured", side_effect=interrupted):
        with pytest.raises(KeyboardInterrupt):
            run_pipeline(settings, mode="clone", summarizer=first)
    raw = {p: p.read_bytes() for p in settings.raw_root.rglob("*") if p.is_file()}
    resumed = ProviderSummarizer(config(tmp_path), chunk_characters=400)
    with patch("tkn_codex_chat_note.session_notes.invoke_structured", side_effect=invoke) as model:
        report = run_pipeline(settings, mode="pull", summarizer=resumed)
    assert report["complete"], report
    metrics = report["threads"][0]["generationMetrics"]
    assert metrics["reusedChunks"] == 1
    assert metrics["modelCalls"] == model.call_count
    assert all(p.read_bytes() == content for p, content in raw.items())
    notes = {p: p.read_bytes() for p in settings.data_root.rglob("*.md")}
    with patch("tkn_codex_chat_note.session_notes.invoke_structured", side_effect=AssertionError("unchanged")):
        assert run_pipeline(settings, mode="pull", summarizer=resumed)["complete"]
    assert all(p.read_bytes() == content for p, content in notes.items())


def test_failed_atomic_checkpoint_write_keeps_earlier_validated_work(tmp_path):
    case = candidate(tmp_path, tuple(event(f"e{i}") for i in range(3)))
    cache = tmp_path / "cache"
    runner = ProviderSummarizer(config(tmp_path), chunk_characters=250, cache_root=cache)
    from tkn_codex_chat_note.file_io import replace_file

    writes = 0

    def failed_write(source, destination):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("simulated checkpoint write failure")
        replace_file(source, destination)

    with patch("tkn_codex_chat_note.session_notes.invoke_structured", side_effect=fake_inference(case)):
        with patch("tkn_codex_chat_note.generation_cache.replace_file", side_effect=failed_write):
            with pytest.raises(OSError, match="checkpoint write failure"):
                runner.generate(case)
    assert len(list(cache.rglob("*.json"))) == 1
    assert not list(cache.rglob(".tmp-*"))
    with patch("tkn_codex_chat_note.session_notes.invoke_structured", side_effect=fake_inference(case)) as model:
        runner.generate(case)
    assert runner.last_metrics["reusedChunks"] == 1 and model.call_count == 3
