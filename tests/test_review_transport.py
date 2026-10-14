from __future__ import annotations

import json
from datetime import date
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from test_review_quality import evidence
from test_reviews import config_for, item

from tkn_codex_chat_note.review_generation import ReviewGenerator
from tkn_codex_chat_note.review_model import period_for
from tkn_codex_chat_note.review_quality import EvidenceResult, _audit_batches, _check_evidence, _payload
from tkn_codex_chat_note.review_transport import evidence_aliases, map_evidence_ids


def test_alias_roundtrip_preserves_prose_metadata_and_unit_ids() -> None:
    source = evidence("long-source-id", "long-source-id is literal prose", 0)
    source["supports"] = ["support-id"]
    value = {"auditUnits": [{"id": "long-source-id", "item": item("long-source-id"),
                             "evidenceIds": ["long-source-id"]}], "evidence": [source],
             "verifiedItems": [item("support-id")], "availableEvidenceIds": ["long-source-id"]}
    aliases = evidence_aliases(value)
    wire = map_evidence_ids(value, aliases)
    assert wire["auditUnits"][0]["id"] == "long-source-id"
    assert wire["evidence"][0]["text"] == source["text"]
    assert wire["evidence"][0]["timestamps"] == source["timestamps"]
    assert wire["evidence"][0]["id"] != source["id"]
    assert map_evidence_ids(wire, {v: k for k, v in aliases.items()}) == value


def test_packing_unites_separated_sources_without_dropping_corrections() -> None:
    units = []
    for n, topic in enumerate(["a", "b", "a", "b"]):
        sources = [evidence(topic + "-request", topic * 700, 0, "user"),
                   evidence(topic + "-completion", "Completed " + topic * 700, 1),
                   evidence(topic + "-correction", "Do not automate " + topic * 700, 2, "user")]
        units.append({"id": f"D{n}", "item": item(sources[0]["id"]), "restoreFindings": [],
                      "relatedFindings": [item(sources[-1]["id"])], "evidence": sources})
    packed = _audit_batches(units, 4200)
    assert [[u["id"] for u in p["auditUnits"]] for p in packed] == [["D0", "D2"], ["D1", "D3"]]
    for p in packed:
        assert len(json.dumps(map_evidence_ids(p, evidence_aliases(p)), ensure_ascii=False)) <= 4200
        actual = {e["id"]: e for e in p["evidence"]}
        for u in p["auditUnits"]:
            original = next(x for x in units if x["id"] == u["id"])
            assert u["evidenceIds"] == [e["id"] for e in original["evidence"]]
            assert all(actual[e["id"]] == e for e in original["evidence"])
    assert _audit_batches(units, 4200) == packed
    with pytest.raises(Exception, match="exceeds"):
        _audit_batches(units, 200)


def test_alias_cache_identity_and_repair_preserve_canonical_citations(tmp_path: Path) -> None:
    calls = []

    def generate(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        calls.append(prompt)
        data = json.loads(prompt.split("BEGIN_REVIEW_INPUT_JSON\n")[1].split("\nEND_REVIEW_INPUT_JSON")[0])
        wire = data.get("originalInput", data)
        id = wire["auditUnits"][0]["item"]["evidenceIds"][0]
        assert id == "e1"
        return {"decisions": [{"itemId": "D0", "action": "replace", "reason": "Supported",
                               "replacements": [item(id, text="x" * 400 if len(calls) == 1 else "Completed")]}]}

    g = ReviewGenerator(config_for(tmp_path), "alias-cache", generate=generate)
    period = period_for("weekly", date(2026, 5, 19))
    for id in ["canonical-original", "canonical-changed", "canonical-changed"]:
        unit = {"id": "D0", "item": item(id), "restoreFindings": [],
                "evidence": [evidence(id, "Same source text and metadata", 0)],
                "replacementBudget": {"maxItems": 1, "maxProseCharacters": 250}}
        result = g._quality_call("evidence-check", period, _payload([unit], {}, "auditUnits"), EvidenceResult,
                                 partial(_check_evidence, units={"D0": unit}))
        assert result.decisions[0].replacements[0].evidenceIds == [id]
    assert len(calls) == 3 and g.cache_hits == 1


def test_alias_does_not_expand_unit_citation_permissions(tmp_path: Path) -> None:
    def generate(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        return {"decisions": [
            {"itemId": "D0", "action": "replace", "reason": "Invalid cross-unit citation",
             "replacements": [item("e2")]},
            {"itemId": "D1", "action": "keep", "reason": "Supported", "replacements": []}]}

    units = [{"id": f"D{n}", "item": item(id), "restoreFindings": [],
              "evidence": [evidence(id, "Source", n)]} for n, id in enumerate(["a", "b"])]
    g = ReviewGenerator(config_for(tmp_path), "alias-permissions", generate=generate)
    with pytest.raises(ValueError, match="unavailable"):
        g._quality_call("evidence-check", period_for("weekly", date(2026, 5, 19)),
                        _payload(units, {}, "auditUnits"), EvidenceResult,
                        partial(_check_evidence, units={u["id"]: u for u in units}))
    assert g.calls == 2
