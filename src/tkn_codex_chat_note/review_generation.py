"""Bounded review inference with reusable validated stage checkpoints."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path
from typing import Any

from .api_inference import ApiClient
from .config import AppConfig
from .inference import invoke_structured
from .review_evidence import EvidenceSet, digest, raw_supplement
from .review_model import REVIEW_VERSION, Period, ReviewDraft
from .session_notes import PipelineError, atomic_write_json
from .usage_records import UsageJournal

Generator = Callable[[str, dict[str, Any]], dict[str, Any]]


class ReviewBudgetStop(PipelineError):
    pass


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
        self.instructions = files("tkn_codex_chat_note").joinpath("resources/review-prompt.md").read_text("utf-8")
        self.schema = ReviewDraft.model_json_schema()
        source = config.enabled_source_configs()[0]
        self.pipeline_config = source.session_note_pipeline_config(allow_missing_watermark=True)
        self.signature = digest({"version": REVIEW_VERSION, "instructions": self.instructions,
                                 "schema": self.schema, "settings": self.settings.model_dump(mode="json", include={
                                     "language", "utc_offset_minutes", "input_characters",
                                     "raw_characters", "raw_requests"
                                 }),
                                 "provider": config.provider, "model": config.model,
                                 "options": self.pipeline_config.inference_options,
                                 "effort": config.reasoning_effort})
        self.cache_root = config.cache_root / "reviews" / self.signature
        self.journal = UsageJournal(self.settings.state_root / "usage" / run_id,
                                    {"runId": run_id, "command": "review-notes", "sourceId": "reviews",
                                     "generationProfile": config.generation.active_profile, "noteStatus": "unfinished"})
        self.api: ApiClient | None = None

    def close(self) -> None:
        if self.api:
            self.api.close()

    def _call(self, stage: str, period: Period, payload: dict[str, Any], ids: set[str]) -> ReviewDraft:
        prompt = (self.instructions + f"\nStage: {stage}\nLanguage: {self.settings.language}\n"
                  + "Period: " + json.dumps(period.document()) + "\nBEGIN_REVIEW_INPUT_JSON\n"
                  + json.dumps(payload, ensure_ascii=False) + "\nEND_REVIEW_INPUT_JSON")
        if len(prompt) > self.settings.input_characters:
            raise PipelineError("review stage exceeds reviews.input_characters; increase that bound or reduce input")
        path = self.cache_root / (digest(prompt) + ".json")
        if path.is_file() and not self.force:
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                if saved.get("sha256") != digest(saved["draft"]):
                    raise ValueError("invalid cache digest")
                draft = ReviewDraft.model_validate(saved["draft"])
                draft.validate_sources(ids)
                self.cache_hits += 1
                return draft
            except (ValueError, KeyError):
                pass
        if self.calls >= self.settings.max_calls or time.monotonic() >= self.deadline:
            raise ReviewBudgetStop("review call/time budget reached; validated stages are cached for the next run")
        self.calls += 1
        timeout = min(self.config.model_timeout_seconds, max(1, int(self.deadline - time.monotonic())))
        if self.generate:
            value = self.generate(prompt, self.schema)
        elif self.config.provider == "azure-openai" or self.pipeline_config.inference_options.get("limits"):
            if self.api is None:
                self.api = ApiClient(self.pipeline_config)
                self.api.observer = self.journal
            self.api.stage = "review-" + stage
            value = self.api.invoke(prompt, self.schema, timeout=timeout)
        else:
            value = invoke_structured(self.pipeline_config, prompt, self.schema, cwd=Path.cwd(), timeout=timeout,
                                      usage_observer=self.journal)
        draft = ReviewDraft.model_validate(value)
        draft.validate_sources(ids)
        atomic_write_json(path, {"draft": draft.model_dump(), "sha256": digest(draft.model_dump())})
        return draft

    def _batches(self, records: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        ceiling = self.settings.input_characters - len(self.instructions) - 2200
        batches: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        size = 0
        for record in records:
            amount = len(json.dumps(record, ensure_ascii=False)) + 2
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

    def build(self, period: Period, evidence: EvidenceSet) -> ReviewDraft:
        self.journal.context.update(threadId=period.kind + "/" + period.key, taskTitle=period.key + " review")
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
                           "instruction": "Final Raw pass. Leave rawRequests empty; state unresolved limits."}
                draft = self._call("raw-check", period, payload, ids)
                if draft.rawRequests:
                    raise PipelineError("review requested additional Raw after the bounded Raw pass")
            drafts.append(draft)
        if not drafts:
            return ReviewDraft(items=[], rawRequests=[])
        # Bounded hierarchical merge also permits full-year reviews without a single giant prompt.
        for _ in range(6):
            if len(drafts) == 1:
                if not drafts[0].items:
                    raise PipelineError("model returned no review items for nonempty evidence")
                self.journal.finish("generated")
                return drafts[0]
            items = [i.model_dump() for d in drafts for i in d.items]
            if not items:
                raise PipelineError("model returned no review items for nonempty evidence")
            batches = self._batches(items)
            merged = [self._call("merge", period, {"items": batch, "rawRequestsAllowed": False},
                                 {id for item in batch for id in item["evidenceIds"]}) for batch in batches]
            if any(d.rawRequests for d in merged):
                raise PipelineError("merge stages must preserve uncertainty instead of requesting new Raw")
            old_size = len(json.dumps(items, ensure_ascii=False))
            new_size = len(json.dumps([i.model_dump() for d in merged for i in d.items], ensure_ascii=False))
            if len(merged) > 1 and new_size >= old_size:
                raise PipelineError("review merge did not shrink; increase reviews.input_characters")
            drafts = merged
        raise PipelineError("review merge depth exceeded; cached stages are retained")
