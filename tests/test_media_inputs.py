from __future__ import annotations

import base64
import json
from hashlib import sha256

import pytest

from tkn_codex_chat_note.media_inputs import describe_embedded_images

IMAGE_BYTES = b"\x89PNG\r\n\x1a\n" + b"image-payload" * 10000
ENCODED = base64.b64encode(IMAGE_BYTES).decode("ascii")
URI = "data:image/png;base64," + ENCODED


@pytest.mark.parametrize("body", [
    [{"type": "input_image", "image_url": URI}, {"type": "text", "text": "Keep this correction."}],
    {"type": "image", "mimeType": "image/png", "data": ENCODED, "caption": "Keep this correction."},
    {"item": {"type": "Extension", "kind": "image_gen.generation", "result": ENCODED,
              "revisedPrompt": "Keep this correction.", "savedPath": "C:/example/diagram.png"}},
])
def test_image_payload_becomes_metadata_without_losing_neighboring_text(body: object) -> None:
    source = json.dumps(body, ensure_ascii=False)
    result = describe_embedded_images(source)
    assert result.image_count == 1 and result.encoded_characters == len(ENCODED)
    assert ENCODED not in result.text
    assert "Keep this correction." in result.text
    assert sha256(IMAGE_BYTES).hexdigest() in result.text
    assert f"bytes={len(IMAGE_BYTES)}" in result.text
    assert "visual content was not inspected" in result.text
    assert len(result.text) < 1000
    json.loads(result.text)
    assert ENCODED in source


def test_embedded_data_uri_in_plain_text_and_multiple_images() -> None:
    source = "Before " + URI + " middle " + URI + " after"
    result = describe_embedded_images(source)
    assert result.image_count == 2
    assert result.text.startswith("Before ") and result.text.endswith(" after")
    assert " middle " in result.text


@pytest.mark.parametrize("source", [
    ENCODED,
    "long ordinary text " * 30000,
    '{ "arbitraryData": "' + ENCODED + '" }',
    '{"type":"Extension","kind":"image_gen.generation","result":"done"}',
    '{"type":"image","mimeType":"image/png","data":"not valid base64!"}',
    "data:image/png;base64,a",
], ids=["untyped-base64", "long-text", "unknown-json-field", "text-result", "invalid-encoding", "invalid-uri"])
def test_untyped_payloads_and_invalid_encoding_are_unchanged(source: str) -> None:
    result = describe_embedded_images(source)
    assert result.text == source
    assert result.image_count == 0
