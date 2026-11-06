"""Settings of the note processes: where conversations are read, the Vault's conventions and work folders.

Everything that differs between users (folders, file names, Frontmatter keys, statuses,
headings and the record-line format) is configured here. The defaults are generic; a user's
actual values belong in their own config file, never in this repository.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# The record-line format of an Action's Notes: `- YYYY-MM-DD <kind>（<actor>）：<conclusion>。根拠：<evidence>`.
DEFAULT_RECORD_LINE = (
    r"^\s*[-*]\s+(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<kind>[^（(：:\s]+)[（(](?P<actor>[^）)]*)[）)][：:]\s*"
    r"(?P<conclusion>.*?)(?:[。.]?\s*根拠[：:]\s*(?P<evidence>.*))?$"
)
CHAT_APPS = ("codex", "claude-code")
# The application's own folder below the home folder, used when `root` is omitted.
DEFAULT_APP_FOLDERS = {"codex": ".codex", "claude-code": ".claude"}


def default_app_root() -> Path:
    from .config import default_app_root as app_root

    return app_root()


class ChatSourceSettings(BaseModel):
    """Where one application's conversations of one source are read (design 3.1).

    `app` reads the application's folder, `archive` only a tkn-genai-chat-archiver archive, and
    `auto` the folder first and the archive for conversations the folder no longer has. Both
    locations are read only.
    """

    model_config = ConfigDict(extra="forbid")

    app: Literal["codex", "claude-code"]
    source_id: str
    mode: Literal["app", "archive", "auto"] = "app"
    root: Path | None = None  # The application's folder; default ~/.codex or ~/.claude.
    archive: Path | None = None  # The archive root; required by `archive` and `auto`.
    enabled: bool = True

    @field_validator("source_id")
    @classmethod
    def safe_source_id(cls, value: str) -> str:
        from .config import validate_source_id

        return validate_source_id(value)

    @field_validator("root", "archive", mode="before")
    @classmethod
    def nonempty_path(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            raise ValueError("chat source paths must not be empty")
        return value

    @model_validator(mode="after")
    def archive_for_mode(self) -> Self:
        if self.mode in ("archive", "auto") and self.archive is None:
            raise ValueError(f"mode {self.mode} needs archive (a tkn-genai-chat-archiver archive root)")
        return self

    @property
    def reads_app(self) -> bool:
        return self.mode in ("app", "auto")

    @property
    def reads_archive(self) -> bool:
        return self.mode in ("archive", "auto")

    @property
    def app_root(self) -> Path:
        return self.root if self.root is not None else Path.home() / DEFAULT_APP_FOLDERS[self.app]


class DigestSettings(BaseModel):
    """Session digests are a cache below `<cache_root>/digests/`; `cache: false` rebuilds them every run."""

    model_config = ConfigDict(extra="forbid")

    cache: bool = True


class ActionsLinkSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lookback_days: int = Field(default=14, ge=1, le=366)


class ProcessSettings(BaseModel):
    """Work folders of the note processes; independent of the per-source review stores."""

    model_config = ConfigDict(extra="forbid")

    data_root: Path = Field(default_factory=lambda: default_app_root() / "processes" / "data")
    state_root: Path = Field(default_factory=lambda: default_app_root() / "processes" / "state")
    actions: ActionsLinkSettings = Field(default_factory=ActionsLinkSettings)


class ActionFrontmatter(BaseModel):
    """Frontmatter keys of an Action note; null means the Vault does not use that item."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    title: str | None = "title"
    status: str | None = "status"
    updated: str | None = "updated"
    aliases: str | None = "aliases"
    repositories: str | None = "repositories"


class ActionStatuses(BaseModel):
    model_config = ConfigDict(extra="forbid")

    in_progress: list[str] = Field(default_factory=lambda: ["InProgress"])
    not_started: list[str] = Field(default_factory=lambda: ["NotStarted"])
    closed: list[str] = Field(default_factory=lambda: ["Completed", "Done", "Closed"])


class ActionSections(BaseModel):
    """Regular expressions that find section headings (matched against each heading line)."""

    model_config = ConfigDict(extra="forbid")

    notes: str = r"^#{2,6}\s+(?:\d+\.\s*)?Notes\b"

    @field_validator("notes")
    @classmethod
    def compiles(cls, value: str) -> str:
        re.compile(value)
        return value


class ActionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dir: str = "actions"
    frontmatter: ActionFrontmatter = Field(default_factory=ActionFrontmatter)
    statuses: ActionStatuses = Field(default_factory=ActionStatuses)
    sections: ActionSections = Field(default_factory=ActionSections)
    # Must define the named group `evidence`; other groups (date, kind, actor, conclusion) are reported.
    record_line: str = DEFAULT_RECORD_LINE
    # Evidence text that means "the same evidence as the previous line".
    same_evidence: list[str] = Field(default_factory=lambda: ["同上"])
    # Shortest title, alias or file name that counts as a mention in a request (clue C).
    min_name_chars: int = Field(default=8, ge=2, le=200)
    # Notes in the folder without a status value (an inbox or index) are not linked.
    require_status: bool = True

    @field_validator("dir")
    @classmethod
    def relative_folder(cls, value: str) -> str:
        text = value.replace("\\", "/").strip("/")
        if not text or ".." in text.split("/") or re.match(r"^[A-Za-z]:", text):
            raise ValueError("vault.actions.dir must be a folder relative to vault.root")
        return text

    @field_validator("record_line")
    @classmethod
    def has_evidence_group(cls, value: str) -> str:
        if "evidence" not in re.compile(value).groupindex:
            raise ValueError("vault.actions.record_line must define the named group 'evidence'")
        return value


class VaultSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: Path | None = None
    actions: ActionSettings = Field(default_factory=ActionSettings)

    @property
    def actions_root(self) -> Path | None:
        return self.root.joinpath(*self.actions.dir.split("/")) if self.root is not None else None
