"""Require an explicit, source-backed disposition for each partial pending state item."""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from typing import Any

from .chat_logs import ChatEvent

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


def collect_state_items(partials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for part in partials:
        state = part["lastKnownState"]
        for kind in ("unresolved", "unverified"):
            for text in state[kind]:
                # Retain distinct histories/contexts even when their wording is identical.
                identity = (kind, text, tuple(state["eventIds"]))
                if any((i["kind"], i["text"], tuple(i["eventIds"])) == identity for i in items):
                    continue
                items.append(
                    {
                        "itemId": f"S{len(items) + 1:05d}",
                        "kind": kind,
                        "text": text,
                        "eventIds": list(state["eventIds"]),
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
                    f"state item {item['itemId']} resolution must cite evidence after the partial state "
                    f"({', '.join(origins)}); supplied: {', '.join(evidence)}. "
                    "Retain this item when later proof is unavailable."
                )
            continue
        if review["disposition"] != "retain":
            raise ValueError("invalid state disposition")
        # The model cannot silently drop or rewrite the unresolved/unverified text.
        if item["text"] not in state[item["kind"]]:
            state[item["kind"]].append(item["text"])
        for event_id in item["eventIds"]:
            if event_id not in state["eventIds"]:
                state["eventIds"].append(event_id)
    return result
