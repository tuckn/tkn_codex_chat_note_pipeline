"""Validated, offline reference-price scenarios, independent of inference pricing."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator
from tkn_genai_bridge import TokenPricing


class PriceScenario(TokenPricing):
    """Inline scenario; share Bridge's rate contract and validation."""
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    # Annotation metadata also runs during per-layer validation, before config merging.
    pricing_date: Annotated[str, BeforeValidator(lambda value: value.isoformat() if isinstance(value, date) else value)]


class GenAIPriceScenario(BaseModel):
    """Reference the confirmed rates of one model in a root genai_profiles entry."""

    model_config = ConfigDict(extra="forbid")
    genai_profile: str = Field(min_length=1)
    model: str = Field(min_length=1)


class UsageReportSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    utc_offset_minutes: int = Field(default=0, ge=-720, le=840)
    price_scenarios: dict[str, GenAIPriceScenario | PriceScenario] = Field(default_factory=dict)

    @field_validator("price_scenarios")
    @classmethod
    def validate_names(cls, value: dict[str, GenAIPriceScenario | PriceScenario]
                       ) -> dict[str, GenAIPriceScenario | PriceScenario]:
        if any(not name.strip() or len(name) > 120 for name in value):
            raise ValueError("price scenario names must contain 1-120 characters")
        return value
