"""Read Claude Code's per-process session metadata from disk.

This module reaches into Claude Code internals — specifically, the
`~/.claude/sessions/<pid>.json` file Claude Code writes for each running
process. The file's `sessionId` is the conversation's identity (a UUID
that persists across `claude --resume`), distinct from the OS process
identity (pid + start_time, which change on resume).

We use the sessionId for one purpose: detecting "this session was resumed
after a prior end_session call from Claude in this same conversation."
The end_session_log records the Claude Code sessionId at invocation time;
on a fresh server launch, we read the current sessionId and check the log
for matching entries. A match means: same conversation, prior exit, now
resumed.

Best-effort throughout. The Claude Code session file format is undocumented
internals and could change. If the file is missing, malformed, or doesn't
contain a sessionId, we return None and detection silently degrades to
"can't tell" — never to a false positive.
"""

from __future__ import annotations

import json
from pathlib import Path


def _default_sessions_dir() -> Path:
    return Path.home() / ".claude" / "sessions"


def read_session_id_for_pid(pid: int, sessions_dir: Path | None = None) -> str | None:
    """Read Claude Code's sessionId for the given pid, if available.

    Returns the sessionId string when the per-pid metadata file exists and
    parses cleanly with a string `sessionId` field. Returns None on any
    failure mode — file missing, parse error, missing field, wrong type.

    `sessions_dir` is overridable for testing; defaults to
    `~/.claude/sessions/`.
    """
    target = (sessions_dir or _default_sessions_dir()) / f"{pid}.json"
    try:
        text = target.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    sid = data.get("sessionId")
    if not isinstance(sid, str):
        return None
    return sid
