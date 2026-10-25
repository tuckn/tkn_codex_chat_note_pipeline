"""Bounded review inference with reusable validated stage checkpoints."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import replace
from itertools import groupby
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from .api_inference import ApiClient
from .config import AppConfig
from .inference import invoke_structured
from .review_comparison import COMPARISON_VERSION, PreviousContext
from .review_evidence import EvidenceSet, digest, raw_supplement
from .review_model import REVIEW_VERSION, Period, ReviewDraft
from .review_profiles import ReviewProfile
from .review_quality import (
    EVIDENCE_CONTEXT_VERSION,
    QUALITY_VERSION,
    CoverageResult,
    EvidenceBudgetError,
    EvidenceResult,
    audit_review,
    check_editorial,
    editorial_limits,
)
from .review_render import RENDER_VERSION
from .review_transport import evidence_aliases, map_evidence_ids
from .runtime import PipelineError, atomic_write_json
from .usage_records import UsageJournal

LOGGER = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

Generator = Callable[[str, dict[str, Any]], dict[str, Any]]


class ReviewBudgetStop(PipelineError):
    def __init__(self, details: dict[str, Any]) -> None:
        super().__init__(str(details["message"]))
        self.details = details



class ReviewGenerator:
    def __init__(self, config: AppConfig, run_id: str, *, generate: Generator | None = None,
                 force: bool = False) -> None:
        self.config = config
        self.settings = config.reviews
        self.generate = generate
        self.force = force
        self.calls = 0
        self.cache_hits = 0
        self.deadline = time.monotonic() + config.runtime_minutes * 60
        self.profile = ReviewProfile(self.settings.language)
        self.request_signature = self.profile.request_signature()
        self.instructions = self.profile.text("prompt.md")
        self.synthesis = self.profile.text("synthesis-prompt.md")
        self.quality_policy = self.profile.text("quality-prompt.md")
        self.comparison_policy = self.profile.text("comparison-prompt.md")
        self.quality_report: dict[str, Any] = {}
        self.previous_evidence: dict[str, dict[str, Any]] = {}
        self.schema = self.profile.document("output.schema.json")
        if self.schema != ReviewDraft.model_json_schema():
            raise PipelineError("review profile schema differs from the validated ReviewDraft contract")
        source = config.enabled_source_configs()[0]
        self.pipeline_config = source.inference_config()
        options = dict(self.pipeline_config.inference_options)
        if options.get("generation_settings_sha256"):
            # Bridge's generation identity excludes profile aliases and price-only changes.
            options.pop("bridge", None)
            options.pop("bridge_profile", None)
        self.signature = digest({"version": REVIEW_VERSION, "instructions": self.instructions,
                                 "schema": self.schema, "settings": self.settings.model_dump(mode="json", include={
                                     "language", "utc_offset_minutes", "input_characters",
                                     "raw_characters", "raw_requests"
                                 }),
                                 "provider": config.provider, "model": config.model,
                                 "options": options,
                                 "effort": config.reasoning_effort})
        # Extraction checkpoints remain reusable when only editorial policy changes.
        self.extraction_signature = self.signature
        self.cache_root = config.cache_root / "reviews" / self.extraction_signature
        self.signature = digest({"extraction": self.extraction_signature, "synthesis": self.synthesis,
                                 "renderer": RENDER_VERSION, "quality": self.quality_policy,
                                 "comparisonVersion": COMPARISON_VERSION, "comparisonPolicy": self.comparison_policy,
                                 "qualityVersion": QUALITY_VERSION,
                                 "evidenceContextVersion": EVIDENCE_CONTEXT_VERSION,
                                 "qualitySchemas": [CoverageResult.model_json_schema(),
                                                    EvidenceResult.model_json_schema()]})
        self.journal = UsageJournal(config.review_state_root / "usage" / run_id,
                                    {"runId": run_id, "command": "review-notes", "sourceId": config.source_id,
                                     "generationProfile": config.generation.active_profile, "noteStatus": "unfinished"})
        self.api: ApiClient | None = None

    def close(self) -> None:
        if self.api:
            self.api.close()

    def _call(self, stage: str, period: Period, payload: dict[str, Any], ids: set[str]) -> ReviewDraft:
        instructions = self.synthesis if stage in {"merge", "compose"} else self.instructions
        if stage == "compose" and self.previous_evidence:
            instructions += "\n" + self.comparison_policy
        if stage == "compose":
            payload = {**payload, "editorialLimits": editorial_limits(self.settings.language)}
        prompt = self.profile.render("request.template.md", instructions=instructions, stage=stage,
                                     language=self.settings.language, period_json=json.dumps(period.document()),
                                     input_json=json.dumps(payload, ensure_ascii=False))
        if len(prompt) > self.settings.input_characters:
            raise PipelineError("review stage exceeds reviews.input_characters; increase that bound or reduce input")
        path = self.cache_root / (digest(prompt) + ".json")
        if path.is_file() and not self.force:
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                if saved.get("sha256") != digest(saved["draft"]):
                    raise ValueError("invalid cache digest")
                draft = ReviewDraft.model_validate(saved["draft"])
                draft.validate_sources(ids, current_ids=ids - self.previous_evidence.keys())
                if stage == "compose":
                    check_editorial(draft, self.settings.language)
                self.cache_hits += 1
                LOGGER.info("%s %s: reusing cached %s response; no model call", period.kind, period.key, stage)
                return draft
            except (ValueError, KeyError):
                pass
        pending = path.with_suffix(".pending.json")
        value = None
        if pending.is_file() and not self.force:
            saved = json.loads(pending.read_text("utf-8"))
            if saved.get("sha256") == digest(saved.get("draft")):
                value = saved["draft"]
        if value is None:
            value = self._invoke(stage, prompt)
            atomic_write_json(pending, {"draft": value, "sha256": digest(value)})
        try:
            draft = ReviewDraft.model_validate(value)
            draft.validate_sources(ids, current_ids=ids - self.previous_evidence.keys())
            if stage == "compose":
                check_editorial(draft, self.settings.language)
        except ValueError as exc:
            # Repair only the returned draft, not the expensive original evidence batch.
            # Retain it across budget stops so the next invocation can resume the repair.
            repair = self.profile.render("repair-prompt.md", instructions=instructions,
                input_json=json.dumps({"draft": value, "validationError": str(exc)[:4000],
                                       "availableEvidenceIds": sorted(ids),
                                       "editorialLimits": payload.get("editorialLimits")}, ensure_ascii=False))
            value = self._invoke("repair", repair)
            draft = ReviewDraft.model_validate(value)
            draft.validate_sources(ids, current_ids=ids - self.previous_evidence.keys())
            if stage == "compose":
                check_editorial(draft, self.settings.language)
        atomic_write_json(path, {"draft": draft.model_dump(), "sha256": digest(draft.model_dump())})
        pending.unlink(missing_ok=True)
        return draft

    def _quality_call(self, stage: str, period: Period, payload: dict[str, Any],
                      contract: type[T], validate: Callable[[T], None]) -> T:
        schema = contract.model_json_schema()
        aliases = evidence_aliases(payload) if stage in {"evidence-check", "overview-check"} else {}
        canonical = {alias: id for id, alias in aliases.items()}
        prompt = self.profile.render("request.template.md", instructions=self.quality_policy, stage=stage,
                                     language=self.settings.language, period_json=json.dumps(period.document()),
                                     input_json=json.dumps(map_evidence_ids(payload, aliases), ensure_ascii=False))
        identity = {"prompt": prompt, "schema": schema, "version": QUALITY_VERSION}
        if aliases:
            identity["evidenceAliases"] = aliases
        path = self.cache_root / (digest(identity) + ".json")
        if path.is_file() and not self.force:
            try:
                saved = json.loads(path.read_text("utf-8"))
                if saved["sha256"] != digest(saved["result"]):
                    raise ValueError("invalid audit cache digest")
                result = contract.model_validate(saved["result"])
                validate(result)
                self.cache_hits += 1
                LOGGER.info("%s %s: reusing cached %s response; no model call", period.kind, period.key, stage)
                return result
            except (ValueError, KeyError):
                pass
        pending = path.with_suffix(".pending.json")
        value = None
        if pending.is_file() and not self.force:
            saved = json.loads(pending.read_text("utf-8"))
            if saved.get("sha256") == digest(saved.get("result")):
                value = saved["result"]
        if value is None:
            value = map_evidence_ids(self._invoke(stage, prompt, schema), canonical)
            atomic_write_json(pending, {"result": value, "sha256": digest(value), "evidenceAliases": aliases})
        try:
            result = contract.model_validate(value)
            validate(result)
        except ValueError as exc:
            repair_input, repair_value = payload, value
            repair_ids: set[str] | None = None
            if isinstance(exc, EvidenceBudgetError) and "evidence" in payload:
                repair_ids = exc.item_ids
                units = [u for u in payload["auditUnits"] if u["id"] in repair_ids]
                allowed = {id for u in units for id in u["evidenceIds"]}
                repair_input = {**payload, "auditUnits": units,
                                "evidence": [e for e in payload["evidence"] if e["id"] in allowed]}
                repair_value = {"decisions": [d for d in value["decisions"] if d["itemId"] in repair_ids]}
            repair = self.profile.render("audit-repair-prompt.md", instructions=self.quality_policy,
                input_json=json.dumps(map_evidence_ids({"result": repair_value,
                    "validationError": str(exc)[:3000], "originalInput": repair_input}, aliases), ensure_ascii=False))
            # Repair includes the sources; an oversized repair stops without publishing unchecked text.
            repaired = map_evidence_ids(self._invoke("audit-repair", repair, schema), canonical)
            if repair_ids is not None:
                patch = EvidenceResult.model_validate(repaired)
                ids = [d.itemId for d in patch.decisions]
                if set(ids) != repair_ids or len(ids) != len(repair_ids):
                    raise ValueError("targeted repair must account for the supplied items exactly once") from exc
                replacements = {d.itemId: d.model_dump() for d in patch.decisions}
                value = {"decisions": [replacements.get(d["itemId"], d) for d in value["decisions"]]}
            else:
                value = repaired
            result = contract.model_validate(value)
            validate(result)
        atomic_write_json(path, {"result": result.model_dump(), "sha256": digest(result.model_dump()),
                                 "evidenceAliases": aliases})
        pending.unlink(missing_ok=True)
        return result

    def _invoke(self, stage: str, prompt: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
        schema = self.schema if schema is None else schema
        if len(prompt) > self.settings.input_characters:
            raise PipelineError("review stage exceeds reviews.input_characters; increase that bound or reduce input")
        if self.calls >= self.settings.max_calls:
            raise ReviewBudgetStop({
                "reason": "review-call-budget", "requestCount": self.calls, "maxCalls": self.settings.max_calls,
                "message": f"Model call limit reached ({self.calls}/{self.settings.max_calls}); "
                           "stopped before the next model call. Review generation is incomplete.",
            })
        started = time.monotonic()
        if started >= self.deadline:
            raise ReviewBudgetStop({
                "reason": "runtime-deadline", "runtimeMinutes": self.config.runtime_minutes,
                "requestCount": self.calls, "maxCalls": self.settings.max_calls,
                "message": f"Review runtime limit reached ({self.config.runtime_minutes} minutes); "
                           "stopped before the next model call. Review generation is incomplete.",
            })
        self.calls += 1
        LOGGER.info("Model call %s/%s: starting %s; source %s; period %s; waiting for response",
                    self.calls, self.settings.max_calls, stage, self.config.source_id,
                    self.journal.context.get("threadId", "unspecified"))
        timeout = min(self.config.model_timeout_seconds, max(1, int(self.deadline - started)))
        if self.generate:
            result = self.generate(prompt, schema)
        elif self.config.provider == "azure-openai" or self.pipeline_config.inference_options.get("limits"):
            if self.api is None:
                self.api = ApiClient(self.pipeline_config)
                self.api.observer = self.journal
            self.api.stage = "review-" + stage
            result = self.api.invoke(prompt, schema, timeout=timeout)
        else:
            result = invoke_structured(self.pipeline_config, prompt, schema, cwd=Path.cwd(), timeout=timeout,
                                       usage_observer=self.journal)
        LOGGER.info("Model call %s/%s: %s response received (%.1fs)",
                    self.calls, self.settings.max_calls, stage, time.monotonic() - started)
        return result

    def _batches(self, records: list[dict[str, Any]], *, synthesis: bool = False,
                 reserve: int = 0) -> list[list[dict[str, Any]]]:
        instructions = self.synthesis if synthesis else self.instructions
        ceiling = self.settings.input_characters - len(instructions) - 2200 - reserve
        batches: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        size = 0
        for _, group in groupby(records, key=lambda r: r.get("threadId")):
            chunk = list(group)
            amounts = [len(json.dumps(record, ensure_ascii=False)) + 2 for record in chunk]
            # A conversation that fits in one call should not be split by an earlier conversation.
            if current and sum(amounts) <= ceiling and size + sum(amounts) > ceiling:
                batches.append(current)
                current, size = [], 0
            for record, amount in zip(chunk, amounts, strict=True):
                if amount > ceiling:
                    raise PipelineError("one review evidence item exceeds the configured input bound")
                if current and size + amount > ceiling:
                    batches.append(current)
                    current, size = [], 0
                current.append(record)
                size += amount
        if current:
            batches.append(current)
        return batches

    def build(self, period: Period, evidence: EvidenceSet, previous: PreviousContext | None = None) -> ReviewDraft:
        self.journal.context.update(threadId=period.kind + "/" + period.key, taskTitle=period.key + " review")
        self.quality_report = {}
        self.previous_evidence = {}
        self.quality_policy = self.profile.text("quality-prompt.md")
        drafts: list[ReviewDraft] = []
        batches = self._batches(evidence.input_records())
        for number, batch in enumerate(batches, 1):
            ids = {e["id"] for e in batch}
            draft = self._call("review", period, {
                "evidence": batch, "batch": number, "batchCount": len(batches),
                "coverage": {"completeHistory": False, "gapCount": len(evidence.gaps),
                             "limitationExamples": [g[:200] for g in evidence.gaps[:5]]},
            }, ids)
            if draft.rawRequests:
                requested = list(dict.fromkeys(r.evidenceId for r in draft.rawRequests))
                allowed = requested[:self.settings.raw_requests]
                if len(allowed) < len(requested):
                    evidence.gaps.append("Raw request limit reached; some questions remain unverified")
                supplement = raw_supplement(self.config, evidence, allowed, period, self.settings.raw_characters)
                evidence.merge(supplement)
                ids.update(supplement.selected)
                # The earlier draft already provides a compact account of the batch. Include its
                # citations and original requested summaries alongside the new source records.
                payload = {"draft": draft.model_dump(), "rawEvidence": supplement.input_records(),
                           "requestedEvidence": [e for e in batch if e["id"] in allowed],
                           "availableEvidenceIds": sorted(ids), "gaps": supplement.gaps,
                           "instruction": self.profile.instruction("raw-pass")}
                draft = self._call("raw-check", period, payload, ids)
                if draft.rawRequests:
                    raise PipelineError("review requested additional Raw after the bounded Raw pass")
            drafts.append(draft)
        if not drafts:
            return ReviewDraft(items=[], rawRequests=[])
        findings = [i for d in drafts for i in d.items]
        if previous is not None:
            previous.select(findings, evidence, max(0, self.settings.input_characters // 8))
            self.previous_evidence = {e["id"]: e for e in previous.selected}
        draft = self.synthesize(period, drafts)
        if self.previous_evidence:
            self.quality_policy += "\n" + self.comparison_policy
        audit_evidence = replace(evidence, evidence={**evidence.evidence, **self.previous_evidence})
        return audit_review(self, period, draft, findings, audit_evidence)

    def synthesize(self, period: Period, drafts: list[ReviewDraft]) -> ReviewDraft:
        """Edit validated findings into one period review, including a single input draft."""
        self.journal.context.update(threadId=period.kind + "/" + period.key, taskTitle=period.key + " review")
        # Intermediate merges preserve distinct findings; only the final pass edits for readers.
        for _ in range(6):
            items = [i.model_dump() for d in drafts for i in d.items]
            if not items:
                raise PipelineError("model returned no review items for nonempty evidence")
            context = [{k: v for k, v in e.items() if k in {
                "id", "kind", "scope", "period", "text", "basis", "caveat", "nextCheck"}}
                for e in self.previous_evidence.values()]
            reserve = (len(json.dumps(context, ensure_ascii=False))
                       + len(self.comparison_policy) + 100) if context else 0
            batches = self._batches(items, synthesis=True, reserve=reserve)
            stage = "compose" if len(batches) == 1 else "merge"
            comparison = context if stage == "compose" else []
            merged = [self._call(stage, period, {"items": batch, "rawRequestsAllowed": False,
                                                 "previousContext": comparison},
                                 {id for item in batch for id in item["evidenceIds"]}
                                 | {e["id"] for e in comparison}) for batch in batches]
            if any(d.rawRequests for d in merged):
                raise PipelineError("merge stages must preserve uncertainty instead of requesting new Raw")
            if any(not d.items for d in merged):
                raise PipelineError("model returned no review items for nonempty evidence")
            if stage == "compose":
                return merged[0]
            old_size = len(json.dumps(items, ensure_ascii=False))
            new_size = len(json.dumps([i.model_dump() for d in merged for i in d.items], ensure_ascii=False))
            if new_size >= old_size:
                raise PipelineError("review merge did not shrink; increase reviews.input_characters")
            drafts = merged
        raise PipelineError("review merge depth exceeded; cached stages are retained")
