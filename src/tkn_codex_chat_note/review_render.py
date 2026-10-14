"""Prepare citation data for the packaged Markdown presentation template."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .review_frontmatter import render_frontmatter
from .review_profiles import ReviewProfile

# Generation identity is unchanged: profile relocation preserves inference policy.
RENDER_VERSION = "3"


def render_review(value: dict[str, Any], json_path: Path, language: str) -> str:
    profile = ReviewProfile(language)
    p = value["period"]
    title = str(profile.document("profile.json")["periodTitles"][p["kind"]]) + f" — {p['key']}"
    evidence = {e["id"]: e for e in value["evidence"]}
    used = list(dict.fromkeys(id for item in value["items"] for id in item["evidenceIds"]))
    groups: dict[str, list[dict[str, Any]]] = {}
    labels: dict[str, str] = {}
    for id in used:
        e = evidence[id]
        path = (e.get("canonicalPath") or e.get("rawSnapshotPath") or e.get("monthlyPath")
                or e.get("weeklyPath") or e.get("notePath"))
        key = str(path or id)
        groups.setdefault(key, []).append(e)
        labels[id] = f"S{list(groups).index(key) + 1:03d}"
    items = [{**item, "refs": " ".join(f"[{label}](#{label.lower()})" for label in
              dict.fromkeys(labels[id] for id in item["evidenceIds"]))} for item in value["items"]]
    sources = []
    for number, (path, records) in enumerate(groups.items(), 1):
        dates = sorted({datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(
            timezone(timedelta(minutes=p["utcOffsetMinutes"]))).date().isoformat()
            for e in records for t in e["timestamps"]})
        sources.append({"label": f"S{number:03d}",
                        "name": records[0].get("title") or records[0].get("label") or records[0]["kind"],
                        "span": dates[0] + (" — " + dates[-1] if len(dates) > 1 else ""),
                        "path": Path(path).as_posix() if path not in evidence else ""})
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
                          coverage=value["coverage"], items=items, sources=sources, json_path=json_path.as_posix(),
                          important_gaps=[g[:300] + ("…" if len(g) > 300 else "")
                                          for g in list(dict.fromkeys(important))[:3]], **sections).strip() + "\n"
