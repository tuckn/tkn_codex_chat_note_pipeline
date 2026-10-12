"""Settings for source-backed calendar reviews, independent of usage reports."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ReviewSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_root: Path = Field(default_factory=lambda: Path.home() / ".tkn/codex_chat_note_pipeline/data/reviews")
    state_root: Path = Field(default_factory=lambda: Path.home() / ".tkn/codex_chat_note_pipeline/state/reviews")
    language: Literal["ja", "en"] = "ja"
    utc_offset_minutes: int = Field(default=540, ge=-720, le=840)
    start_date: date | None = None
    max_calls: int = Field(default=12, ge=1)
    session_note_limit: int = Field(default=10, ge=1)
    input_characters: int = Field(default=50000, ge=8000)
    raw_characters: int = Field(default=16000, ge=1000)
    raw_requests: int = Field(default=8, ge=0, le=32)
