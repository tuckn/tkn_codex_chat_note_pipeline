"""Packaged Japanese/English review policies and presentation templates."""

from __future__ import annotations

import json
from hashlib import sha256
from importlib.resources import files
from typing import Any

from jinja2 import Environment, StrictUndefined

from .runtime import PipelineError


class ReviewProfile:
    def __init__(self, language: str) -> None:
        if language not in {"ja", "en"}:
            raise PipelineError("unsupported review profile language")
        self.language = language
        self.name = "default-jp" if language == "ja" else "default-en"
        self.root = files("tkn_codex_chat_note").joinpath("profiles", self.name)

    def text(self, name: str) -> str:
        return self.root.joinpath(name).read_text("utf-8")

    def document(self, name: str) -> dict[str, Any]:
        value = json.loads(self.text(name))
        if not isinstance(value, dict):
            raise PipelineError("review profile resource must be a JSON object")
        return value

    def render(self, name: str, **fields: Any) -> str:
        env = Environment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True,
                          extensions=["jinja2.ext.do"])
        env.filters["cell"] = markdown_cell
        return env.from_string(self.text(name)).render(**fields)

    def instruction(self, name: str) -> str:
        return str(self.document("instructions.json")[name])

    def request_signature(self) -> str:
        return sha256(json.dumps({name: self.text(name) for name in (
            "request.template.md", "repair-prompt.md", "audit-repair-prompt.md", "instructions.json")},
            ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    def presentation_signature(self) -> str:
        return sha256(json.dumps({name: self.text(name) for name in (
            "profile.json", "frontmatter.template.yaml", "template.md")},
            ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def markdown_cell(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("|", "&#124;").replace("\r", "").replace("\n", "<br>"))
