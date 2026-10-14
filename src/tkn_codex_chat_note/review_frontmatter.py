"""Flat, typed review metadata; presentation changes do not require inference."""

from __future__ import annotations

import json
import re
from hashlib import sha256
from pathlib import Path
from string import Template
from typing import Any

from .review_profiles import ReviewProfile


def presentation_signature(language: str = "ja") -> str:
    return sha256(("formatter:3\n" + ReviewProfile(language).presentation_signature()).encode("utf-8")).hexdigest()


def _scalar(value: str | int | None) -> str:
    if value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    if re.match(r"^(?:[A-Za-z]:[\\/]|\\\\)", value):
        return "'" + value.replace("'", "''") + "'"
    # Quoting every dynamic string preserves dates, precision, YAML-like values
    # and literal newlines without allowing metadata to introduce new YAML keys.
    return json.dumps(value, ensure_ascii=False)


def render_frontmatter(value: dict[str, Any], json_path: Path, title: str, language: str = "ja") -> str:
    p = value["period"]
    generation = value.get("generation", {})
    # Keep the complete overview and its qualifications; do not truncate a claim.
    overview = [part for item in value["items"] if item["category"] == "overview"
                for part in (item["text"], item.get("caveat", "")) if part]
    description = " ".join(" ".join(overview).split())
    profile = ReviewProfile(language)
    template = profile.text("frontmatter.template.yaml")
    fields = {
        "title": title, "description": description,
        "periodKind": p["kind"], "periodKey": p["key"], "periodStart": p["start"],
        "periodEndExclusive": p["endExclusive"], "utcOffsetMinutes": p["utcOffsetMinutes"],
        "sourceId": value["sourceId"], "source": json_path.absolute().as_uri(),
        "coverageStatus": value["coverage"]["status"], "reviewStatus": value["reviewStatus"],
        "generationProfile": generation.get("profile"), "provider": generation.get("provider"),
        # The stored model is a requested setting, not provider-confirmed telemetry.
        "requestedModel": generation.get("model"), "generatedAt": value["generatedAt"],
        "noteId": value["id"], "sourceFingerprint": value["inputFingerprint"],
        "generationSettingsSha256": generation.get("settingsSha256"),
        "created": value.get("created"), "updated": value.get("updated", value["generatedAt"]),
        "noteProfile": profile.name, "templateSha256": profile.presentation_signature(),
    }
    rendered = Template(template).substitute({k: _scalar(v) for k, v in fields.items()})
    return "\n".join(line.rstrip() for line in rendered.splitlines())
