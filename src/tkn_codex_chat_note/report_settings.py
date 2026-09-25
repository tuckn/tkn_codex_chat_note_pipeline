"""Validated, offline reference-price scenarios, independent of inference pricing."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PrivateAttr, field_validator, model_validator
from tkn_genai_bridge import GenAIError, TokenPricing, load_profile


class PriceScenario(TokenPricing):
    """Legacy inline scenario; share Bridge's rate contract and validation."""
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    # Annotation metadata also runs during per-layer validation, before config merging.
    pricing_date: Annotated[str, BeforeValidator(lambda value: value.isoformat() if isinstance(value, date) else value)]


class BridgePriceScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bridge_profile: str = Field(min_length=1)
    model: str = Field(min_length=1)
    _pricing: TokenPricing | None = PrivateAttr(default=None)

    @property
    def pricing(self) -> TokenPricing:
        if self._pricing is None:
            try:
                profile = load_profile(self.bridge_profile)
            except GenAIError as exc:
                raise ValueError(f"Bridge price scenario {self.bridge_profile!r}: {exc}") from None
            if self.model not in profile.pricing:
                raise ValueError(f"Bridge profile {self.bridge_profile!r} has no pricing for {self.model!r}")
            self._pricing = profile.pricing[self.model]
        return self._pricing

    @model_validator(mode="after")
    def resolve_price(self) -> BridgePriceScenario:
        self._pricing = self.pricing
        return self


class UsageReportSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    utc_offset_minutes: int = Field(default=0, ge=-720, le=840)
    price_scenarios: dict[str, BridgePriceScenario | PriceScenario] = Field(default_factory=dict)

    @field_validator("price_scenarios")
    @classmethod
    def validate_names(cls, value: dict[str, BridgePriceScenario | PriceScenario]
                       ) -> dict[str, BridgePriceScenario | PriceScenario]:
        if any(not name.strip() or len(name) > 120 for name in value):
            raise ValueError("price scenario names must contain 1-120 characters")
        return value
