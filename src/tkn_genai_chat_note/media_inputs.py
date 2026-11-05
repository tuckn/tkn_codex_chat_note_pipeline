"""Describe embedded image bytes for text inference while retaining source evidence."""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

_DATA_IMAGE = re.compile(r"data:(image/[A-Za-z0-9.+-]+);base64,([A-Za-z0-9+/=_-]+)")


@dataclass(frozen=True)
class PreparedText:
    text: str
    image_count: int = 0
    encoded_characters: int = 0


def describe_embedded_images(text: str) -> PreparedText:
    """Replace only typed image payloads, not long text or arbitrary base64 strings.

    No image is decoded for visual interpretation. Its bytes remain in Raw and
    canonical evidence; the text model receives a hash and explicit limitation.
    """
    count = 0
    characters = 0

    def describe(encoded: str, mime: str) -> str:
        nonlocal count, characters
        try:
            content = base64.b64decode(encoded, altchars=b"-_", validate=True)
        except (ValueError, binascii.Error):
            return encoded
        if not content:
            return encoded
        if mime == "image/unknown":
            if content.startswith(b"\x89PNG\r\n\x1a\n"):
                mime = "image/png"
            elif content.startswith(b"\xff\xd8\xff"):
                mime = "image/jpeg"
            elif content.startswith((b"GIF87a", b"GIF89a")):
                mime = "image/gif"
            elif content.startswith(b"RIFF") and content[8:12] == b"WEBP":
                mime = "image/webp"
            else:
                return encoded
        count += 1
        characters += len(encoded)
        return (
            f"[Embedded image: {mime}; bytes={len(content)}; sha256={sha256(content).hexdigest()}; "
            "image bytes retained in source; visual content was not inspected]"
        )

    def replace_uri(match: re.Match[str]) -> str:
        replacement = describe(match[2], match[1])
        return match[0] if replacement == match[2] else replacement

    def visit(value: Any) -> Any:
        if isinstance(value, list):
            return [visit(item) for item in value]
        if isinstance(value, dict):
            result = dict(value)
            mime = value.get("mimeType") or value.get("mime_type") or "image/unknown"
            typed_image = value.get("type") == "image" and str(mime).startswith("image/")
            image_generation = value.get("type") == "Extension" and str(value.get("kind", "")).startswith("image_gen.")
            for key, child in value.items():
                if isinstance(child, str) and ((typed_image and key == "data") or
                                              (image_generation and key == "result")):
                    result[key] = describe(child, str(mime))
                else:
                    result[key] = visit(child)
            return result
        if isinstance(value, str):
            return _DATA_IMAGE.sub(replace_uri, value)
        return value

    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        prepared = _DATA_IMAGE.sub(replace_uri, text)
    else:
        if isinstance(value, (dict, list)):
            value = visit(value)
            prepared = json.dumps(value, ensure_ascii=False, separators=(",", ":")) if count else text
        else:
            prepared = _DATA_IMAGE.sub(replace_uri, text)
    return PreparedText(prepared, count, characters)
