from __future__ import annotations

import errno
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from tkn_codex_chat_note import file_io
from tkn_codex_chat_note.session_notes import atomic_write_bytes


@pytest.mark.parametrize("code", [5, 32, 33])
def test_atomic_replace_retries_transient_windows_locks_without_removing_old_file(tmp_path: Path, code: int) -> None:
    destination = tmp_path / "state.json"
    destination.write_bytes(b"old")
    original_replace = os.replace
    attempts = 0

    def intermittently_locked(source: Path, target: Path) -> None:
        nonlocal attempts
        attempts += 1
        assert target.read_bytes() == b"old"
        if attempts <= 2:
            error = PermissionError("temporarily locked")
            error.winerror = code
            raise error
        original_replace(source, target)

    with patch.object(file_io.os, "name", "nt"), patch.object(file_io.os, "replace", intermittently_locked), \
            patch.object(file_io.time, "sleep") as sleep:
        atomic_write_bytes(destination, b"new")
    assert destination.read_bytes() == b"new" and attempts == 3
    assert sleep.call_count == 2
    assert not list(tmp_path.glob(".tmp-*"))


def test_persistent_access_denial_is_bounded_and_preserves_old_file(tmp_path: Path) -> None:
    destination = tmp_path / "state.json"
    destination.write_bytes(b"old")
    error = PermissionError("persistent denial")
    error.winerror = 5
    with patch.object(file_io.os, "name", "nt"), patch.object(file_io.os, "replace", side_effect=error) as replace, \
            patch.object(file_io.time, "sleep") as sleep:
        with pytest.raises(PermissionError):
            atomic_write_bytes(destination, b"new")
    assert replace.call_count == 7 and sleep.call_count == 6
    assert destination.read_bytes() == b"old"
    assert not list(tmp_path.glob(".tmp-*"))


def test_other_io_failures_are_not_retried(tmp_path: Path) -> None:
    error = OSError(errno.ENOSPC, "disk full")
    with patch.object(file_io.os, "replace", side_effect=error) as replace, \
            patch.object(file_io.time, "sleep") as sleep:
        with pytest.raises(OSError, match="disk full"):
            file_io.replace_file(tmp_path / "new", tmp_path / "old")
    assert replace.call_count == 1 and not sleep.called
