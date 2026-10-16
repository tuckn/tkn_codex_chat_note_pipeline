"""Prepare citation data for the packaged Markdown presentation template."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .review_frontmatter import render_frontmatter
from .review_profiles import ReviewProfile
from .review_sources import citation_view, coverage_groups

# Citation and coverage presentation changes have their own presentation signature.
RENDER_VERSION = "3"


def render_review(value: dict[str, Any], json_path: Path, language: str) -> str:
    profile = ReviewProfile(language)
    p = value["period"]
    title = str(profile.document("profile.json")["periodTitles"][p["kind"]]) + f" — {p['key']}"
    items, sources = citation_view(value)
    important = [g for g in value["coverage"]["gaps"] if any(word in g.lower() for word in
                 ("failed", "missing", "mismatch", "invalid", "unavailable", "excluded"))]
    sections = {name: [i for i in items if i["category"] in categories] for name, categories in (
        ("overview", {"overview"}), ("questions", {"questions", "learning"}),
        ("activities", {"activities", "decisions", "unresolved"}), ("insights", {"insights"}),
        ("ai", {"ai-improvement"}), ("candidates", {"intelligence-candidate"}),
        ("resources", {"reusable-resource", "next-actions"}))}
    return profile.render("template.md", frontmatter=render_frontmatter(value, json_path, title, language),
                          title=title, period=p, period_end=(date.fromisoformat(p["endExclusive"])
                          - timedelta(days=1)).isoformat(), offset=f"{p['utcOffsetMinutes']:+d}",
                          comparison=value.get("comparison"), coverage=value["coverage"],
                          coverage_groups=coverage_groups(value["coverage"]["gaps"]),
                          items=items, sources=sources, json_path=json_path.as_posix(),
                          presentation=profile.document("profile.json"),
                          important_gaps=[g[:300] + ("…" if len(g) > 300 else "")
                                          for g in list(dict.fromkeys(important))[:3]], **sections).strip() + "\n"
