from __future__ import annotations

import base64
import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

import pytest
from test_session_note_pipeline import note_data
from test_thread_timeline import candidate, config, event

from tkn_codex_chat_note.media_inputs import describe_embedded_images
from tkn_codex_chat_note.session_notes import ProviderSummarizer, chunk_events, prepare_events

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


def test_image_description_precedes_chunking_and_retains_source_identity() -> None:
    source = replace(event("L000042"), text="Image: " + URI + "\nKeep the written result.", branch_id="Htest",
                     raw_ref="raw:/codex/windows/sessions/source.jsonl#L000042")
    prepared = prepare_events([source])
    assert prepared[0].embedded_image_count == 1
    assert prepared[0].as_dict()["branchId"] == "Htest"
    chunks = chunk_events(prepared, 1200)
    assert len(chunks) == 1
    assert chunks[0][0].id == source.id
    assert chunks[0][0].raw_ref == source.raw_ref
    assert "Keep the written result." in chunks[0][0].text
    assert URI in source.text


def test_generated_note_reports_that_visual_contents_were_not_read(tmp_path: Path) -> None:
    source = replace(event("L000042"), text="Image: " + URI)
    case = candidate(tmp_path, (source,))
    runner = ProviderSummarizer(config(tmp_path))
    with patch.object(runner, "_invoke", return_value=note_data(case)) as invoke:
        result = runner.generate(case)
    assert ENCODED not in invoke.call_args.args[0]
    assert any("L000042" in item and "画像の視覚的内容は確認していません" in item
               for item in result["sourceLimitations"])
    assert runner.last_metrics["embeddedImageCount"] == 1
    assert runner.last_metrics["imageEncodedCharacters"] == len(ENCODED)
