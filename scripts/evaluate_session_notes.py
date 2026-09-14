"""Evaluate explicitly selected, hash-pinned Canonical Events in a separate output folder.

The manifest contains baseline rows with metadata, activity, and files (sessionNote,
raw, canonicalEvents). It is private application data, not repository sample data.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import yaml

from tkn_codex_chat_note.chat_logs import ChatEvent
from tkn_codex_chat_note.config import GenerationConfig
from tkn_codex_chat_note.inference import provider_name
from tkn_codex_chat_note.session_notes import (
    Candidate,
    PipelineConfig,
    Project,
    ProviderSummarizer,
    atomic_write_json,
    atomic_write_text,
    generation_fingerprint,
    render_note,
    validate_session_note,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path, help="YAML with generation settings only")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--thread", action="append", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    generation = GenerationConfig.model_validate(yaml.safe_load(args.config.read_text(encoding="utf-8"))["generation"])
    settings = generation.profiles[generation.active_profile]
    options = settings.inference_options()
    cfg = PipelineConfig(
        installed_at="2026-01-01T00:00:00+00:00",
        sessions_root=args.output,
        raw_root=args.output,
        source_id="evaluation",
        codex_bin=settings.executable or "codex",
        provider=settings.provider,
        generation_profile=generation.active_profile,
        claude_bin=settings.executable or "claude",
        copilot_bin=settings.executable or "copilot",
        model=settings.model,
        reasoning_effort=settings.reasoning_effort,
        inference_options=options,
        ollama_base_url=settings.endpoint or "http://127.0.0.1:11434",
        model_timeout_seconds=300,
        session_note_profile=generation.session_note_profile,
    )
    runner = ProviderSummarizer(
        cfg,
        cache_root=args.output / "cache",
        observer=lambda e: print(
            json.dumps(
                {k: v for k, v in e.items() if k in {"type", "part", "partCount", "stage", "reason"}}, ensure_ascii=True
            ),
            file=sys.stderr,
            flush=True,
        ),
    )
    rows = json.loads(args.manifest.read_text(encoding="utf-8"))
    failed = False
    for thread in args.thread:
        matches = [r for r in rows if r["metadata"]["sourceThreadIds"] == [thread]]
        if not matches:
            raise ValueError("thread missing from pinned manifest")
        row = max(matches, key=lambda r: str(r["metadata"]["generatedAt"]))
        for kind, path in row["files"].items():
            if sha256(Path(path).read_bytes()).hexdigest() != Path(path).name:
                raise ValueError(f"baseline hash mismatch: {kind}")
        canonical = json.loads(Path(row["files"]["canonicalEvents"]).read_text(encoding="utf-8"))
        events = tuple(
            ChatEvent(
                id=e["id"],
                kind=e["kind"],
                actor=e["actor"],
                name=e["name"],
                text=e["text"],
                timestamp=e["timestamp"],
                turn_id=e["turnId"],
                cwd=e["cwd"],
                branch_id=e.get("branchId", ""),
                raw_ref=e.get("rawRef", ""),
            )
            for e in canonical["events"]
        )
        project = Project(
            project_id="evaluation", title="Evaluation", current_root=args.output, context_path=args.output
        )
        candidate = Candidate(
            project=project,
            thread_id=thread,
            started_at=canonical["startedAt"],
            source_path=Path(row["files"]["raw"]),
            source_ref=f"codex/{thread}",
            source_relative_ref=canonical["sourceCaptureRef"],
            fingerprint=row["metadata"]["sourceFingerprint"],
            events=events,
            source_last_event_at=canonical["lastEventAt"],
            source_capture_ref=canonical["sourceCaptureRef"],
            source_capture_sha256=canonical["sourceCaptureSha256"],
        )
        fingerprint = generation_fingerprint(cfg, candidate)
        report_path = args.output / (thread + ".json")
        if report_path.exists():
            prior = json.loads(report_path.read_text(encoding="utf-8"))
            if prior["generationFingerprint"] != fingerprint:
                raise ValueError("output already belongs to different generation settings; use a new output folder")
            if prior["status"] == "passed":
                print(json.dumps({"thread": thread, "status": "unchanged"}), flush=True)
                continue
        if args.dry_run:
            print(json.dumps({"thread": thread, "status": "would-generate", "events": len(events),
                              "generationEstimate": runner.estimate(candidate)}), flush=True)
            continue
        report = {
            "schemaVersion": 1,
            "runId": str(uuid4()),
            "startedAt": datetime.now(UTC).isoformat(),
            "threadId": thread,
            "baseline": row["files"],
            "generationFingerprint": fingerprint,
            "provider": cfg.provider,
            "model": cfg.model,
            "reasoningEffort": cfg.reasoning_effort,
            "inferenceOptions": options,
            "profileSha256": cfg.summary_profile.sha256,
            "status": "running",
        }
        atomic_write_json(report_path, report)
        started = time.monotonic()
        try:
            value = runner.generate(candidate)
            value.update(
                _generator=provider_name(cfg.provider),
                _generatorProvider=cfg.provider,
                _generatorModel=runner.last_metrics.get("responseModel", cfg.model),
                _generatorDeployment=cfg.model if cfg.provider == "azure-openai" else None,
                _generatorReasoningEffort=cfg.reasoning_effort,
            )
            note = args.output / (thread + ".md")
            rendered = render_note(candidate, value, {"id": str(uuid4())}, profile=cfg.summary_profile)
            # Validate a staging file before publishing a successful output.
            staging = args.output / (thread + ".pending.md")
            atomic_write_text(staging, rendered)
            validate_session_note(staging)
            staging.replace(note)
            atomic_write_json(args.output / (thread + ".structured.json"), value)
            report.update(status="passed", note=str(note), noteSha256=sha256(note.read_bytes()).hexdigest())
        except Exception as exc:
            report.update(status="failed", error=str(exc))
            failed = True
        finally:
            report.update(
                durationSeconds=round(time.monotonic() - started, 3),
                endedAt=datetime.now(UTC).isoformat(),
                metrics=runner.last_metrics,
            )
            atomic_write_json(args.output / "run-history" / (report["runId"] + ".json"), report)
            atomic_write_json(report_path, report)
        print(json.dumps({k: report[k] for k in ("threadId", "status", "durationSeconds")}), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
