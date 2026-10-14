"""Period-filtered canonical events and bounded, hash-verified original evidence."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from .config import AppConfig
from .media_inputs import describe_embedded_images
from .references import resolve_store_ref
from .review_model import REVIEW_VERSION, Period, ReviewDraft, overlapping_weeks, period_for
from .review_preparation import PREPARATION_VERSION, routine_index, tool_excerpt
from .runtime import PipelineError
from .storage import read_json


def digest(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def review_path(root: Path, period: Period) -> Path:
    return root / period.kind / period.key[:4] / f"{period.key}.json"


def checked_json(path: Path, expected: str | None = None) -> dict[str, Any]:
    content = path.read_bytes()
    if expected and sha256(content).hexdigest() != expected:
        raise PipelineError(f"evidence hash mismatch: {path}")
    value = json.loads(content)
    if not isinstance(value, dict):
        raise PipelineError(f"expected a JSON object: {path}")
    return value


@dataclass
class EvidenceSet:
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    selected: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    weekly_inputs: list[str] = field(default_factory=list)
    monthly_inputs: list[str] = field(default_factory=list)

    def add(self, value: dict[str, Any], *, select: bool = True) -> None:
        self.evidence[value["id"]] = value
        if select and value["id"] not in self.selected:
            self.selected.append(value["id"])

    def merge(self, other: EvidenceSet) -> None:
        self.evidence.update(other.evidence)
        self.selected = list(dict.fromkeys([*self.selected, *other.selected]))
        self.gaps.extend(other.gaps)
        self.sources.extend(other.sources)
        self.weekly_inputs = list(dict.fromkeys([*self.weekly_inputs, *other.weekly_inputs]))
        self.monthly_inputs = list(dict.fromkeys([*self.monthly_inputs, *other.monthly_inputs]))

    def input_records(self) -> list[dict[str, Any]]:
        return [{**{k: v for k, v in self.evidence[id].items()
                 if k in {"id", "text", "kind", "basis", "actor", "label", "timestamps", "caveat", "nextCheck",
                          "category", "intelligenceKind", "threadId"}},
                 "timestamps": list(dict.fromkeys([self.evidence[id]["timestamps"][0],
                                                    self.evidence[id]["timestamps"][-1]]))}
                for id in self.selected]


def _unit(source: AppConfig, entry: dict[str, Any], canonical: Path, events: list[dict[str, Any]],
          text: str, *, kind: str, actor: str, label: str) -> dict[str, Any]:
    # Raw capture hashes change when a conversation grows; the identity of an unchanged
    # historical episode must not. Source IDs are omitted to deduplicate identical mirrors.
    identity = {"thread": entry["threadId"], "events": [e["localId"] for e in events],
                "timestamps": [e["timestamp"] for e in events], "text": text, "kind": kind,
                "eventContents": [digest({k: e.get(k) for k in ("kind", "actor", "text")}) for e in events]}
    return {"id": "E" + digest(identity)[:24], "kind": kind, "text": text, "actor": actor, "label": label,
            "timestamps": sorted(set(e["timestamp"] for e in events)), "sourceId": source.source_id,
            "threadId": entry["threadId"], "title": entry.get("title", ""),
            "canonicalPath": str(canonical), "canonicalRef": entry["canonicalRef"],
            "canonicalSha256": entry["canonicalSha256"], "dataRoot": str(source.data_root),
            "events": [{k: e.get(k, "") for k in ("id", "localId", "timestamp", "rawRef")} for e in events]}


def canonical_evidence(config: AppConfig, period: Period) -> EvidenceSet:
    result = EvidenceSet()
    for source in config.enabled_source_configs():
        path = source.data_root / "catalog/threads.json"
        if not path.is_file():
            result.gaps.append(f"source {source.source_id}: catalog missing; run pull after raw ingest")
            continue
        catalog = checked_json(path)
        result.sources.append({"sourceId": source.source_id, "catalogPath": str(path),
                               "catalogAsOf": catalog.get("asOf"), "catalogSha256": digest(catalog)})
        preparation: dict[str, Any] = {
            "policy": PREPARATION_VERSION, "routineToolRecords": 0, "diagnosticToolRecords": 0,
        }
        result.sources[-1]["preparation"] = preparation
        descriptor = read_json(source.data_root / "store.json")
        for entry in catalog.get("threads", []):
            if (entry.get("sourceId") != source.source_id
                    or entry.get("sourceProvider", "codex") != source.source_provider):
                continue
            if entry.get("status") == "excluded":
                continue
            # Most closed periods do not overlap most conversations. Prune from catalog metadata
            # before reading a potentially large canonical snapshot; unknown dates are not guessed.
            try:
                first = datetime.fromisoformat(entry.get("startedAt", "").replace("Z", "+00:00"))
                last = datetime.fromisoformat(entry.get("lastEventAt", "").replace("Z", "+00:00"))
                if first.tzinfo and last.tzinfo and (
                    first.astimezone(period.zone).date() >= period.end
                    or last.astimezone(period.zone).date() < period.start
                ):
                    continue
            except ValueError:
                pass
            try:
                canonical = resolve_store_ref(source.data_root, entry["canonicalRef"], descriptor)
                saved = checked_json(canonical, entry["canonicalSha256"])
                events = saved["events"]
                selected = [e for e in events if period.contains(e.get("timestamp", ""))]
                if not selected:
                    continue
                if entry.get("status") == "failed":
                    result.gaps.append(f"{source.source_id}/{entry['threadId']}: failed normalization "
                                       f"({entry.get('reason')}); ambiguous evidence excluded")
                    continue
                routine = []
                for e in selected:
                    # Retain dialogue/actions/results and explicit interruptions, not duplicated completions.
                    if (e["kind"] not in {"user_message", "assistant_message", "tool_call", "tool_result"}
                            and e.get("name") != "turn_aborted"):
                        continue
                    if e["kind"] in {"tool_call", "tool_result"}:
                        excerpt, signal = tool_excerpt(e)
                        if not signal:
                            routine.append(e)
                            continue
                        result.add(_unit(source, entry, canonical, [e], excerpt, kind="canonical-excerpt",
                                         actor=e.get("actor", "tool"), label="tool-diagnostic"))
                        preparation["diagnosticToolRecords"] += 1
                        continue
                    text = describe_embedded_images(e.get("text", "")).text
                    if len(text) > 6000:
                        text = text[:6000] + "\n[Source excerpt truncated; request Raw for context.]"
                        result.gaps.append(f"{source.source_id}/{entry['threadId']}/{e['localId']}: excerpt truncated")
                    result.add(_unit(source, entry, canonical, [e], text, kind="canonical-excerpt",
                                     actor=e.get("actor", "unknown"), label=e["kind"]))
                if routine:
                    result.add(_unit(source, entry, canonical, routine, routine_index(routine),
                                     kind="canonical-excerpt", actor="tool", label="routine-tool-index"))
                    preparation["routineToolRecords"] += len(routine)
                    result.gaps.append(f"{source.source_id}/{entry['threadId']}: routine tool bodies compacted; "
                                       "diagnostic excerpts are bounded; use Raw for verification")
                if saved.get("diagnostics", {}).get("invalidLines"):
                    result.gaps.append(f"{source.source_id}/{entry['threadId']}: invalid original log lines")
            except (OSError, ValueError, KeyError, PipelineError) as exc:
                result.gaps.append(f"{source.source_id}/{entry.get('threadId', 'unknown')}: {exc}")
    # Keep one conversation together where possible; unrelated chats can interleave in wall-clock time.
    result.selected.sort(key=lambda id: (result.evidence[id]["threadId"], result.evidence[id]["timestamps"], id))
    result.gaps = sorted(set(result.gaps))
    return result


def period_evidence(config: AppConfig, period: Period) -> EvidenceSet:
    if period.kind == "weekly":
        return canonical_evidence(config, period)
    result = EvidenceSet()
    children = ([period_for("monthly", date(period.start.year, month, 1), period.offset)
                 for month in range(1, 13)] if period.kind == "yearly" else overlapping_weeks(period))
    for child in children:
        path = review_path(config.review_data_root, child)
        path_field = "monthlyPath" if child.kind == "monthly" else "weeklyPath"
        try:
            saved = checked_json(path)
            if saved.get("schemaVersion") != REVIEW_VERSION or saved.get("period") != child.document():
                raise ValueError(f"incompatible {child.kind} review")
            if saved.get("sourceId") != config.source_id:
                raise ValueError(f"{child.kind} review belongs to a different source")
            evidence = {e["id"]: e for e in saved["evidence"]}
            draft = ReviewDraft.model_validate({"items": saved["items"], "rawRequests": []})
            draft.validate_sources(set(evidence))
            inputs = result.monthly_inputs if child.kind == "monthly" else result.weekly_inputs
            inputs.append(str(path))
            result.sources.append({path_field: str(path), "sha256": sha256(path.read_bytes()).hexdigest()})
            result.gaps.extend(f"{child.key}: {gap}" for gap in saved.get("coverage", {}).get("gaps", []))
            covered: set[str] = set()
            for item in draft.items:
                refs = [evidence[id] for id in item.evidenceIds]
                times = sorted({t for ref in refs for t in ref["timestamps"]})
                if not times or not all(period.contains(t) for t in times):
                    continue
                fields = item.model_dump()
                prefix = "M" if child.kind == "monthly" else "W"
                unit = {"id": prefix + digest([child.key, fields])[:24], "kind": f"{child.kind}-review",
                        "text": item.title + ": " + item.text, "basis": item.basis,
                        "caveat": item.caveat, "nextCheck": item.nextCheck,
                        "category": item.category, "intelligenceKind": item.intelligenceKind,
                        "timestamps": times, path_field: str(path), "supports": item.evidenceIds}
                result.add(unit)
                covered.update(item.evidenceIds)
            # Keep transitive provenance off the model input. Boundary fragments use their dated
            # source evidence, not the whole weekly narrative or an invented proportional allocation.
            for id, ref in evidence.items():
                if ref["timestamps"] and all(period.contains(t) for t in ref["timestamps"]):
                    boundary = child.start < period.start or child.end > period.end
                    result.add(ref, select=boundary and id not in covered)
        except (OSError, ValueError, KeyError, PipelineError) as exc:
            fragment = Period(child.kind, max(child.start, period.start), min(child.end, period.end), period.offset)
            fallback = "weekly" if child.kind == "monthly" else "source"
            result.gaps.append(f"{child.key}: {child.kind} review unavailable ({type(exc).__name__}); "
                               f"{fallback} fallback")
            result.merge(period_evidence(config, fragment))
    result.gaps = sorted(set(result.gaps))
    return result


def raw_supplement(config: AppConfig, evidence: EvidenceSet, ids: list[str], period: Period,
                   max_characters: int) -> EvidenceSet:
    """Resolve only cited, contained immutable snapshots; no model-provided path is opened."""
    result = EvidenceSet()
    remaining = max_characters
    pending = list(ids)
    seen: set[str] = set()
    roots = {s.source_id: s.data_root for s in config.enabled_source_configs()}
    while pending and remaining > 0:
        id = pending.pop(0)
        if id in seen:
            continue
        seen.add(id)
        ref = evidence.evidence[id]
        if ref.get("supports"):
            pending.extend(ref["supports"])
            continue
        try:
            root = roots[ref["sourceId"]]
            descriptor = read_json(root / "store.json")
            canonical_path = resolve_store_ref(root, ref["canonicalRef"], descriptor)
            canonical = checked_json(canonical_path, ref["canonicalSha256"])
            events = canonical["events"]
            requested = {e["localId"] for e in ref["events"]}
            positions = {i for i, e in enumerate(events) if e["localId"] in requested}
            # Nearby recorded context can reveal a user interruption or a paired tool result.
            positions = {j for i in positions for j in range(max(0, i-1), min(len(events), i+4))}
            capture_hashes = {canonical["sourceCaptureRef"]: canonical["sourceCaptureSha256"]}
            capture_hashes.update({b["captureRef"]: b["captureSha256"] for b in canonical.get("historyBranches", [])})
            for i in sorted(positions):
                event = events[i]
                if not period.contains(event.get("timestamp", "")):
                    continue
                raw_ref, _, anchor = event["rawRef"].rpartition("#")
                expected_hash = capture_hashes.get(raw_ref, "")
                if not re.fullmatch(r"[0-9a-f]{64}", expected_hash) or not re.fullmatch(r"L\d+", anchor):
                    result.gaps.append(f"{id}: immutable Raw reference unavailable")
                    continue
                snapshot_ref = f"data:/provenance/blobs/{expected_hash[:2]}/{expected_hash}"
                snapshot = resolve_store_ref(root, snapshot_ref, descriptor)
                content = snapshot.read_bytes()
                if sha256(content).hexdigest() != expected_hash:
                    raise PipelineError("Raw snapshot hash mismatch")
                record = json.loads(content.decode("utf-8-sig").split("\n")[int(anchor[1:])-1])
                text = describe_embedded_images(json.dumps(record, ensure_ascii=False)).text
                cap = min(6000, remaining)
                if len(text) > cap:
                    text = text[:cap] + "\n[Raw excerpt truncated.]"
                    result.gaps.append(f"{id}/{anchor}: Raw excerpt truncated")
                remaining -= len(text)
                unit = {**ref, "id": "R" + digest([ref["threadId"], event["localId"], text])[:24],
                        "kind": "raw-excerpt", "text": text, "timestamps": [event["timestamp"]],
                        "rawSnapshotPath": str(snapshot), "rawSha256": expected_hash,
                        "rawLine": int(anchor[1:]), "requestedFor": id,
                        "events": [{k: event.get(k, "") for k in ("id", "localId", "timestamp", "rawRef")}]}
                result.add(unit)
                if remaining <= 0:
                    break
        except (OSError, ValueError, KeyError, IndexError, PipelineError) as exc:
            result.gaps.append(f"{id}: Raw unavailable ({exc})")
    if pending:
        result.gaps.append("Raw character budget exhausted; some requests remain unresolved")
    return result
