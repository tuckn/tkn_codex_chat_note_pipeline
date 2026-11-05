"""Settings of the note processes: archives to read, the Vault's conventions and work folders.

Everything that differs between users (folders, file names, Frontmatter keys, statuses,
headings and the record-line format) is configured here. The defaults are generic; a user's
actual values belong in their own config file, never in this repository.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

# The record-line format of an Action's Notes: `- YYYY-MM-DD <kind>（<actor>）：<conclusion>。根拠：<evidence>`.
DEFAULT_RECORD_LINE = (
    r"^\s*[-*]\s+(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<kind>[^（(：:\s]+)[（(](?P<actor>[^）)]*)[）)][：:]\s*"
    r"(?P<conclusion>.*?)(?:[。.]?\s*根拠[：:]\s*(?P<evidence>.*))?$"
)
SUPPORTED_ARCHIVE_PROFILES = ("codex", "claude-code")


def default_app_root() -> Path:
    from .config import default_app_root as app_root

    return app_root()


class ArchiveSettings(BaseModel):
    """One tkn-genai-chat-archiver archive root; its profile and source ID come from its marker."""

    model_config = ConfigDict(extra="forbid")

    root: Path
    enabled: bool = True


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
