"""Conservative inference-only compaction; canonical evidence is never changed."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Sequence
from hashlib import sha256
from html.parser import HTMLParser
from typing import Any

from .chat_logs import ChatEvent


def _output_body(text: str) -> str | None:
    match = re.search(r"(?:^|\n)(?:Final output|Output):\r?\n", text)
    return text[match.end():] if match else None


def _line_endings(text: str) -> str:
    return re.sub(r"\r+\n", "\n", text).strip("\r\n")


def _same_output(completed: str, response: str) -> bool:
    if _line_endings(completed) == _line_endings(response):
        return True
    # A debug-escaped completion must match a whole-string encoding of the
    # response. Never decode selected escapes in otherwise plain output/code.
    if any(char in completed for char in "\r\n\t\x1b"):
        return False
    def trim_endings(text: str) -> str:
        return re.sub(r"^(?:\\r|\\n)+|(?:\\r|\\n)+$", "", text)
    for text in (response, re.sub(r"\r+\n", "\r\n", response), _line_endings(response)):
        encoded = json.dumps(text, ensure_ascii=False)[1:-1].replace("\\u001b", "\\x1b")
        if trim_endings(completed) == trim_endings(encoded):
            return True
    return False


_TRUNCATION = re.compile(r"…\d+ chars truncated…")


def _covered_excerpt(completed: str, response: str) -> bool:
    """Only explicit head/tail excerpts whose every retained character is covered."""
    parts = _TRUNCATION.split(_line_endings(completed))
    if len(parts) != 2 or min(map(len, parts)) < 80:
        return False
    other = _TRUNCATION.split(_line_endings(response))
    if len(other) > 2:
        return False
    return other[0].startswith(parts[0]) and other[-1].endswith(parts[-1])


def compact_duplicate_outputs(events: Sequence[ChatEvent]) -> dict[str, str]:
    """Reference identical same-call output or a provably covered head/tail excerpt.

    Retain the completion event, status, command, timestamps, and all other fields.
    Ambiguous IDs, different turns/branches, user boundaries, or changed output
    keep both bodies. The referenced response always remains in the input set.
    """
    responses: dict[tuple[str, str, int, str], list[ChatEvent]] = defaultdict(list)
    completions: dict[tuple[str, str, int, str], list[tuple[ChatEvent, dict[str, Any]]]] = defaultdict(list)
    boundary = 0
    scopes: dict[str, tuple[str, str, int]] = {}
    for event in events:
        if event.actor == "user":
            boundary += 1
        scope = (event.branch_id, event.turn_id, boundary)
        scopes[event.id] = scope
        if event.kind == "tool_result" and event.name:
            responses[(*scope, event.name)].append(event)
        if event.name != "item_completed":
            continue
        try:
            payload = json.loads(event.text)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        item = payload.get("item")
        if (
            isinstance(item, dict) and item.get("type") in {"CommandExecution", "commandExecution"}
            and isinstance(item.get("id"), str) and item["id"]
            and isinstance(item.get("aggregated_output"), str)
        ):
            completions[(*scope, item["id"])].append((event, payload))
    replacements: dict[str, str] = {}
    for key, entries in completions.items():
        matches = responses.get(key, [])
        if len(entries) != 1 or len(matches) != 1:
            continue
        event, payload = entries[0]
        result = matches[0]
        body = _output_body(result.text)
        original = payload["item"]["aggregated_output"]
        if body is None or not (_same_output(original, body) or _covered_excerpt(original, body)):
            continue
        payload["item"]["aggregated_output"] = {
            "duplicateOfEventId": result.id,
            "characters": len(original),
            "sourceTruncated": bool(_TRUNCATION.search(original)),
            "sha256": sha256(original.encode("utf-8")).hexdigest(),
        }
        compacted = json.dumps(payload, ensure_ascii=False)
        if len(compacted) < len(event.text):
            replacements[event.id] = compacted
    for event in events:
        if event.name != "item_completed":
            continue
        try:
            payload = json.loads(replacements.get(event.id, event.text))
            item = payload["item"]
        except (ValueError, KeyError, TypeError):
            continue
        if not isinstance(item, dict):
            continue
        if item.get("type") == "FileChange" and isinstance(item.get("changes"), dict):
            changed = False
            for filename, change in item["changes"].items():
                if not isinstance(change, dict) or not isinstance(change.get("content"), str):
                    continue
                matches = []
                for other in events:
                    if other.name != "apply_patch" or other.kind != "tool_call":
                        continue
                    if scopes[other.id] != scopes[event.id]:
                        continue
                    for patch in re.finditer(r"\*\*\* Add File: ([^\n]+)\n(.*?)(?=\*\*\* |$)", other.text, re.S):
                        name, body = patch.groups()
                        normalized = filename.replace("\\", "/")
                        normalized = re.sub(r"/+", "/", normalized)
                        if not normalized.endswith("/" + name.strip().replace("\\", "/")):
                            continue
                        lines = body.splitlines(keepends=True)
                        if not lines or any(not line.startswith("+") for line in lines):
                            continue
                        added = "".join(line[1:] for line in lines)
                        if _same_output(change["content"], added):
                            matches.append(other)
                if len(matches) == 1:
                    changed = True
                    change["content"] = {"duplicateOfEventId": matches[0].id,
                                         "characters": len(change["content"])}
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if changed and len(encoded) < len(event.text):
                replacements[event.id] = encoded
            continue
        if item.get("type") not in {"UserMessage", "AgentMessage"}:
            continue
        if not event.turn_id:
            continue
        actor = "user" if item["type"] == "UserMessage" else "assistant"
        content = item.get("content")
        if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict):
            continue
        text = content[0].get("text")
        if not isinstance(text, str):
            continue
        matches = [other for other in events
                   if other.actor == actor and other.kind == actor + "_message"
                   and (other.branch_id, other.turn_id) == (event.branch_id, event.turn_id)
                   and _same_output(text, other.text)]
        if len(matches) == 1:
            content[0]["text"] = {"duplicateOfEventId": matches[0].id, "characters": len(text)}
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if len(encoded) < len(event.text):
                replacements[event.id] = encoded
    return replacements


# Bump when the prepared evidence contract changes; recorded in generation identity.
INPUT_PREPARATION_VERSION = 2
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
_DIAGNOSTIC = re.compile(
    r"error|exception|traceback|fail(?:ed|ure)?|warning|denied|timeout|not found|"
    r"not supported|cannot|could not|invalid|assert|passed|succeeded|completed|"
    r"失敗|エラー|警告|未対応|不明|未確認|検証|確認結果|完了|中断", re.I,
)
_STRUCTURE = re.compile(r"^\s*(?:[#]{1,6} |(?:async )?def |class |\*\*\* |@@|[+-](?![+-]))")


class _PageText(HTMLParser):
    """Extract visible text, preferring the article/main region over site navigation."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool, bool]] = []
        self.all_text: list[str] = []
        self.article: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        hidden = (self.stack[-1][1] if self.stack else False) or tag in {"script", "style", "head", "svg"}
        main = (self.stack[-1][2] if self.stack else False) or tag in {"main", "article"}
        main = main or attrs_dict.get("role") == "main"
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}:
            self.stack.append((tag, hidden, main))

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data: str) -> None:
        if self.stack and self.stack[-1][1]:
            return
        if data.strip():
            self.all_text.append(data.strip())
            if self.stack and self.stack[-1][2]:
                self.article.append(data.strip())


def _excerpt(text: str) -> str:
    """Keep boundary excerpts and every diagnostic line with context, with explicit gaps.

    Applied only to recognized bulk tool material, never to conversational prose.
    The budget is soft: diagnostics are never dropped to meet a size target.
    """
    if len(text) <= 12000:
        return text
    lines = text.splitlines(keepends=True)
    # A single long line may contain a useful fact anywhere: retain it in full.
    if len(lines) < 12:
        return text
    keep: set[int] = set()
    for indexes in (range(len(lines)), range(len(lines) - 1, -1, -1)):
        size = 0
        for i in indexes:
            keep.add(i)
            size += len(lines[i])
            if size >= 2000:
                break
    structural_size = 0
    for i, line in enumerate(lines):
        if _DIAGNOSTIC.search(line):
            keep.update(range(max(0, i - 2), min(len(lines), i + 3)))
        elif structural_size < 2000 and _STRUCTURE.search(line):
            keep.add(i)
            structural_size += len(line)
    out: list[str] = []
    previous = -1
    for i in sorted(keep):
        if i > previous + 1:
            out.append(f"[excerpt: source lines {previous + 2}-{i} omitted; full text retained in Raw]\n")
        out.append(lines[i])
        previous = i
    result = "".join(out)
    return result if len(result) < len(text) else text


def _bulk_text(text: str, *, code: bool = False) -> str:
    """Strip terminal decoration and extract evidence from known bulky formats."""
    cleaned = _ANSI.sub("", text)
    prefix, separator, body = cleaned.partition("Output:\n")
    html = body if separator else cleaned
    if re.match(r"\s*(?:<!doctype html|<html\b)", html, re.I):
        # Truncated markup can leave the parser inside a script/tag and hide real evidence.
        if _TRUNCATION.search(html) or "</html>" not in html.lower():
            return cleaned
        parser = _PageText()
        try:
            parser.feed(html)
            visible = "\n".join(parser.article or parser.all_text)
        except (ValueError, AssertionError):
            return cleaned
        if visible:
            return (prefix + separator if separator else "") + "[HTML visible text]\n" + visible
    lines = cleaned.splitlines()
    search_lines = sum(bool(re.match(r"^.*\.(?:md|py|json|yaml|txt):\d+:", line)) for line in lines)
    listing_lines = sum(bool(re.match(r"^\s*(?:\d{4}[-/]\d{2}[-/]\d{2}|[d-][a-z-]{3,}\s)", line))
                        for line in lines)
    is_bulk = code or search_lines >= 12 or listing_lines >= 12
    return _excerpt(cleaned) if is_bulk else cleaned


def compact_event_text(event: ChatEvent, text: str) -> str:
    """Prepare a smaller, explicitly excerpted view; never mutate canonical events."""
    if event.kind in {"user_message", "assistant_message"}:
        return text
    if event.name == "item_completed":
        try:
            payload = json.loads(text)
        except ValueError:
            return text
        if not isinstance(payload, dict) or not isinstance(payload.get("item"), dict):
            return text
        before = json.dumps(payload, ensure_ascii=False)
        item = payload["item"]
        for key in ("aggregated_output", "stdout", "stderr"):
            if isinstance(item.get(key), str):
                item[key] = _bulk_text(item[key])
        changes = item.get("changes")
        if isinstance(changes, dict):
            for change in changes.values():
                if isinstance(change, dict):
                    for key in ("content", "diff"):
                        if isinstance(change.get(key), str):
                            change[key] = _bulk_text(change[key], code=True)
        if json.dumps(payload, ensure_ascii=False) == before:
            return text
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return _bulk_text(text, code=event.kind == "tool_call" and event.name == "apply_patch")
