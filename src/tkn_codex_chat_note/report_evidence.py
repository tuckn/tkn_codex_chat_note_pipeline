"""Labels and diagnostics from saved reports; never execute a provider or edit a note."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

LABEL_FIELDS = ("taskTitle", "noteTitle", "noteFile", "noteRef", "displayTitle", "titleSource")
DIAGNOSTIC_FIELDS = (
    "severity", "category", "message", "sourceId", "runId", "threadId", "usageId",
    "startedAt", "date", "provider", "displayModel", "command", "generationProfile",
    "stage", "attempt", "occurrence", "noteStatus", "evidencePath", *LABEL_FIELDS,
)


def message(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("error") or value.get("message") or value.get("reason")
                   or json.dumps(value, ensure_ascii=False, sort_keys=True))
    return str(value)


class ReportEvidence:
    def __init__(self, inventory: list[dict[str, Any]]) -> None:
        self.inventory = inventory
        self.diagnostics: list[dict[str, Any]] = []
        self.labels: dict[tuple[str, str], dict[str, Any]] = {}
        self.note_cache: dict[Path, dict[str, Any]] = {}

    def add(self, context: dict[str, Any], severity: str, category: str, detail: Any, **extra: Any) -> None:
        self.diagnostics.append({**context, "severity": severity, "category": category,
                                 "message": message(detail), **extra})

    def run(self, report: dict[str, Any], source_id: str, path: Path) -> dict[str, Any]:
        context = {
            "sourceId": source_id, "runId": report["runId"], "threadId": None,
            "command": report.get("mode"), "generationProfile": report.get("generationProfile"),
            "provider": report.get("generationProvider"), "startedAt": report.get("startedAt"),
            "evidencePath": str(path),
        }
        for warning in report.get("warnings") or []:
            self.add(context, "WARNING", "run-warning", warning)
        # Raw failures also appear in the top-level failed list; finalize removes duplicates.
        raw = report.get("rawIngest") or {}
        failures = [*(report.get("failed") or []), *(raw.get("failed") or [])]
        if report.get("error"):
            failures.append({"error": report["error"]})
        for failure in failures:
            fields = {k: failure[k] for k in ("threadId", "stage", "sourceRef") if k in failure} \
                if isinstance(failure, dict) else {}
            self.add({**context, **fields}, "ERROR", "run-failure", failure)
        return context

    def thread(self, entry: dict[str, Any], context: dict[str, Any], root: Path) -> None:
        key = (context["sourceId"], context["threadId"])
        label = {"taskTitle": entry.get("title"), "noteRef": entry.get("noteRef"),
                 "observedAt": context.get("reportStartedAt") or context.get("startedAt") or "",
                 "context": context, "root": root}
        previous = self.labels.get(key, {})
        if label["observedAt"] >= previous.get("observedAt", ""):
            # A later failed generation may still refer to the previous successful note.
            self.labels[key] = {**label, "noteRef": label["noteRef"] or previous.get("noteRef")}
        elif not previous.get("noteRef") and label["noteRef"]:
            previous["noteRef"] = label["noteRef"]
        if entry.get("error") or entry.get("status") == "failed":
            self.add(context, "ERROR", "note-failure", entry.get("error") or entry.get("reason") or "failed")
        for warning in entry.get("warnings") or []:
            self.add(context, "WARNING", "note-warning", warning)
        metrics = entry.get("generationMetrics") or {}
        for occurrence, failure in enumerate(metrics.get("validationFailures") or [], start=1):
            self.add(context, "WARNING", "validation", failure,
                     stage=failure.get("stage"), attempt=failure.get("attempt"), occurrence=occurrence)
        if metrics.get("transportRetries"):
            self.add(context, "WARNING", "transport-retry",
                     f"通信・応答の再試行: {metrics['transportRetries']} 回。個々の理由は保存記録がある場合のみ表示。")
        if entry.get("generationStop"):
            self.add(context, "WARNING", "budget-stop",
                     json.dumps(entry["generationStop"], ensure_ascii=False, sort_keys=True))
        elif entry.get("status") in {"deferred", "blocked"}:
            self.add(context, "INFO", "deferred", entry.get("reason") or entry["status"])

    def _note(self, root: Path, ref: Any) -> dict[str, Any]:
        if not isinstance(ref, str) or not ref.startswith("data:/"):
            return {}
        relative = ref.removeprefix("data:/")
        parts = PurePosixPath(relative).parts
        # Reject Windows drives/ADS, traversal, absolute refs and links outside the data root.
        if not parts or any(p in {"..", "."} for p in parts) or ":" in relative or "\\" in relative:
            raise ValueError("unsafe note reference")
        path = root.joinpath(*parts).resolve()
        if not path.is_relative_to(root.resolve()) or path.suffix.lower() != ".md":
            raise ValueError("note reference is outside Markdown data")
        if path not in self.note_cache:
            # Read only bounded Frontmatter; note bodies never enter the usage report.
            header: list[str] = []
            with path.open(encoding="utf-8-sig") as stream:
                if stream.readline(4096).strip() != "---":
                    raise ValueError("note has no Frontmatter")
                size = 0
                while line := stream.readline(65537):
                    size += len(line)
                    if size > 65536:
                        raise ValueError("note Frontmatter exceeds 64K characters")
                    header.append(line)
                    if line.strip() == "---":
                        break
                else:
                    raise ValueError("note Frontmatter is not closed")
            content = "".join(header[:-1])
            parsed = yaml.safe_load(content)
            title = parsed.get("title") if isinstance(parsed, dict) else None
            self.note_cache[path] = {"noteTitle": title if isinstance(title, str) and title.strip() else None,
                                     "noteFile": path.name}
            self.inventory.append({"path": str(path), "sha256": sha256(content.encode()).hexdigest(),
                                   "scope": "frontmatter-utf8"})
        return self.note_cache[path]

    def enrich(self, rows: list[dict[str, Any]]) -> None:
        for label in self.labels.values():
            if "resolved" not in label:
                ref = label.get("noteRef")
                if isinstance(ref, str) and ref.startswith("data:/"):
                    label["noteFile"] = PurePosixPath(ref).name
                try:
                    label.update(self._note(label["root"], label.get("noteRef")))
                except (OSError, ValueError, yaml.YAMLError) as exc:
                    self.add(label["context"], "WARNING", "note-metadata", f"ノートのtitleを取得できません: {exc}")
                label["resolved"] = True
        for row in rows:
            label = self.labels.get((str(row.get("sourceId") or ""), str(row.get("threadId") or "")), {})
            for key in LABEL_FIELDS:
                row[key] = row.get(key) or label.get(key)
            row["displayTitle"] = (row.get("noteTitle") or row.get("taskTitle")
                                   or row.get("noteFile") or row.get("threadId"))
            row["titleSource"] = ("frontmatter-current" if row.get("noteTitle") else
                                  "run-report" if row.get("taskTitle") else
                                  "filename" if row.get("noteFile") else "thread-id")

    def attempts(self, records: list[dict[str, Any]]) -> None:
        for record in records:
            status = record.get("status")
            if status in {"failed", "transport-failed", "invalid-response", "rejected", "started"}:
                http = f" / HTTP {record['httpStatus']}" if record.get("httpStatus") is not None else ""
                detail = record.get("error") or f"試行結果: {status}{http}。詳細理由は使用量履歴に未記録。"
                self.add(record, "WARNING" if status == "started" else "ERROR", "attempt", detail)
