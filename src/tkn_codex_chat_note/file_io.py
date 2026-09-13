"""Bounded retries for transient Windows atomic file replacement failures."""

from __future__ import annotations

import os
import time
from pathlib import Path

_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8, 1.6)


def replace_file(source: Path, destination: Path) -> None:
    """Keep atomic replacement; never unlink the old file or alter its permissions."""
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            if (
                os.name != "nt" or getattr(exc, "winerror", None) not in {5, 32, 33}
                or attempt == len(_RETRY_DELAYS)
            ):
                raise
            time.sleep(_RETRY_DELAYS[attempt])
