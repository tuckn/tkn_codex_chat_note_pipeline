from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace

import pytest
from test_api_inference import fake_http as fake_http
from test_generation_estimates import review
from test_repair_recovery import azure_runner, reply
from test_session_note_pipeline import note_data
from test_thread_timeline import candidate, config, event

from tkn_codex_chat_note.generation_cache import content_hash
from tkn_codex_chat_note.session_notes import ProviderSummarizer, validate_note_data
from tkn_codex_chat_note.state_reconciliation import (
    collect_state_items,
    public_note_data,
    reconcile_state,
    validate_pending_state_items,
)


def partial_with_pending(tmp_path, events, *, origins, kind="unverified"):
    part = note_data(candidate(tmp_path, events))
    part["lastKnownState"][kind] = ["確認作業が残っている。"]
    part["lastKnownState"]["eventIds"] = [e.id for e in events]
    if kind == "unresolved":
        part["lastKnownState"]["workState"] = "in-progress"
    part["pendingStateItems"] = [{"kind": kind, "itemIndex": 0, "eventIds": origins}]
    return part


def test_unrelated_later_state_citation_does_not_block_resolution(tmp_path):
    pending, resolved, unrelated = (event("L000150"), event("L000192"), event("L000205"))
    partial = partial_with_pending(tmp_path, (pending, unrelated), origins=[pending.id])
    items = collect_state_items([partial])
    assert items[0]["eventIds"] == [pending.id]
    value = note_data(candidate(tmp_path, (unrelated,)))
    value["stateItemReviews"] = [review(items[0], "resolved", [resolved.id])]
    result = reconcile_state(value, items, (pending, resolved, unrelated))
    assert not result["lastKnownState"]["unverified"]
    assert resolved.id in result["lastKnownState"]["eventIds"]


def test_missing_item_sources_never_fall_back_to_overall_state_citations(tmp_path):
    part = partial_with_pending(tmp_path, (event("pending"),), origins=["pending"])
    part.pop("pendingStateItems")
    with pytest.raises(ValueError, match="pendingStateItems"):
        collect_state_items([part])


def test_later_reopened_item_cannot_be_cleared_by_earlier_resolution(tmp_path):
    opened, resolved, reopened = (event("opened"), event("resolved"), event("reopened"))
    parts = [partial_with_pending(tmp_path, (e,), origins=[e.id]) for e in (opened, reopened)]
    items = collect_state_items(parts)
    assert len(items) == 2  # Equal wording does not collapse separate occurrences.
    value = note_data(candidate(tmp_path, (reopened,)))
    value["stateItemReviews"] = [review(item, "resolved", [resolved.id]) for item in items]
    with pytest.raises(ValueError, match="after the pending item"):
        reconcile_state(value, items, (opened, resolved, reopened))


def test_resolving_multiple_histories_requires_later_proof_for_each(tmp_path):
    a, b, later_a, later_b = tuple(replace(event(name), branch_id=branch) for name, branch in (
        ("a", "A"), ("b", "B"), ("later-a", "A"), ("later-b", "B")))
    part = partial_with_pending(tmp_path, (a, b), origins=[a.id, b.id])
    items = collect_state_items([part])
    value = note_data(candidate(tmp_path, (later_a, later_b)))
    value["stateItemReviews"] = [review(items[0], "resolved", [later_a.id, b.id])]
    with pytest.raises(ValueError, match="after the pending item"):
        reconcile_state(value, items, (a, b, later_a, later_b))
    value["stateItemReviews"][0]["eventIds"] = [later_a.id, later_b.id]
    assert not reconcile_state(value, items, (a, b, later_a, later_b))["lastKnownState"]["unverified"]


def test_done_conflict_identifies_retained_request_without_changing_status_or_dropping_it(tmp_path):
    source = event("pending")
    part = partial_with_pending(tmp_path, (source,), origins=[source.id], kind="unresolved")
    items = collect_state_items([part])
    value = note_data(candidate(tmp_path, (source,)))
    value["stateItemReviews"] = [review(items[0])]
    original = deepcopy(value)
    with pytest.raises(ValueError, match="S00001.*lastKnownState.workState"):
        reconcile_state(value, items, (source,))
    assert value == original
    value["lastKnownState"]["workState"] = "waiting-for-user"
    result = reconcile_state(value, items, (source,))
    assert result["lastKnownState"]["unresolved"] == part["lastKnownState"]["unresolved"]
    assert result["lastKnownState"]["workState"] == "waiting-for-user"


def test_resolution_review_cannot_contradict_final_pending_list(tmp_path):
    source, later = event("pending"), event("later")
    part = partial_with_pending(tmp_path, (source,), origins=[source.id])
    items = collect_state_items([part])
    value = deepcopy(part)
    value["stateItemReviews"] = [review(items[0], "resolved", [later.id])]
    with pytest.raises(ValueError, match="resolved.*still present"):
        reconcile_state(value, items, (source, later))


@pytest.mark.parametrize("fault", ["missing", "duplicate", "negative", "bool", "string", "out-of-range",
                                  "unknown-event", "empty", "repeated-event", "wrong-kind", "unneeded"])
def test_pending_evidence_must_match_each_item_and_known_source(tmp_path, fault):
    source = event("pending")
    part = partial_with_pending(tmp_path, (source,), origins=[source.id])
    record = part["pendingStateItems"][0]
    if fault == "missing":
        part.pop("pendingStateItems")
    elif fault == "duplicate":
        part["pendingStateItems"].append(deepcopy(record))
    elif fault in {"negative", "bool", "string", "out-of-range"}:
        record["itemIndex"] = {"negative": -1, "bool": True, "string": "0", "out-of-range": 1}[fault]
    elif fault == "unknown-event":
        record["eventIds"] = ["invented"]
    elif fault == "empty":
        record["eventIds"] = []
    elif fault == "repeated-event":
        record["eventIds"] = [source.id, source.id]
    elif fault == "wrong-kind":
        record["kind"] = "unresolved"
    else:
        part["lastKnownState"]["unverified"] = []
    with pytest.raises(ValueError, match="pendingStateItems"):
        validate_pending_state_items(part, {source.id})


def test_resolved_and_retained_occurrences_with_equal_text_remain_distinct(tmp_path):
    opened, resolved, reopened = event("opened"), event("resolved"), event("reopened")
    items = collect_state_items([
        partial_with_pending(tmp_path, (e,), origins=[e.id]) for e in (opened, reopened)
    ])
    value = note_data(candidate(tmp_path, (reopened,)))
    value["stateItemReviews"] = [review(items[0], "resolved", [resolved.id]), review(items[1])]
    result = reconcile_state(value, items, (opened, resolved, reopened))
    assert result["lastKnownState"]["unverified"] == [items[1]["text"]]
    assert set(result["lastKnownState"]["eventIds"]) == {resolved.id, reopened.id}


def test_item_evidence_is_cached_but_never_leaks_into_public_note(tmp_path, monkeypatch):
    source = event("pending")
    case = candidate(tmp_path, (source,))
    part = partial_with_pending(tmp_path, case.events, origins=[source.id])
    cache = tmp_path / "cache"
    runner = ProviderSummarizer(config(tmp_path), cache_root=cache)
    monkeypatch.setattr(runner, "_invoke", lambda *args, **kwargs: deepcopy(part))
    result = runner.generate(case)
    assert "pendingStateItems" not in result
    validate_note_data(result, {source.id})
    cached_path = next(cache.rglob("*.json"))
    envelope = json.loads(cached_path.read_text(encoding="utf-8"))
    assert envelope["value"]["pendingStateItems"] == part["pendingStateItems"]
    resumed = ProviderSummarizer(config(tmp_path), cache_root=cache)
    assert resumed.estimate(case)["cachedChunkCount"] == 1
    assert resumed.generate(case) == result
    assert resumed.last_metrics["reusedChunks"] == 1
    # A hash-valid checkpoint missing item evidence is not usable by estimate or generation.
    envelope["value"].pop("pendingStateItems")
    envelope["sha256"] = content_hash(envelope["value"])
    cached_path.write_text(json.dumps(envelope), encoding="utf-8")
    regenerated = ProviderSummarizer(config(tmp_path), cache_root=cache)
    assert regenerated.estimate(case)["cachedChunkCount"] == 0
    monkeypatch.setattr(regenerated, "_invoke", lambda *args, **kwargs: deepcopy(part))
    assert regenerated.generate(case) == result
    assert regenerated.last_metrics["rejectedCheckpoints"] == 1


def test_merge_uses_item_origins_and_generates_reviews_before_final_state(tmp_path, monkeypatch):
    pending, resolution, unrelated = event("pending"), event("resolution"), event("unrelated")
    case = candidate(tmp_path, (pending, resolution, unrelated))
    runner = ProviderSummarizer(config(tmp_path), chunk_characters=250, cache_root=tmp_path / "cache")

    def invoke(prompt, *, overview_only=False):
        payload = json.loads(prompt.split("BEGIN_INPUT_JSON\n", 1)[1].split("\nEND_INPUT_JSON", 1)[0])
        if not overview_only:
            source = next(e for e in case.events if e.id == payload["events"][0]["id"])
            part = note_data(candidate(tmp_path, (source,)))
            if source.id == pending.id:
                part = partial_with_pending(tmp_path, (source,), origins=[source.id])
            return part
        assert next(iter(runner.overview_schema["properties"])) == "stateItemReviews"
        review_schema = runner.overview_schema["properties"]["stateItemReviews"]
        assert review_schema["items"]["properties"]["itemId"]["enum"] == ["S00001"]
        assert payload["stateItems"][0]["eventIds"] == [pending.id]
        assert all("pendingStateItems" not in p for p in payload["partials"])
        result = note_data(candidate(tmp_path, (unrelated,)))
        result.pop("timeline")
        result["stateItemReviews"] = [review(payload["stateItems"][0], "resolved", [resolution.id])]
        return result

    monkeypatch.setattr(runner, "_invoke", invoke)
    result = runner.generate(case)
    assert not result["lastKnownState"]["unverified"]
    assert resolution.id in result["lastKnownState"]["eventIds"]
    assert len(result["timeline"]) == 6
    assert "stateItemReviews" not in result and "pendingStateItems" not in result
    assert runner.last_metrics["stateItems"][0]["eventIds"] == [pending.id]
    resumed = ProviderSummarizer(config(tmp_path), chunk_characters=250, cache_root=tmp_path / "cache")
    assert resumed.generate(case) == result
    assert resumed.last_metrics["reusedChunks"] == 3 and resumed.last_metrics["reusedReductions"] == 1
    assert result == public_note_data(result)


def test_azure_pending_evidence_uses_source_aliases_and_is_not_published(tmp_path, fake_http):
    calls, replies = fake_http
    source = event("known")
    case = candidate(tmp_path, (source,))
    part = partial_with_pending(tmp_path, case.events, origins=[source.id])
    replies.append(reply(part))
    runner = azure_runner(tmp_path)
    result = runner.generate(case)
    assert result == public_note_data(part) and len(calls) == 1
    body = json.loads(calls[0].content)
    schema = body["response_format"]["json_schema"]["schema"]
    assert "pendingStateItems" in schema["required"]
    ref = schema["properties"]["pendingStateItems"]["items"]["properties"]["eventIds"]["items"]
    assert ref == {"$ref": "#/$defs/sourceEventId"}
    assert schema["$defs"]["sourceEventId"]["enum"] == ["E00001"]
    cached = json.loads(next((tmp_path / "cache").rglob("*.json")).read_text(encoding="utf-8"))
    assert cached["value"]["pendingStateItems"][0]["eventIds"] == [source.id]


def test_missing_item_evidence_is_repaired_before_accepting_a_partial(tmp_path, monkeypatch):
    source = event("pending")
    case = candidate(tmp_path, (source,))
    good = partial_with_pending(tmp_path, case.events, origins=[source.id])
    bad = public_note_data(good)
    runner = ProviderSummarizer(config(tmp_path))
    prompts = []

    def invoke(prompt, **kwargs):
        prompts.append(prompt)
        return deepcopy(bad if len(prompts) == 1 else good)

    monkeypatch.setattr(runner, "_invoke", invoke)
    assert runner.generate(case) == public_note_data(good)
    assert len(prompts) == 2 and "MODE: repair-invalid-draft" in prompts[1]
    assert runner.last_metrics["semanticRetries"] == 1
    assert "pendingStateItems" in runner.last_metrics["validationFailures"][0]["reason"]
