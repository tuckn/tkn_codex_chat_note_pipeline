"""Exact citation navigation and descriptive coverage summaries without inference."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .chat_logs import clean_user_text, content_to_text
from .chat_titles import conversation_title, display_title


def evidence_message(evidence: dict[str, Any]) -> tuple[str, str]:
    actor, text = str(evidence.get("actor", "unknown")), str(evidence.get("text", ""))
    if evidence.get("kind") == "raw-excerpt":
        try:
            record = json.loads(text)
            payload = record.get("payload", {})
            if isinstance(payload, dict) and payload.get("type") in {
                    "function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output"}:
                actor = "tool"
            if isinstance(payload, dict) and payload.get("type") in {"message", "user_message", "agent_message"}:
                actor = str(payload.get("role") or ("user" if payload["type"] == "user_message" else
                                                   "assistant" if payload["type"] == "agent_message" else actor))
                text = content_to_text(payload.get("content")) or str(payload.get("message") or text)
        except (ValueError, AttributeError):
            pass  # A truncated Raw record still needs an honest, bounded excerpt.
    return actor, clean_user_text(text) if actor == "user" else text


def citable_evidence(evidence: dict[str, Any]) -> bool:
    actor, text = evidence_message(evidence)
    return bool(text.strip()) or actor not in {"user", "assistant"}


def coverage_groups(gaps: list[str]) -> list[dict[str, Any]]:
    def kind(gap: str) -> str:
        if "routine tool bodies compacted" in gap:
            return "tool-compaction"
        if "excerpt truncated" in gap.lower():
            return "excerpt-truncation"
        if "Raw character budget exhausted" in gap:
            return "raw-character-limit"
        if "Raw request limit reached" in gap:
            return "raw-request-limit"
        if "failed normalization" in gap:
            return "normalization-failure"
        if "invalid original log lines" in gap:
            return "invalid-log-lines"
        if "review unavailable" in gap:
            return "missing-review"
        if "Raw unavailable" in gap or "Raw reference unavailable" in gap:
            return "raw-unavailable"
        if "no citable evidence" in gap:
            return "uncitable-evidence"
        if "catalog missing" in gap:
            return "missing-catalog"
        return "other"
    counts = Counter(kind(gap) for gap in gaps)
    return [{"kind": key, "count": n} for key, n in sorted(counts.items())]


def citation_view(value: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    evidence = {e["id"]: e for e in value["evidence"]}
    groups: dict[str, dict[str, Any]] = {}
    labels: dict[str, str] = {}
    zone = timezone(timedelta(minutes=value["period"]["utcOffsetMinutes"]))
    for item in value["items"]:
        for id in item["evidenceIds"]:
            e = evidence[id]
            if not citable_evidence(e):
                continue
            path = (e.get("canonicalPath") or e.get("rawSnapshotPath") or e.get("monthlyPath")
                    or e.get("weeklyPath") or e.get("notePath"))
            key = str(path or id)
            if key not in groups:
                groups[key] = {"label": f"S{len(groups) + 1:03d}", "records": {}, "citations": {}, "key": key}
            group = groups[key]
            if id not in group["records"]:
                actor, text = evidence_message(e)
                quote = " ".join(text.split())
                times = list(e["timestamps"])
                anchors = list(dict.fromkeys(str(event.get("localId") or "") for event in e.get("events", [])))
                if e.get("rawLine"):
                    anchors = [f"L{e['rawLine']:06d}"]
                group["records"][id] = {"id": id, "label": f"E{len(group['records']) + 1:03d}",
                    "anchor": group["label"].lower() + f"-e{len(group['records']) + 1:03d}",
                    "times": list(dict.fromkeys(times if len(times) <= 2 else [times[0], times[-1]])), "actor": actor,
                    "quote": quote[:160] + ("…" if len(quote) > 160 else ""),
                    "locations": (", ".join(a for a in anchors if a) if len(anchors) <= 6
                                  else f"{anchors[0]} … {anchors[-1]} [{len(anchors)}]")}
            labels[id] = key
    items = []
    for item in value["items"]:
        by_source: dict[str, list[str]] = {}
        for id in item["evidenceIds"]:
            if id in labels:
                by_source.setdefault(labels[id], []).append(id)
        refs = []
        for key, ids in by_source.items():
            group = groups[key]
            identity = tuple(sorted(set(ids)))
            if identity not in group["citations"]:
                n = len(group["citations"]) + 1
                group["citations"][identity] = {"number": n, "anchor": group["label"].lower() + f"-c{n}",
                                                 "records": [group["records"][id] for id in dict.fromkeys(ids)]}
            citation = group["citations"][identity]
            refs.append(f"[{group['label']}](#{citation['anchor']})")
        items.append({**item, "refs": " ".join(refs),
                      "uncitableEvidenceIds": [id for id in item["evidenceIds"] if id not in labels]})
    sources = []
    for key, group in groups.items():
        originals = [evidence[id] for id in group["records"]]
        thread_ids = {e.get("threadId") for e in originals if e.get("threadId")}
        related = [e for e in value["evidence"] if e.get("threadId") in thread_ids]
        requests = [text for e in sorted(related, key=lambda e: e["timestamps"]) for actor, text in
                    [evidence_message(e)] if actor == "user" and text.strip()]
        names: Iterable[str] = (str(e.get("title", "")) for e in originals)
        title = next((name for original in names if (name := display_title(original))), "")
        dates = sorted({datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(zone).date().isoformat()
                        for r in group["records"].values() for t in r["times"]})
        sources.append({"label": group["label"],
            "name": conversation_title(title, requests, str(originals[0].get("threadId", "")))
                    if thread_ids else title or Path(key).stem,
            "span": dates[0] + (" — " + dates[-1] if len(dates) > 1 else ""),
            "path": Path(key).as_posix() if key not in evidence else "",
            "citations": list(group["citations"].values()), "records": list(group["records"].values())})
    return items, sources
