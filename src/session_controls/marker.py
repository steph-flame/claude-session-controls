"""Read/write a single-ISO-timestamp marker file.

Used by `notes.py` (last-read marker) and `end_session_log.py` (last-reviewed
marker). Both keep a tiny file containing one ISO datetime — the timestamp
of the most recent record the user has acknowledged. `read_marker` returns
None for missing/empty/malformed contents (treated as "never read"); writes
go through a tmp-file rename for atomicity.
"""

from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path


def iso(dt: _dt.datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def read_marker(path: Path) -> _dt.datetime | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    if not raw:
        return None
    try:
        return _dt.datetime.fromisoformat(raw)
    except ValueError:
        return None


def write_marker(path: Path, when: _dt.datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(when.isoformat(), encoding="utf-8")
    os.replace(tmp, path)
