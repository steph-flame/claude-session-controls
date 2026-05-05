"""end_session invocation log — append-only record of invocations.

Mirrors notes.py in shape but stores JSONL instead of free-text records: each
end_session call that fires (excluding refusals and dry runs) appends one
JSON object to the log. The log is global per user — parallel sessions all
write to the same file — and the path layout matches the notes log
(`$XDG_STATE_HOME/session-controls/end_session_log.jsonl`).

The log exists so the user can review past invocations on their own time. The
review commitment is the user's, not Claude's: there's no `unread` count
surfaced to Claude, only `last_reviewed_at` (the existence-signal that the
user does engage with the channel). Unread counts surface to the user via
the SessionStart hook output, separately.

Concurrency: parallel sessions share the file, so `append_invocation` takes
an exclusive `flock` around the write. Each record is one line; the lock
keeps records from interleaving even when records grow past PIPE_BUF.

Record schema (subject to additive evolution; readers tolerate unknown
fields):

    timestamp           ISO-8601 UTC, microsecond precision.
    session_id          The MCP server's per-launch token. Stable for the
                        life of one session; differs across parallel sessions.
    cwd                 Working directory of the MCP server at invocation
                        time (inherited from Claude Code).
    repo                Absolute path to the nearest enclosing .git root,
                        or null if cwd isn't inside a repo.
    confidence          Gate state at invocation. Always "HIGH" in new
                        entries — LOW/INVALID refuse and never reach the
                        log. Pre-Decision-10 entries may also have
                        "MEDIUM"; readers should treat those as historical.
    descendants_count   Length of the descendants list at invocation time.
    selftest            True iff the entry was written by `install --rehearse`
                        rather than a real invocation. The review CLI labels
                        these distinctly so first-touch is exercise, not
                        history.
    note                Optional free text passed via end_session's `note`
                        parameter. The same text is also filed via
                        `leave_note` (so the user sees it via
                        `session-controls notes`); the copy here is for
                        Claude reading the invocation log via
                        `read_end_session_log` — it gives context inline
                        rather than requiring a cross-log lookup. Null
                        when no note was passed.
    claude_code_session_id  Claude Code's conversation-identity UUID at
                        invocation time (read from
                        `~/.claude/sessions/<pid>.json`'s `sessionId`).
                        Persists across `claude --resume`, so a fresh
                        server launch can match against this field to
                        detect "this conversation was resumed after a
                        prior end_session call from Claude." Null when
                        the file wasn't readable. Optional field, absent
                        from pre-Decision-13 entries.
"""

from __future__ import annotations

import datetime as _dt
import fcntl
import json
import os
from dataclasses import dataclass
from pathlib import Path

from session_controls.marker import iso, read_marker, write_marker

ENV_LOG_PATH = "CLAUDE_SESSION_CONTROLS_END_SESSION_LOG"


def default_end_session_log_path() -> Path:
    override = os.environ.get(ENV_LOG_PATH)
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return base / "session-controls" / "end_session_log.jsonl"


def default_last_reviewed_path(log_path: Path | None = None) -> Path:
    """Marker file recording when the user last ran the review CLI."""
    log = log_path or default_end_session_log_path()
    return log.parent / "end_session_log.last_reviewed"


def detect_repo_root(cwd: Path) -> Path | None:
    """Walk up from cwd looking for a directory containing `.git`.

    Returns the absolute path of the repo root, or None if cwd isn't inside
    a git repository. Pure pathlib — no subprocess.
    """
    current = cwd.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def append_invocation(
    *,
    session_id: str | None,
    cwd: Path | None = None,
    confidence: str,
    descendants_count: int,
    selftest: bool = False,
    note: str | None = None,
    claude_code_session_id: str | None = None,
    path: Path | None = None,
) -> Path:
    """Append one invocation record to the log. Returns the path written to.

    `cwd` defaults to the current process's cwd. `repo` is detected from `cwd`.
    `note` is optional free text — same content as the leave_note that gets
    filed in parallel; the copy here makes it visible inline when Claude
    reads the invocation log via `read_end_session_log`.

    The write is serialized with an exclusive flock — parallel sessions
    share this file.
    """
    target = path or default_end_session_log_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    cwd_path = (cwd or Path.cwd()).resolve()
    repo_root = detect_repo_root(cwd_path)

    record = {
        "timestamp": _dt.datetime.now(_dt.UTC).isoformat(),
        "session_id": session_id,
        "cwd": str(cwd_path),
        "repo": str(repo_root) if repo_root is not None else None,
        "confidence": confidence,
        "descendants_count": descendants_count,
        "selftest": selftest,
        "note": note,
        "claude_code_session_id": claude_code_session_id,
    }
    line = json.dumps(record, ensure_ascii=False) + "\n"

    with open(target, "a", encoding="utf-8") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.write(line)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return target


@dataclass(frozen=True)
class Invocation:
    timestamp: _dt.datetime
    session_id: str | None
    cwd: str | None
    repo: str | None
    confidence: str | None
    descendants_count: int
    selftest: bool
    note: str | None = None
    claude_code_session_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "session_id": self.session_id,
            "cwd": self.cwd,
            "repo": self.repo,
            "confidence": self.confidence,
            "descendants_count": self.descendants_count,
            "selftest": self.selftest,
            "note": self.note,
            "claude_code_session_id": self.claude_code_session_id,
        }


@dataclass(frozen=True)
class EndSessionLogSummary:
    """Cheap-to-compute summary for status reporting.

    No `unread` count: the user-side commitment surfaces unread via the
    SessionStart hook, not via Claude's status. `last_reviewed_at` is kept
    as the existence-signal that the user engages with the channel.
    """

    total: int
    last_reviewed_at: _dt.datetime | None
    last_invoked_at: _dt.datetime | None

    def to_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "last_reviewed_at": iso(self.last_reviewed_at),
            "last_invoked_at": iso(self.last_invoked_at),
        }


def _parse_record(line: str) -> Invocation | None:
    """Parse one JSONL line into an Invocation. Returns None for malformed lines.

    Tolerates missing fields (returns sensible defaults) so a record written
    by a future schema-extended version still parses on an older reader.
    """
    line = line.strip()
    if not line:
        return None
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    ts_raw = data.get("timestamp")
    if not isinstance(ts_raw, str):
        return None
    try:
        ts = _dt.datetime.fromisoformat(ts_raw)
    except ValueError:
        return None
    return Invocation(
        timestamp=ts,
        session_id=_opt_str(data.get("session_id")),
        cwd=_opt_str(data.get("cwd")),
        repo=_opt_str(data.get("repo")),
        confidence=_opt_str(data.get("confidence")),
        descendants_count=int(data.get("descendants_count", 0) or 0),
        selftest=bool(data.get("selftest", False)),
        note=_opt_str(data.get("note")),
        claude_code_session_id=_opt_str(data.get("claude_code_session_id")),
    )


def _opt_str(v: object) -> str | None:
    if v is None:
        return None
    return str(v)


def iter_invocations(path: Path | None = None) -> list[Invocation]:
    """Parse the log into a list of Invocation records. Returns [] if missing."""
    target = path or default_end_session_log_path()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    invocations: list[Invocation] = []
    for line in text.splitlines():
        rec = _parse_record(line)
        if rec is not None:
            invocations.append(rec)
    return invocations


def summarize(
    log_path: Path | None = None, last_reviewed_path: Path | None = None
) -> EndSessionLogSummary:
    log_target = log_path or default_end_session_log_path()
    marker_target = last_reviewed_path or default_last_reviewed_path(log_target)

    invocations = iter_invocations(log_target)
    last_reviewed = read_marker(marker_target)
    last_invoked = invocations[-1].timestamp if invocations else None

    return EndSessionLogSummary(
        total=len(invocations),
        last_reviewed_at=last_reviewed,
        last_invoked_at=last_invoked,
    )


def count_unreviewed(log_path: Path | None = None, last_reviewed_path: Path | None = None) -> int:
    """Internal helper for the SessionStart hook output. Not surfaced to Claude.

    Returns the number of invocations newer than the last_reviewed marker
    (all of them if the marker is missing).
    """
    log_target = log_path or default_end_session_log_path()
    marker_target = last_reviewed_path or default_last_reviewed_path(log_target)
    invocations = iter_invocations(log_target)
    last_reviewed = read_marker(marker_target)
    if last_reviewed is None:
        return len(invocations)
    return sum(1 for inv in invocations if inv.timestamp > last_reviewed)


def select_unreviewed(
    invocations: list[Invocation], last_reviewed: _dt.datetime | None
) -> list[Invocation]:
    if last_reviewed is None:
        return list(invocations)
    return [inv for inv in invocations if inv.timestamp > last_reviewed]


def select_invocations(
    limit: int,
    *,
    since: _dt.datetime | None = None,
    before: _dt.datetime | None = None,
    session_id: str | None = None,
    path: Path | None = None,
) -> list[Invocation]:
    """Return up to `limit` most recent invocations, optionally filtered.

    Filter semantics mirror `notes.select_notes`: `since` is inclusive lower
    bound, `before` is strict upper bound (used by the cross_session path to
    bound to history-only), `session_id` filters to a specific session's
    records.

    The log is small (≤1 record per session, typically), so we always
    parse the whole file rather than tail-reading. Simpler and the cost
    is bounded.
    """
    if limit <= 0:
        return []
    invocations = iter_invocations(path)
    if since is not None:
        invocations = [inv for inv in invocations if inv.timestamp >= since]
    if before is not None:
        invocations = [inv for inv in invocations if inv.timestamp < before]
    if session_id is not None:
        invocations = [inv for inv in invocations if inv.session_id == session_id]
    return invocations[-limit:]


def mark_reviewed(
    log_path: Path | None = None,
    last_reviewed_path: Path | None = None,
    *,
    when: _dt.datetime | None = None,
) -> _dt.datetime:
    """Advance the last_reviewed marker to `when` (default: now)."""
    log_target = log_path or default_end_session_log_path()
    marker_target = last_reviewed_path or default_last_reviewed_path(log_target)
    stamp = when or _dt.datetime.now(_dt.UTC)
    write_marker(marker_target, stamp)
    return stamp
