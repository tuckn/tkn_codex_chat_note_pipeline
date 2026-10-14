"""Lossless, request-local identifiers for source audit transport."""
from __future__ import annotations

from typing import Any

_REFERENCE_LISTS = {"evidenceIds", "availableEvidenceIds", "supports"}


def evidence_aliases(payload: dict[str, Any]) -> dict[str, str]:
    ids: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in _REFERENCE_LISTS and isinstance(child, list):
                    ids.update(x for x in child if isinstance(x, str))
                elif key == "evidence" and isinstance(child, list):
                    ids.update(e["id"] for e in child if isinstance(e, dict) and "id" in e)
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(payload)
    return {id: f"e{n}" for n, id in enumerate(sorted(ids), 1)}


def map_evidence_ids(value: Any, mapping: dict[str, str]) -> Any:
    """Map references only, never prose, item IDs, or source provenance fields."""
    if isinstance(value, list):
        return [map_evidence_ids(child, mapping) for child in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, child in value.items():
        if key in _REFERENCE_LISTS and isinstance(child, list):
            result[key] = [mapping.get(id, id) if isinstance(id, str) else id for id in child]
        elif key == "evidence" and isinstance(child, list):
            result[key] = [{**map_evidence_ids(e, mapping), "id": mapping.get(e["id"], e["id"])}
                           for e in child]
        else:
            result[key] = map_evidence_ids(child, mapping)
    return result
