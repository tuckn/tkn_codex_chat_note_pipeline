"""Bounded adjacent-period context, separate from current-period event evidence."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from .config import AppConfig
from .review_evidence import EvidenceSet, checked_json, digest, review_path
from .review_model import Period, ReviewDraft, ReviewItem, period_for
from .runtime import PipelineError

COMPARISON_VERSION = "1"
MAX_ITEMS = 6
MAX_CHARACTERS = 6000
_CATEGORIES = {"decisions", "unresolved", "activities", "learning", "ai-improvement",
               "intelligence-candidate", "reusable-resource"}
_STOP = {"the", "and", "for", "with", "from", "this", "that", "user", "review", "codex",
         "した", "する", "して", "こと", "ため", "確認", "今回", "検討", "内容", "について", "ました"}


def previous_period(period: Period) -> Period:
    return period_for(period.kind, period.start - timedelta(days=1), period.offset)


def _terms(text: str) -> set[str]:
    result = set(re.findall(r"[a-z][a-z0-9_-]{2,}", text.lower()))
    for word in re.findall(r"[一-龥ァ-ヶー]{2,}", text):
        result.update(word[n:n+2] for n in range(len(word)-1))
    return result - _STOP


@dataclass
class PreviousContext:
    metadata: dict[str, Any]
    candidates: list[dict[str, Any]] = field(default_factory=list)
    selected: list[dict[str, Any]] = field(default_factory=list)

    def select(self, findings: list[ReviewItem], evidence: EvidenceSet, character_limit: int) -> None:
        """Lexical/thread overlap proposes relevance; synthesis still decides whether to use it."""
        queries = [(_terms(i.title + " " + i.text),
                    {evidence.evidence[id].get("threadId") for id in i.evidenceIds
                     if evidence.evidence[id].get("threadId")}) for i in findings]
        ranked = []
        for candidate in self.candidates:
            terms = _terms(candidate["item"]["title"] + " " + candidate["item"]["text"])
            title_terms = _terms(candidate["item"]["title"])
            score = max((len(terms & words) + 2 * len(title_terms & words)
                         + 20 * bool(threads & set(candidate["threadIds"]))
                         for words, threads in queries), default=0)
            if score >= 4:
                ranked.append((score, candidate))
        self.selected = []
        size = 2
        for score, candidate in sorted(ranked, key=lambda pair: (-pair[0], pair[1]["id"])):
            record = {**candidate, "relevanceScore": score}
            amount = len(json.dumps(record, ensure_ascii=False)) + 2
            # Keep complete items (including caveats), never truncate a historical claim.
            if size + amount <= min(MAX_CHARACTERS, character_limit):
                self.selected.append(record)
                size += amount
            if len(self.selected) == MAX_ITEMS:
                break

    def document(self) -> dict[str, Any]:
        return {**self.metadata, "selection": "thread-or-topic-overlap", "maxItems": MAX_ITEMS,
                "maxCharacters": MAX_CHARACTERS, "selectedCount": len(self.selected),
                "evidence": self.selected}


def load_previous(config: AppConfig, period: Period) -> PreviousContext:
    prior = previous_period(period)
    path = review_path(config.review_data_root, prior)
    meta: dict[str, Any] = {"version": COMPARISON_VERSION, "period": prior.document(),
                            "status": "disabled" if not config.reviews.previous_period else "missing"}
    result = PreviousContext(meta)
    if not config.reviews.previous_period or not path.is_file():
        return result
    try:
        if path.is_symlink() or not path.resolve().is_relative_to(config.review_data_root.resolve()):
            raise PipelineError("unsafe previous review path")
        # Validate its own period and citations; never reinterpret it as current evidence.
        from .reviews import validate_review
        validate_review(path)
        saved = checked_json(path)
        if saved["period"] != prior.document() or saved["sourceId"] != config.source_id:
            raise ValueError("previous review belongs to a different source or period")
        from hashlib import sha256
        meta.update(status="available", notePath=str(path), sha256=sha256(path.read_bytes()).hexdigest())
        refs = {e["id"]: e for e in saved["evidence"]}
        for item in saved["items"]:
            # Do not recursively import older comparisons or proposal-only next-action lists.
            if item["category"] not in _CATEGORIES or set(item["evidenceIds"]) - refs.keys():
                continue
            times = sorted({t for id in item["evidenceIds"] for t in refs[id]["timestamps"]})
            threads = sorted({refs[id]["threadId"] for id in item["evidenceIds"] if refs[id].get("threadId")})
            result.candidates.append({"id": "P" + digest([prior.document(), item])[:24],
                "kind": "previous-review", "scope": "comparison-only", "period": prior.document(),
                "title": prior.key + " / " + item["title"], "text": item["title"] + ": " + item["text"],
                "basis": item["basis"], "caveat": item["caveat"], "nextCheck": item["nextCheck"],
                "item": item, "timestamps": times, "threadIds": threads, "notePath": str(path),
                "sourceSha256": meta["sha256"]})
    except (OSError, ValueError, KeyError, PipelineError) as exc:
        meta.update(status="unavailable", reason=type(exc).__name__)
        result.candidates.clear()
    return result


def validate_comparison(value: dict[str, Any], period: Period) -> dict[str, dict[str, Any]]:
    comparison = value.get("comparison")
    if comparison is None:
        return {}
    prior = previous_period(period)
    if comparison["period"] != prior.document():
        raise ValueError("comparison must use the immediately preceding period")
    rows = comparison["evidence"]
    if len(rows) > MAX_ITEMS or comparison["selectedCount"] != len(rows):
        raise ValueError("invalid comparison selection count")
    if rows and comparison["status"] != "available":
        raise ValueError("unavailable comparison cannot provide evidence")
    refs = {e["id"]: e for e in rows}
    if len(refs) != len(rows) or set(refs) & {e["id"] for e in value["evidence"]}:
        raise ValueError("duplicate comparison evidence IDs")
    for e in rows:
        if (e["kind"] != "previous-review" or e["scope"] != "comparison-only"
                or e["period"] != prior.document() or not e["timestamps"]
                or not all(prior.contains(t) for t in e["timestamps"])):
            raise ValueError("invalid historical comparison scope")
        item = ReviewItem.model_validate(e["item"])
        if (e["id"] != "P" + digest([prior.document(), item.model_dump()])[:24]
                or e["basis"] != item.basis or e["caveat"] != item.caveat or e["nextCheck"] != item.nextCheck
                or e["text"] != item.title + ": " + item.text):
            raise ValueError("comparison snapshot differs from its original item")
    ReviewDraft(items=[ReviewItem.model_validate(i) for i in value["items"]], rawRequests=[]).validate_sources(
        {e["id"] for e in value["evidence"]} | refs.keys(), current_ids={e["id"] for e in value["evidence"]})
    return refs
