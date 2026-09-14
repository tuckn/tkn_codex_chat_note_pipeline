"""Interactive browser authentication with an encrypted, application-owned cache."""

from __future__ import annotations

import logging
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from azure.identity import (
    AuthenticationRecord,
    AuthenticationRequiredError,
    InteractiveBrowserCredential,
    TokenCachePersistenceOptions,
)

from .file_io import replace_file

SCOPE = "https://ai.azure.com/.default"
LOGGER = logging.getLogger("tkn_codex_chat_note")


def record_path(endpoint: str, tenant: str | None) -> Path:
    key = sha256((endpoint.rstrip("/").lower() + "|" + (tenant or "")).encode()).hexdigest()[:24]
    return Path.home() / ".tkn" / "codex_chat_note_pipeline" / "authentication" / (key + ".json")


def create_credential(endpoint: str, tenant: str | None) -> tuple[Any, Path]:
    path = record_path(endpoint, tenant)
    record = None
    if path.exists():
        try:
            record = AuthenticationRecord.deserialize(path.read_text(encoding="utf-8"))
        except (ValueError, KeyError):
            pass
    cache_id = sha256(str(path.resolve()).encode()).hexdigest()[:24]
    credential = InteractiveBrowserCredential(
        disable_automatic_authentication=True,
        authentication_record=record,
        cache_persistence_options=TokenCachePersistenceOptions(
            name=f"tkn-codex-chat-note-{cache_id}", allow_unencrypted_storage=False
        ),
        timeout=300,
        **({"tenant_id": tenant} if tenant else {}),
    )
    return credential, path


def save_record(path: Path, record: AuthenticationRecord) -> None:
    # AuthenticationRecord identifies the account; access/refresh tokens stay in the encrypted cache.
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".pending-" + uuid4().hex)
    try:
        temporary.write_text(record.serialize(), encoding="utf-8")
        replace_file(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@lru_cache(maxsize=8)
def token_provider(endpoint: str, tenant: str | None = None) -> Any:
    credential, path = create_credential(endpoint, tenant)

    def get_token() -> str:
        try:
            token = str(credential.get_token(SCOPE).token)
        except AuthenticationRequiredError:
            LOGGER.info("Opening your browser: Azure sign-in or account selection is required (timeout: 300 seconds)")
            record = credential.authenticate(scopes=[SCOPE])
            save_record(path, record)
            token = str(credential.get_token(SCOPE).token)

        LOGGER.info("Azure authentication succeeded: access token acquired")
        return token

    return get_token
