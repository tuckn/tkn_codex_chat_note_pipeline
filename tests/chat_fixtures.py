from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path


def json_line(event_type: str, payload: dict, timestamp: str) -> str:
    return json.dumps({"timestamp": timestamp, "type": event_type, "payload": payload})

def write_chat(
    path: Path,
    *,
    thread_id: str,
    cwd: Path,
    request: str = "do work",
    started_at: str = "2026-07-01T00:00:00Z",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))

    def event_time(seconds: int) -> str:
        return (started + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")

    lines = [
        json_line(
            "session_meta",
            {
                "id": thread_id,
                "timestamp": started_at,
                "cwd": str(cwd),
                "thread_source": "user",
            },
            event_time(0),
        ),
        json_line(
            "turn_context",
            {"turn_id": "turn-1", "cwd": str(cwd)},
            event_time(1),
        ),
        json_line(
            "response_item",
            {"type": "message", "role": "user", "content": [{"text": request}]},
            event_time(2),
        ),
        json_line(
            "event_msg",
            {"type": "user_message", "message": request},
            event_time(2),
        ),
        json_line(
            "response_item",
            {"type": "custom_tool_call", "name": "exec", "input": {"cmd": "test"}},
            event_time(3),
        ),
        json_line(
            "response_item",
            {
                "type": "custom_tool_call_output",
                "call_id": "call-1",
                "output": "password=0123456789abcdef and tests passed",
            },
            event_time(4),
        ),
        json_line(
            "response_item",
            {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"text": "completed"}],
            },
            event_time(5),
        ),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    old = (datetime.now().astimezone() - timedelta(hours=1)).timestamp()
    os.utime(path, (old, old))


def config(root: Path):
    from tkn_codex_chat_note.runtime import InferenceConfig
    return InferenceConfig()
