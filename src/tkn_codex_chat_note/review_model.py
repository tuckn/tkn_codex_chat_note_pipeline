"""Calendar boundaries and the source-cited review output contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PeriodKind = Literal["weekly", "monthly", "yearly"]
KINDS: tuple[PeriodKind, ...] = ("weekly", "monthly", "yearly")
REVIEW_VERSION = "1.0.0"


@dataclass(frozen=True)
class Period:
    kind: PeriodKind
    start: date
    end: date
    offset: int = 540

    @property
    def key(self) -> str:
        if self.kind == "weekly":
            year, week, _ = self.start.isocalendar()
            return f"{year}-W{week:02}"
        return self.start.strftime("%Y-%m" if self.kind == "monthly" else "%Y")

    @property
    def zone(self) -> timezone:
        return timezone(timedelta(minutes=self.offset))

    def contains(self, value: str) -> bool:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.tzinfo is not None and self.start <= dt.astimezone(self.zone).date() < self.end
        except ValueError:
            return False

    def document(self) -> dict[str, Any]:
        return {"kind": self.kind, "key": self.key, "start": self.start.isoformat(),
                "endExclusive": self.end.isoformat(), "utcOffsetMinutes": self.offset,
                "startAt": datetime.combine(self.start, time(), self.zone).isoformat(),
                "endAtExclusive": datetime.combine(self.end, time(), self.zone).isoformat()}


def period_for(kind: PeriodKind, day: date, offset: int = 540) -> Period:
    if kind == "weekly":
        start = day - timedelta(days=day.weekday())
        end = start + timedelta(days=7)
    elif kind == "monthly":
        start = day.replace(day=1)
        end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    else:
        start, end = date(day.year, 1, 1), date(day.year + 1, 1, 1)
    return Period(kind, start, end, offset)


def closed_periods(start: date, today: date, offset: int) -> list[Period]:
    result = []
    for kind in KINDS:
        p = period_for(kind, start, offset)
        while p.end <= today:
            result.append(p)
            p = period_for(kind, p.end, offset)
    return sorted(result, key=lambda p: (p.end, KINDS.index(p.kind)))


def overlapping_weeks(period: Period) -> list[Period]:
    result = []
    week = period_for("weekly", period.start, period.offset)
    while week.start < period.end:
        result.append(week)
        week = period_for("weekly", week.end, period.offset)
    return result


Category = Literal["overview", "questions", "learning", "activities", "decisions", "unresolved", "insights",
                   "next-actions", "intelligence-candidate", "ai-improvement", "reusable-resource"]


class ReviewItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Category
    title: str = Field(min_length=1)
    text: str = Field(min_length=1)
    basis: Literal["observed", "reported", "inferred", "proposed"]
    evidenceIds: list[str] = Field(min_length=1)
    caveat: str
    nextCheck: str
    intelligenceKind: Literal["none", "semantics", "knowledge", "context", "domain"]

    @model_validator(mode="after")
    def distinguish_proposals(self) -> ReviewItem:
        if self.category in {"next-actions", "ai-improvement"} and self.basis != "proposed":
            raise ValueError("improvements and next actions must remain proposals")
        if self.category == "insights" and self.basis != "inferred":
            raise ValueError("cross-chat insights must remain inferences")
        if self.category == "intelligence-candidate" and self.intelligenceKind == "none":
            raise ValueError("intelligence candidates require a kind")
        if self.category != "intelligence-candidate" and self.intelligenceKind != "none":
            raise ValueError("only intelligence candidates carry an intelligence kind")
        if self.category in {"insights", "intelligence-candidate", "ai-improvement"}:
            if not self.caveat.strip() or not self.nextCheck.strip():
                raise ValueError("insights and candidates require limitations and a next check")
        return self


class RawRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidenceId: str
    reason: str = Field(min_length=1)


class ReviewDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ReviewItem]
    rawRequests: list[RawRequest]

    def validate_sources(self, ids: set[str]) -> None:
        cited = {e for item in self.items for e in item.evidenceIds}
        cited.update(request.evidenceId for request in self.rawRequests)
        if missing := cited - ids:
            raise ValueError("review cites unavailable evidence: " + ", ".join(sorted(missing)))
