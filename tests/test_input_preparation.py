from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from test_thread_timeline import candidate, config, event

from tkn_codex_chat_note.inference_inputs import compact_duplicate_outputs, compact_event_text
from tkn_codex_chat_note.session_notes import PipelineError, ProviderSummarizer, prepare_events


def tool(text: str, *, name: str = "call-1"):
    return replace(event("output", actor="tool"), kind="tool_result", name=name, text=text)


def test_display_markup_is_removed_only_from_tool_material():
    text = "\x1b[31mError: failed\x1b[0m\nKeep the result."
    source = tool(text)
    prepared = prepare_events([source])[0]
    assert prepared.text == "Error: failed\nKeep the result."
    assert prepared.id == source.id and source.text == text
    assert prepared.input_preparation["sourceCharacters"] == len(text)
    assert not prepared.input_preparation["excerpted"]
    assert compact_event_text(replace(source, kind="user_message"), text) == text
    assert compact_event_text(replace(source, kind="assistant_message"), text) == text


def test_html_keeps_article_facts_and_command_outcome():
    text = ('Exit code: 0\nOutput:\n<!DOCTYPE html><html><head><script>tracking();</script></head>'
            '<body><nav>Sign in</nav><main><h1>Tables</h1><p>Maximum 10 tables &amp; 2 apps.</p>'
            '<pre>Do not delete existing data.</pre></main></body></html>')
    prepared = prepare_events([tool(text)])[0]
    assert "Maximum 10 tables & 2 apps." in prepared.text
    assert "Do not delete existing data." in prepared.text
    assert "Exit code: 0" in prepared.text
    assert "tracking()" not in prepared.text and "Sign in" not in prepared.text
    assert "<main>" not in prepared.text


def test_truncated_html_falls_back_to_preserving_text():
    text = '<html><script>something…100 chars truncated…important finding</html>'
    assert compact_event_text(tool(text), text) == text


def test_bulk_excerpt_retains_middle_failure_context_tail_and_original_source():
    lines = [f"notes/topic.md:{i}: ordinary evidence detail " + "x" * 90 + "\n" for i in range(400)]
    lines[207] = "WARNING: not all files were generated; missing module 17\n"
    lines[208] = "The expected result is 64 files, actual result is 63.\n"
    lines[-1] = "Final outcome: failed, manual action required\n"
    text = "Exit code: 1\nOutput:\n" + "".join(lines)
    source = tool(text)
    prepared = prepare_events([source])[0]
    assert len(prepared.text) < len(text) // 2
    assert "WARNING: not all files were generated" in prepared.text
    assert "expected result is 64 files, actual result is 63" in prepared.text
    assert "Final outcome: failed" in prepared.text and "Exit code: 1" in prepared.text
    assert "[excerpt: source lines" in prepared.text
    assert prepared.input_preparation["excerpted"]
    assert source.text == text


def test_unrecognized_long_prose_and_requests_are_not_excerpted():
    text = "Important observation and a distinct fact.\n" * 1000
    for kind in ["tool_result", "user_message", "assistant_message"]:
        source = replace(tool(text), kind=kind)
        assert prepare_events([source])[0].text == text


@pytest.mark.parametrize("difference", [None, "suffix", "turn", "branch", "ambiguous"])
def test_head_tail_completion_is_referenced_only_when_covered_by_same_call(difference):
    head, tail = "H" * 100, "T" * 100
    result = tool("Exit code: 0\nOutput:\n" + head + "middle" * 200 + tail)
    completion = replace(result, id="completion", name="item_completed", kind="event", text=json.dumps({
        "item": {"type": "CommandExecution", "id": "call-1", "exit_code": 0,
                 "aggregated_output": head + "…1200 chars truncated…" + tail}}))
    if difference == "suffix":
        result = replace(result, text=result.text + "failure")
    if difference == "turn":
        result = replace(result, turn_id="different")
    if difference == "branch":
        result = replace(result, branch_id="different")
    sources = [result, completion]
    if difference == "ambiguous":
        sources.append(replace(result, id="other"))
    replacements = compact_duplicate_outputs(sources)
    if difference:
        assert replacements == {}
    else:
        ref = json.loads(replacements["completion"])["item"]["aggregated_output"]
        assert ref["duplicateOfEventId"] == result.id and ref["sourceTruncated"]


def test_file_addition_duplicate_preserves_path_status_and_patch():
    content = "print('hello')\n" * 200
    source = replace(event("patch", actor="assistant"), kind="tool_call", name="apply_patch",
                     text="*** Begin Patch\n*** Add File: example.py\n" +
                     "".join("+" + line for line in content.splitlines(keepends=True)) + "*** End Patch")
    completed = replace(source, id="completed", kind="event", actor="tool", name="item_completed", text=json.dumps({
        "item": {"type": "FileChange", "status": "completed", "changes": {
            "C:/workspace/example.py": {"content": content}}}}))
    prepared = prepare_events([source, completed])
    data = json.loads(prepared[1].text)["item"]
    assert data["status"] == "completed"
    assert data["changes"]["C:/workspace/example.py"]["content"]["duplicateOfEventId"] == source.id
    assert prepared[0].text == source.text
    different = replace(source, branch_id="different")
    assert compact_duplicate_outputs([different, completed]) == {}


def test_only_oversized_chunks_split_and_all_text_offsets_survive(tmp_path):
    sources = tuple(replace(event(name, actor="assistant"), name=name, text=letter * 1200)
                    for name, letter in [("first", "a"), ("dense", "b"), ("last", "c")])
    runner = ProviderSummarizer(config(tmp_path), chunk_characters=1600)
    def estimate(prompt, schema):
        payload = json.loads(prompt.split("BEGIN_INPUT_JSON\n")[1].split("\nEND_INPUT_JSON")[0])
        assert schema["properties"]["timeline"]["items"]["properties"]["eventId"]["enum"]
        return 200 if any(e["name"] == "dense" and len(e["text"]) > 250 for e in payload["events"]) else 50
    api = SimpleNamespace(limits=SimpleNamespace(chunk_characters=1600, input_tokens=100), estimate=estimate)
    with patch.object(runner, "_api", return_value=api):
        prepared, chunks, target = runner._prepared_chunks(candidate(tmp_path, sources))
    assert target == 1600
    assert chunks[0] == [prepared[0]] and chunks[-1] == [prepared[-1]]
    parts = [e for chunk in chunks for e in chunk if e.name == "dense"]
    assert "".join(e.text for e in parts) == sources[1].text
    assert [e.text_part for e in parts] == list(range(1, len(parts) + 1))
    assert all(e.text_part_count == len(parts) for e in parts)
    assert parts[0].text_start == 0 and parts[-1].text_end == 1200
    assert all(a.text_end == b.text_start for a, b in zip(parts, parts[1:], strict=False))
    assert all(e.full_text_characters == 1200 and len(e.text) <= 250 for e in parts)


def test_impossible_api_budget_terminates_without_invocation(tmp_path):
    runner = ProviderSummarizer(config(tmp_path))
    api = SimpleNamespace(limits=SimpleNamespace(chunk_characters=120000, input_tokens=1), estimate=lambda *args: 100)
    with patch.object(runner, "_api", return_value=api), pytest.raises(PipelineError, match="minimum event input"):
        runner._prepared_chunks(candidate(tmp_path, (replace(event("tiny"), text="x"),)))


def test_html_literals_in_code_and_large_json_configuration_are_preserved():
    text = "source = '<html><head><script>important_code()</script></head><body>text</body></html>'"
    assert compact_event_text(tool(text), text) == text
    settings = {f"setting_{i}": "precise value " * 20 for i in range(100)}
    encoded = "Exit code: 0\nOutput:\n" + json.dumps(settings, indent=2)
    assert compact_event_text(tool(encoded), encoded) == encoded


def test_input_preparation_version_changes_generation_identity(tmp_path, monkeypatch):
    import tkn_codex_chat_note.session_notes as module
    before = module.generator_fingerprint(config(tmp_path))
    monkeypatch.setattr(module, "INPUT_PREPARATION_VERSION", module.INPUT_PREPARATION_VERSION + 1)
    assert module.generator_fingerprint(config(tmp_path)) != before
