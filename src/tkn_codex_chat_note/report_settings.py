"""Validated, offline reference-price scenarios, independent of inference pricing."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PriceScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    pricing_date: date
    input_per_million: float = Field(ge=0, allow_inf_nan=False)
    output_per_million: float = Field(ge=0, allow_inf_nan=False)
    cache_policy: Literal["no-cache", "observed"] = "no-cache"
    cached_input_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    cache_write_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def require_cache_rate(self) -> PriceScenario:
        if self.cache_policy == "observed" and self.cached_input_per_million is None:
            raise ValueError("observed cache policy requires cached_input_per_million")
        return self


class UsageReportSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    utc_offset_minutes: int = Field(default=0, ge=-720, le=840)
    price_scenarios: dict[str, PriceScenario] = Field(default_factory=dict)

    @field_validator("price_scenarios")
    @classmethod
    def validate_names(cls, value: dict[str, PriceScenario]) -> dict[str, PriceScenario]:
        if any(not name.strip() or len(name) > 120 for name in value):
            raise ValueError("price scenario names must contain 1-120 characters")
        return value
