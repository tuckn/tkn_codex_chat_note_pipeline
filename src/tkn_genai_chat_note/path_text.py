"""Compare folder paths that appear in logs, settings and commands as plain text.

Logs write the same folder in many shapes: `C:\\x`, `C:/x`, `file:///C:/x`, `\\\\?\\C:\\x`,
`/c/x` (Git Bash), `/mnt/c/x` (WSL) and, through junctions or drive letters, another root
altogether. `PathText` turns any of them into one comparable lower-case form and applies the
configured `path_aliases` (the first prefix of a group is the canonical one).
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import unquote

_FILE_URL = re.compile(r"file:///+([^\s\"'<>|]+)", re.IGNORECASE)
_SLASHES = re.compile(r"/{2,}")
_GIT_BASH_DRIVE = re.compile(r"(^|[\s\"'=(;,])/(?:mnt/)?([a-z])/")


def _base(value: str) -> str:
    text = unicodedata.normalize("NFC", value)
    text = _FILE_URL.sub(lambda match: unquote(match.group(1)), text)
    text = text.replace("\\", "/")
    text = _SLASHES.sub("/", text)
    text = text.replace("/?/unc/", "/").replace("/?/UNC/", "/").replace("/?/", "")
    text = text.casefold()
    return _GIT_BASH_DRIVE.sub(lambda match: f"{match.group(1)}{match.group(2)}:/", text)


class PathText:
    """Normalize path-bearing text; aliases are replaced wherever they occur."""

    def __init__(self, aliases: list[list[str]] | None = None) -> None:
        self._aliases: list[tuple[str, str]] = []
        for group in aliases or []:
            canonical = _base(group[0]).rstrip("/")
            for alias in group[1:]:
                normalized = _base(alias).rstrip("/")
                if normalized and normalized != canonical:
                    self._aliases.append((normalized, canonical))
        # Longer prefixes first, so a nested alias wins over its parent.
        self._aliases.sort(key=lambda item: len(item[0]), reverse=True)

    def text(self, value: str) -> str:
        result = _base(value)
        for alias, canonical in self._aliases:
            result = re.sub(re.escape(alias) + r"(?=/|$|[\s\"'])", canonical, result)
        return result

    def folder(self, value: str) -> str:
        return self.text(value).rstrip("/")

    def is_within(self, path: str, folder: str) -> bool:
        child, parent = self.folder(path), self.folder(folder)
        return bool(parent) and (child == parent or child.startswith(parent + "/"))


def last_folder_name(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1] if path else ""
