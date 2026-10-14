"""Bounded coverage reconciliation and source-backed editorial verification."""
from __future__ import annotations

import json
from collections import defaultdict
from functools import partial
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .review_evidence import EvidenceSet, digest
from .review_model import Period, ReviewDraft, ReviewItem
from .review_transport import evidence_aliases, map_evidence_ids
from .runtime import PipelineError

if TYPE_CHECKING:
    from .review_generation import ReviewGenerator

QUALITY_VERSION = "3"
EVIDENCE_CONTEXT_VERSION = "6"
EXCERPT_LIMIT = 2400
NEIGHBORS = 2


def prose_characters(items: list[ReviewItem]) -> int:
    return sum(len(getattr(i, key)) for i in items for key in ("title", "text", "caveat", "nextCheck"))


def editorial_limits(language: str, *, final: bool = False) -> dict[str, int]:
    return {"maxItems": 36 if final else 32,
            "maxProseCharacters": (8400 if final else 7000) if language == "ja" else (24000 if final else 20000)}


def check_editorial(draft: ReviewDraft, language: str, *, final: bool = False) -> None:
    limits = editorial_limits(language, final=final)
    if len(draft.items) > limits["maxItems"] or prose_characters(draft.items) > limits["maxProseCharacters"]:
        raise ValueError(f"review exceeds editorial limits {limits}; merge or defer lower-priority detail, "
                         "preserving material corrections, distinct questions and source citations")


class CoverageDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    findingId: str
    action: Literal["covered", "restore", "omit"]
    targetId: str  # empty when a distinct new row is needed or omitted
    reason: str = Field(min_length=1)


class CoverageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decisions: list[CoverageDecision]

    def check(self, findings: set[str], targets: set[str]) -> None:
        ids = [d.findingId for d in self.decisions]
        if len(ids) != len(set(ids)) or set(ids) != findings:
            raise ValueError("coverage must account for every supplied finding exactly once")
        for d in self.decisions:
            if d.targetId and d.targetId not in targets:
                raise ValueError("coverage target is unavailable")
            if d.action == "covered" and not d.targetId:
                raise ValueError("covered findings require a target")
            if d.action == "omit" and d.targetId:
                raise ValueError("omitted findings cannot have a target")


class EvidenceDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    itemId: str
    action: Literal["keep", "replace", "omit"]
    replacements: list[ReviewItem]
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def replacement_contract(self) -> EvidenceDecision:
        if (self.action == "replace") != bool(self.replacements):
            raise ValueError("only replace decisions require nonempty replacements")
        return self


class EvidenceBudgetError(ValueError):
    def __init__(self, violations: dict[str, str]) -> None:
        self.item_ids = set(violations)
        super().__init__("replacement exceeds its editorial budget: " + "; ".join(violations.values())
                         + ". Shorten only these items, aiming at 80 percent of each character limit.")


class EvidenceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decisions: list[EvidenceDecision]

    def check(self, units: dict[str, dict[str, Any]]) -> None:
        ids = [d.itemId for d in self.decisions]
        if len(ids) != len(set(ids)) or set(ids) != set(units):
            raise ValueError("evidence audit must account for every supplied item exactly once")
        violations = {}
        for d in self.decisions:
            budget = units[d.itemId].get("replacementBudget")
            outputs = (d.replacements if d.action == "replace" else
                       [ReviewItem.model_validate(units[d.itemId]["item"])]
                       if d.action == "keep" and "item" in units[d.itemId] else [])
            count = prose_characters(outputs)
            if budget and (len(outputs) > budget["maxItems"] or count > budget["maxProseCharacters"]):
                violations[d.itemId] = (f"{d.itemId}: {len(outputs)} rows / {count} characters; "
                                        f"limit {budget['maxItems']} rows / {budget['maxProseCharacters']} characters")
        if violations:
            raise EvidenceBudgetError(violations)
        for d in self.decisions:
            unit = units[d.itemId]
            if d.action == "keep" and (unit["restoreFindings"] or unit.get("requireReplacement")):
                raise ValueError("restored findings must be incorporated explicitly, not silently kept")
            allowed = {e["id"] for e in unit["evidence"]}
            outputs = d.replacements if d.action == "replace" else (
                [ReviewItem.model_validate(unit["item"])] if d.action == "keep" and "item" in unit else [])
            for item in outputs:
                ReviewDraft(items=[item], rawRequests=[]).validate_sources(allowed)


def _check_coverage(result: CoverageResult, *, findings: set[str], targets: set[str],
                    restores: int = 6, new_topics: int = 2) -> None:
    result.check(findings, targets)
    restored = [d for d in result.decisions if d.action == "restore"]
    if len(restored) > restores or sum(not d.targetId for d in restored) > new_topics:
        raise ValueError("coverage exceeds restoration budget; select consequential omissions only")


def _check_evidence(result: EvidenceResult, *, units: dict[str, dict[str, Any]], max_splits: int | None = None) -> None:
    result.check(units)
    if max_splits is not None and sum(max(0, len(d.replacements) - 1) for d in result.decisions) > max_splits:
        raise ValueError("audit exceeds shared split budget; keep related details in one row")


def _payload(records: list[dict[str, Any]], base: dict[str, Any], key: str) -> dict[str, Any]:
    if key != "auditUnits":
        return {**base, key: records}
    sources = {e["id"]: e for u in records for e in u["evidence"]}
    # A shared excerpt must have one meaning within a request: prefer the fullest available text.
    for unit in records:
        for e in unit["evidence"]:
            if len(e["text"]) > len(sources[e["id"]]["text"]):
                sources[e["id"]] = e
    return {**base, key: [{**{k: v for k, v in u.items() if k != "evidence"},
                          "evidenceIds": [e["id"] for e in u["evidence"]]} for u in records],
            "evidence": list(sources.values())}


def _batches(records: list[dict[str, Any]], base: dict[str, Any], ceiling: int,
             key: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    for record in records:
        candidate = _payload([*current, record], base, key)
        if len(json.dumps(candidate, ensure_ascii=False)) > ceiling:
            if not current:
                raise PipelineError("one editorial audit unit exceeds reviews.input_characters")
            result.append(_payload(current, base, key))
            current = [record]
            if len(json.dumps(_payload(current, base, key), ensure_ascii=False)) > ceiling:
                raise PipelineError("one editorial audit unit exceeds reviews.input_characters")
        else:
            current.append(record)
    if current:
        result.append(_payload(current, base, key))
    return result


def _audit_batches(units: list[dict[str, Any]], ceiling: int) -> list[dict[str, Any]]:
    """Co-locate overlapping sources without dropping any unit or changing its citation scope."""
    if not units:
        return []

    def payload(indices: tuple[int, ...]) -> dict[str, Any]:
        return _payload([units[n] for n in indices], {}, "auditUnits")

    sizes: dict[tuple[int, ...], int] = {}

    def size(indices: tuple[int, ...]) -> int:
        if indices not in sizes:
            value = payload(indices)
            sizes[indices] = len(json.dumps(map_evidence_ids(value, evidence_aliases(value)), ensure_ascii=False))
        return sizes[indices]

    sequential: list[tuple[int, ...]] = []
    current: tuple[int, ...] = ()
    for n in range(len(units)):
        if size((n,)) > ceiling:
            raise PipelineError("one editorial audit unit exceeds reviews.input_characters")
        if current and size((*current, n)) > ceiling:
            sequential.append(current)
            current = ()
        current = (*current, n)
    if current:
        sequential.append(current)
    groups: list[tuple[int, ...]] = [(n,) for n in range(len(units))]
    while True:
        candidates = []
        for i, left in enumerate(groups):
            for j in range(i + 1, len(groups)):
                merged = tuple(sorted((*left, *groups[j])))
                length = size(merged)
                if length <= ceiling:
                    saving = size(left) + size(groups[j]) - length
                    candidates.append((saving, length, -i, -j, merged))
        if not candidates:
            break
        _, _, neg_i, neg_j, merged = max(candidates)
        groups[-neg_i] = merged
        groups.pop(-neg_j)
    groups.sort()
    # Avoid a greedy packing regression on unrelated or awkwardly sized records.
    if len(groups) > len(sequential) or sum(map(size, groups)) > sum(map(size, sequential)):
        groups = sequential
    return [payload(indices) for indices in groups]


def _source_transmission(batches: list[dict[str, Any]]) -> dict[str, int]:
    sources = [e for batch in batches for e in batch["evidence"]]
    fullest: dict[str, int] = {}
    for e in sources:
        fullest[e["id"]] = max(fullest.get(e["id"], 0), len(e["text"]))
    sent = sum(len(e["text"]) for e in sources)
    return {"batches": len(batches), "sourceOccurrences": len(sources), "uniqueSources": len(fullest),
            "sourceTextCharacters": sent, "repeatedSourceTextCharacters": sent - sum(fullest.values())}


def coverage_plan(draft: ReviewDraft, findings: list[ReviewItem], ceiling: int) -> list[dict[str, Any]]:
    # Only byte-equivalent structured findings are already accounted for. Shared citations
    # do not establish semantic coverage: one record can contain a decision and a correction.
    exact = {digest(i.model_dump()) for i in draft.items if i.category not in {"overview", "insights"}}
    candidates = list({digest(i.model_dump()): i for i in findings if i.category != "overview"
                       and digest(i.model_dump()) not in exact}.values())
    outlines = [{"id": f"D{n}", "category": i.category, "title": i.title, "text": i.text,
                 "caveat": i.caveat, "nextCheck": i.nextCheck} for n, i in enumerate(draft.items)
                if i.category not in {"overview", "insights"}]
    records = [{"id": "F" + digest(i.model_dump())[:16], "item": i.model_dump()} for i in candidates]
    return _batches(records, {"draftItems": outlines}, ceiling, "findings")


def _snippet(e: dict[str, Any], limit: int = EXCERPT_LIMIT) -> dict[str, Any]:
    text = e["text"]
    truncated = len(text) > limit
    if truncated:
        text = text[:limit // 2] + "\n[... omitted middle ...]\n" + text[-limit // 2:]
    return {**{k: e[k] for k in ("id", "actor", "basis", "kind", "threadId", "timestamps", "supports") if k in e},
            "text": text, "truncated": truncated, "originalCharacters": len(e["text"])}


def audit_units(draft: ReviewDraft, evidence: EvidenceSet,
                restored: dict[str, list[dict[str, Any]]], ceiling: int = 44000,
                related: dict[str, list[dict[str, Any]]] | None = None) -> list[dict[str, Any]]:
    by_thread: dict[str, list[str]] = defaultdict(list)
    for id, e in evidence.evidence.items():
        if e.get("threadId") and e.get("actor") in {"user", "assistant"}:
            by_thread[e["threadId"]].append(id)
    for ids in by_thread.values():
        ids.sort(key=lambda id: (evidence.evidence[id]["timestamps"][-1], id))
    rows = [(f"D{n}", i.model_dump()) for n, i in enumerate(draft.items)]
    rows += [(id, values[0]) for id, values in restored.items() if id.startswith("F")]
    units = []
    for id, item in rows:
        extra = restored.get(id, [])
        connected = (related or {}).get(id, [])
        required = list(dict.fromkeys(e for i in [item, *extra, *connected] for e in i["evidenceIds"]))
        selected = list(required)
        core = set(required)
        # Include nearby dialogue so a request can be distinguished from a later completion report.
        for source_id in required:
            e = evidence.evidence[source_id]
            supports = [x for x in e.get("supports", []) if x in evidence.evidence]
            selected.extend(supports)
            core.update(supports)
            neighbors = by_thread.get(e.get("threadId", ""), [])
            if source_id in neighbors:
                at = neighbors.index(source_id)
                selected.extend(neighbors[max(0, at - 1):at + NEIGHBORS + 1])
                # Progress messages can separate a request from its actual completion.
                # Include the turn's final reply and the next user correction/question.
                following = neighbors[at + 1:]
                boundary = next((n for n, x in enumerate(following)
                                 if evidence.evidence[x]["actor"] == "user"), len(following))
                replies = [x for x in following[:boundary] if evidence.evidence[x]["actor"] == "assistant"]
                if replies:
                    selected.append(replies[-1])
                if boundary < len(following):
                    selected.append(following[boundary])
                # The last saved reply can supersede an earlier retry/error in the same chat.
                terminal = next((x for x in reversed(neighbors)
                                 if evidence.evidence[x]["actor"] == "assistant"), None)
                if terminal:
                    selected.append(terminal)
        selected = list(dict.fromkeys(selected))
        unit: dict[str, Any] = {"id": id, "item": item, "restoreFindings": extra, "relatedFindings": connected,
                "evidence": [_snippet(evidence.evidence[e]) for e in selected]}
        limit = EXCERPT_LIMIT
        while len(json.dumps(_payload([unit], {}, "auditUnits"), ensure_ascii=False)) > ceiling and limit > 600:
            limit //= 2
            unit["evidence"] = [_snippet(evidence.evidence[e], limit) for e in selected]
        optional = [e for e in selected if e not in core]
        omitted = 0
        while len(json.dumps(_payload([unit], {}, "auditUnits"), ensure_ascii=False)) > ceiling and optional:
            selected.remove(optional.pop())
            omitted += 1
            unit["nearbyEvidenceOmitted"] = omitted
            unit["evidence"] = [_snippet(evidence.evidence[e], limit) for e in selected]
        if len(json.dumps(_payload([unit], {}, "auditUnits"), ensure_ascii=False)) > ceiling:
            unit["evidence"] = [_snippet(evidence.evidence[e], 300) for e in selected]
        units.append(unit)
    return units


def allocate_editorial_budgets(units: list[dict[str, Any]], language: str) -> dict[str, int]:
    """Allocate a fixed aggregate budget; keep and replace consume the same allocation."""
    limit = editorial_limits(language, final=True)["maxProseCharacters"]
    ceiling = limit * 97 // 100  # Leave small editorial headroom without relying on it for validity.
    sizes = [prose_characters([ReviewItem.model_validate(u["item"])]) for u in units]
    scale = 1 if language == "ja" else 3
    desired = [size + scale * (80 + 100 * min(2, len(u["restoreFindings"])))
               for size, u in zip(sizes, units, strict=True)]
    if sum(desired) <= ceiling:
        budgets = desired
    elif sum(sizes) <= ceiling:
        remaining = ceiling - sum(sizes)
        growth = [want - size for want, size in zip(desired, sizes, strict=True)]
        budgets = [size + remaining * more // sum(growth) for size, more in zip(sizes, growth, strict=True)]
    else:
        budgets = [ceiling * size // sum(sizes) for size in sizes]
    for unit, budget in zip(units, budgets, strict=True):
        if budget < 1:
            raise PipelineError("too many audit units for the editorial budget")
        unit["replacementBudget"] = {
            "maxItems": 1 if unit["id"].startswith("F") or unit["item"]["category"] == "overview" else 2,
            "maxProseCharacters": budget,
        }
    return {"maxProseCharacters": limit, "allocatedProseCharacters": sum(budgets),
            "unallocatedProseCharacters": limit - sum(budgets)}


def _check_overview(result: EvidenceResult, *, units: dict[str, dict[str, Any]]) -> None:
    result.check(units)
    if any(i.category != "overview" for d in result.decisions for i in d.replacements):
        raise ValueError("overview check can only return overview items")


def _audit_overviews(generator: ReviewGenerator, period: Period, units: list[dict[str, Any]],
                     body: list[ReviewItem]) -> list[EvidenceDecision]:
    """Check summaries last, using immutable source-audited body statements and their citations."""
    allowed = sorted({id for item in body for id in item.evidenceIds})
    results: list[EvidenceDecision] = []
    for original in units:
        unit = {**original, "restoreFindings": [], "requireReplacement": True,
                "evidence": [{"id": id} for id in allowed]}
        payload = {"auditUnits": [{k: v for k, v in unit.items() if k not in {"evidence", "relatedFindings"}}],
                   "verifiedItems": [i.model_dump() for i in body], "availableEvidenceIds": allowed,
                   "instruction": "Rewrite only the overview from the source-audited body. "
                                  "Do not change the body or add new facts. Preserve reported/inferred status."}
        checked = generator._quality_call("overview-check", period, payload, EvidenceResult,
                                          partial(_check_overview, units={unit["id"]: unit}))
        results.extend(checked.decisions)
    return results


def audit_review(generator: ReviewGenerator, period: Period, draft: ReviewDraft,
                 findings: list[ReviewItem], evidence: EvidenceSet) -> ReviewDraft:
    ceiling = generator.settings.input_characters - len(generator.quality_policy) - 1800
    target_ids = {f"D{n}" for n, i in enumerate(draft.items) if i.category not in {"overview", "insights"}}
    restored: dict[str, list[dict[str, Any]]] = defaultdict(list)
    related: dict[str, list[dict[str, Any]]] = defaultdict(list)
    coverage: list[dict[str, Any]] = []
    considered: list[dict[str, Any]] = []
    remaining_restores, remaining_topics = 6, 2
    for payload in coverage_plan(draft, findings, ceiling):
        payload["restorationBudget"] = {"maxRestores": remaining_restores, "maxNewTopics": remaining_topics}
        considered.extend(payload["findings"])
        ids = {r["id"] for r in payload["findings"]}
        result = generator._quality_call("coverage-check", period, payload, CoverageResult,
                                         partial(_check_coverage, findings=ids, targets=target_ids,
                                                 restores=remaining_restores, new_topics=remaining_topics))
        coverage.extend(d.model_dump() for d in result.decisions)
        originals = {r["id"]: r["item"] for r in payload["findings"]}
        for decision in result.decisions:
            if decision.action == "covered":
                related[decision.targetId].append(originals[decision.findingId])
            if decision.action == "restore":
                remaining_restores -= 1
                remaining_topics -= int(not decision.targetId)
                restored[decision.targetId or decision.findingId].append(originals[decision.findingId])
    units = audit_units(draft, evidence, restored, ceiling, related)
    budgets = allocate_editorial_budgets(units, generator.settings.language)
    overviews = [u for u in units if u["item"]["category"] == "overview"]
    body_units = [u for u in units if u["item"]["category"] != "overview"]
    decisions: dict[str, EvidenceDecision] = {}
    input_chars = 0
    evidence_batches = _audit_batches(body_units, ceiling)
    sequential_batches = _batches(body_units, {}, ceiling, "auditUnits")
    remaining_splits = max(0, editorial_limits(generator.settings.language, final=True)["maxItems"] - len(units))
    for payload in evidence_batches:
        payload["splitBudget"] = {"maxAdditionalRows": remaining_splits,
                                  "instruction": "Across this batch, sum(max(0, replacement count - 1)) must not "
                                                 "exceed maxAdditionalRows. Split unrelated topics only; keep "
                                                 "an activity and its remaining status together in one row."}
        supplied_ids = {u["id"] for u in payload["auditUnits"]}
        supplied = {u["id"]: u for u in units if u["id"] in supplied_ids}
        checked = generator._quality_call("evidence-check", period, payload, EvidenceResult,
                                          partial(_check_evidence, units=supplied, max_splits=remaining_splits))
        remaining_splits -= sum(max(0, len(d.replacements) - 1) for d in checked.decisions)
        decisions.update({d.itemId: d for d in checked.decisions})
        input_chars += len(json.dumps(payload, ensure_ascii=False))
    output: list[ReviewItem] = []
    for unit in body_units:
        verdict = decisions[unit["id"]]
        if verdict.action == "keep":
            output.append(ReviewItem.model_validate(unit["item"]))
        elif verdict.action == "replace":
            output.extend(verdict.replacements)
    if overviews:
        overview_decisions = _audit_overviews(generator, period, overviews, output)
        decisions.update({d.itemId: d for d in overview_decisions})
        output = [i for d in overview_decisions for i in d.replacements] + output
    if not output and draft.items:
        raise PipelineError("editorial verification removed every item; no review published")
    # Exact duplicates can arise when multiple findings restore one semantic theme.
    output = list({digest(i.model_dump()): i for i in output}.values())
    final = ReviewDraft(items=output, rawRequests=[])
    final.validate_sources(set(evidence.evidence))
    check_editorial(final, generator.settings.language, final=True)
    generator.quality_report = {
        "version": QUALITY_VERSION, "status": "completed", "method": "model-assisted-source-audit",
        "scope": "bounded excerpts; model checks are not a guarantee of semantic correctness",
        "evidenceContextVersion": EVIDENCE_CONTEXT_VERSION,
        "coverageScreen": "all distinct findings except exact structured matches outside overview/insights",
        "relatedFindingCount": sum(len(v) for v in related.values()),
        "overviewChecks": len(overviews), "overviewBasis": "source-audited body; no independent new facts",
        "editorialAllocation": budgets,
        "unitBudgets": {u["id"]: u["replacementBudget"] for u in units},
        "inputItemCount": len(draft.items), "outputItemCount": len(output),
        "evidenceBatches": len(evidence_batches), "evidenceInputCharacters": input_chars,
        "sourceTransmission": {"sequential": _source_transmission(sequential_batches),
                               "packed": _source_transmission(evidence_batches)},
        "evidenceTransport": "request-local aliases; canonical citations restored before validation and storage",
        "nearbyEvidenceOmitted": sum(u.get("nearbyEvidenceOmitted", 0) for u in units),
        "truncatedExcerpts": sum(e["truncated"] for u in units for e in u["evidence"]),
        "editorialLimits": editorial_limits(generator.settings.language, final=True),
        "proseCharacters": prose_characters(final.items),
        "consideredFindings": considered,
        "coverageDecisions": coverage, "evidenceDecisions": [d.model_dump() for d in decisions.values()],
    }
    generator.journal.finish("generated")
    return final
