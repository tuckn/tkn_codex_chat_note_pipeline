"""Require an explicit, source-backed disposition for each partial pending state item."""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from typing import Any

from .chat_logs import ChatEvent
from .summary_resources import validate_summary_output_schema

PENDING_STATE_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "kind": {"type": "string", "enum": ["unresolved", "unverified"]},
            "itemIndex": {"type": "integer", "minimum": 0},
            "eventIds": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        },
        "required": ["kind", "itemIndex", "eventIds"],
    },
}

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "itemId": {"type": "string"},
            "disposition": {"type": "string", "enum": ["retain", "resolved"]},
            "reason": {"type": "string"},
            "eventIds": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["itemId", "disposition", "reason", "eventIds"],
    },
}


def public_note_data(value: dict[str, Any]) -> dict[str, Any]:
    """Keep inference-only pending evidence out of the published note contract."""
    return {key: item for key, item in value.items() if key != "pendingStateItems"}


def validate_pending_state_items(value: dict[str, Any], allowed_ids: set[str] | None = None) -> None:
    records = value.get("pendingStateItems", [])
    validate_summary_output_schema(records, PENDING_STATE_SCHEMA, path="$.pendingStateItems")
    state = value["lastKnownState"]
    expected = {(kind, index) for kind in ("unresolved", "unverified") for index in range(len(state[kind]))}
    observed = [(record["kind"], record["itemIndex"]) for record in records]
    if len(observed) != len(set(observed)) or set(observed) != expected:
        raise ValueError(
            "pendingStateItems must cite each unresolved/unverified item exactly once by kind and itemIndex"
        )
    for record in records:
        cited = record["eventIds"]
        if len(cited) != len(set(cited)):
            raise ValueError("pendingStateItems must not repeat source event IDs")
        if allowed_ids is not None and set(cited) - allowed_ids:
            raise ValueError("pendingStateItems cites unknown source events")


def collect_state_items(partials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for part in partials:
        validate_pending_state_items(part)
        state = part["lastKnownState"]
        sources = {(record["kind"], record["itemIndex"]): record["eventIds"]
                   for record in part.get("pendingStateItems", [])}
        for kind in ("unresolved", "unverified"):
            for index, text in enumerate(state[kind]):
                origins = sources[kind, index]
                # Retain distinct histories/contexts even when their wording is identical.
                identity = (kind, text, frozenset(origins))
                if any((i["kind"], i["text"], frozenset(i["eventIds"])) == identity for i in items):
                    continue
                items.append(
                    {
                        "itemId": f"S{len(items) + 1:05d}",
                        "kind": kind,
                        "text": text,
                        "eventIds": list(origins),
                    }
                )
    return items


def reconcile_state(
    value: dict[str, Any],
    items: list[dict[str, Any]],
    events: Sequence[ChatEvent],
) -> dict[str, Any]:
    result = deepcopy(value)
    reviews = result.pop("stateItemReviews", [])
    if not items:
        return result
    by_id = {item["itemId"]: item for item in items}
    reviewed = [review.get("itemId") for review in reviews]
    if len(reviewed) != len(set(reviewed)) or set(reviewed) != set(by_id):
        raise ValueError("stateItemReviews must account for every pending item exactly once")
    order = {event.id: index for index, event in enumerate(events)}
    branches = {event.id: event.branch_id for event in events}
    state = result["lastKnownState"]
    retained_requests: list[str] = []
    resolved_items: list[dict[str, Any]] = []
    retained_texts: set[tuple[str, str]] = set()
    for review in reviews:
        item = by_id[review["itemId"]]
        evidence = review["eventIds"]
        if not review["reason"].strip():
            raise ValueError("state review requires a reason")
        if set(evidence) - set(order):
            raise ValueError("state review cites unknown source events")
        if review["disposition"] == "resolved":
            origins = item["eventIds"]
            if not evidence or not origins or set(origins) - set(order):
                raise ValueError("resolving a pending item requires known origin and later source evidence")
            origin_branches = {branches[e] for e in origins}
            if {branches[e] for e in evidence} != origin_branches:
                raise ValueError("state resolution cannot cancel a different history")
            if any(
                not any(
                    order[e] > max(order[o] for o in origins if branches[o] == branch)
                    for e in evidence
                    if branches[e] == branch
                )
                for branch in origin_branches
            ):
                raise ValueError(
                    f"state item {item['itemId']} resolution must cite evidence after the pending item "
                    f"({', '.join(origins)}); supplied: {', '.join(evidence)}. "
                    "Retain this item when later proof is unavailable."
                )
            resolved_items.append(item)
            for event_id in evidence:
                if event_id not in state["eventIds"]:
                    state["eventIds"].append(event_id)
            continue
        if review["disposition"] != "retain":
            raise ValueError("invalid state disposition")
        retained_texts.add((item["kind"], item["text"]))
        if item["kind"] == "unresolved":
            retained_requests.append(item["itemId"])
        # The model cannot silently drop or rewrite the unresolved/unverified text.
        if item["text"] not in state[item["kind"]]:
            state[item["kind"]].append(item["text"])
        for event_id in item["eventIds"]:
            if event_id not in state["eventIds"]:
                state["eventIds"].append(event_id)
    for item in resolved_items:
        if (item["kind"], item["text"]) not in retained_texts and item["text"] in state[item["kind"]]:
            raise ValueError(
                f"state item {item['itemId']} is resolved but still present in lastKnownState.{item['kind']}; "
                "make the review and final pending list consistent with the evidence"
            )
    if state["workState"] == "done" and retained_requests:
        raise ValueError(
            f"retained unresolved state items {', '.join(retained_requests)} "
            "conflict with lastKnownState.workState=done; "
            "choose the supported unfinished state, or resolve these items with later evidence. "
            "Do not discard retained requests to make the output pass."
        )
    return result
