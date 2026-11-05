"""Settings for source-backed calendar reviews, independent of usage reports."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ReviewSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: Literal["ja", "en"] = "ja"
    utc_offset_minutes: int = Field(default=540, ge=-720, le=840)
    # Accepted for existing configuration files; due always uses all saved history.
    start_date: date | None = None
    previous_period: bool = True
    max_calls: int = Field(default=12, ge=1)
    input_characters: int = Field(default=50000, ge=8000)
    raw_characters: int = Field(default=16000, ge=1000)
    raw_requests: int = Field(default=8, ge=0, le=32)
