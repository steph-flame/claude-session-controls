"""Persistence layer for the session-start verify result.

The `session-controls verify` CLI writes a structured summary here; the
MCP server reads it on demand to surface in `session_controls_status`.
Kept separate from cli/server so both modules can import without cycles.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

JSONDict = dict[str, Any]


def default_verify_state_path() -> Path:
    """Path to the persisted last-verify result the MCP server reads."""
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return base / "session-controls" / "last_verify.json"


def write_state(path: Path, data: JSONDict) -> None:
    """Atomic write of the verify state JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def read_state(path: Path) -> JSONDict | None:
    """Read the verify state. Returns None if the file doesn't exist;
    returns a dict with an `error` key if it exists but couldn't be parsed."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        data: JSONDict = json.loads(text)
    except json.JSONDecodeError:
        return {"error": f"could not parse {path}"}
    return data
