"""Read-only token estimates: never download or create a tokenizer cache."""

from __future__ import annotations

import base64
import os
from functools import lru_cache
from hashlib import sha1, sha256
from pathlib import Path

import tiktoken

O200K_URL = "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
O200K_SHA256 = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
# o200k_base pre-tokenization pattern from the MIT-licensed tiktoken implementation.
O200K_PATTERN = "|".join(
    [
        r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
        r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
        r"\p{N}{1,3}",
        r" ?[^\s\p{L}\p{N}]+[\r\n/]*",
        r"\s*[\r\n]+",
        r"\s+(?!\S)",
        r"\s+",
    ]
)


def tokenizer_cache_path() -> Path | None:
    directory = os.environ.get("TIKTOKEN_CACHE_DIR", os.environ.get("DATA_GYM_CACHE_DIR"))
    if directory is None:
        # tempfile.gettempdir() can create probe files; preview must remain read-only.
        directory = str(
            Path(os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp")
            / "data-gym-cache"
        )
    return Path(directory) / sha1(O200K_URL.encode()).hexdigest() if directory else None


@lru_cache(maxsize=4)
def cached_encoding(path: Path | None) -> tiktoken.Encoding | None:
    if path is None:
        return None
    try:
        data = path.read_bytes()
        if sha256(data).hexdigest() != O200K_SHA256:
            return None
        ranks = {base64.b64decode(token): int(rank) for token, rank in (line.split() for line in data.splitlines())}
        return tiktoken.Encoding(
            name="o200k_base_offline", pat_str=O200K_PATTERN, mergeable_ranks=ranks, special_tokens={}
        )
    except (OSError, ValueError):
        return None


def token_estimate(text: str, *, azure: bool) -> tuple[int, str]:
    encoding = cached_encoding(tokenizer_cache_path()) if azure else None
    if encoding is not None:
        import math

        return math.ceil(len(encoding.encode(text, disallowed_special=())) * 1.1) + 512, "o200k_base-plus-margin"
    return len(text.encode("utf-8")) + 512, "utf8-byte-upper-bound"
