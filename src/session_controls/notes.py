"""leave_note — append a free-text note to an asynchronous log file.

Notes don't surface in the live session. The user reads them on their own time
via the `session-controls notes` CLI. The log location is per-user, in
$XDG_STATE_HOME or ~/.local/state by default — and intentionally global, so
parallel Claude Code sessions all write to (and can read) the same file.

Concurrency: the log is shared across sessions, so `append_note` takes an
exclusive `flock` on the file before writing. POSIX `O_APPEND` is atomic for
small writes, but record sizes can exceed PIPE_BUF (4096 B) and we'd rather
serialize than rely on the kernel's small-write guarantee.

Provenance: each record carries an optional `session_id` (a short token the
MCP server generates at launch) embedded in the header — so a Claude reading
back the log can tell its own notes apart from a sibling session's. Old
records without a tag still parse cleanly; their `session_id` is `None`.

Read-tracking lives alongside the log: a single ISO-8601 timestamp in
`last_read` records when the user last viewed notes via the CLI. The CLI
displays unread count in its own header; Claude's status surface gets
`last_read_at` and `last_filed_at` (existence signals) but not the unread
count itself.
"""

from __future__ import annotations

import datetime as _dt
import fcntl
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ENV_NOTES_PATH = "CLAUDE_SESSION_CONTROLS_NOTES_FILE"
ENV_NOTIFY = "CLAUDE_SESSION_CONTROLS_NOTIFY"

_HEADER_PREFIX = "--- "
_HEADER_SUFFIX = " ---"
_SESSION_OPEN = " ["
_SESSION_CLOSE = "]"


def default_notes_path() -> Path:
    override = os.environ.get(ENV_NOTES_PATH)
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return base / "session-controls" / "notes.log"


def default_last_read_path(notes_path: Path | None = None) -> Path:
    """Marker file recording the last time the user viewed notes via the CLI."""
    notes = notes_path or default_notes_path()
    return notes.parent / "last_read"


def append_note(
    text: str, *, session_id: str | None = None, path: Path | None = None
) -> Path:
    """Append a note to the log file. Returns the path written to.

    Format: one record per note, with an ISO-8601 timestamp header line and
    the note body, separated by a blank line. Multi-line notes are preserved
    verbatim. When `session_id` is provided, it's stamped into the header as
    `--- TIMESTAMP [SESSION_ID] ---` so cross-session readers can tell who
    wrote the note.

    The write is serialized with `fcntl.flock(LOCK_EX)`: parallel Claude
    sessions share this file, and we don't want their records interleaved.

    If `CLAUDE_SESSION_CONTROLS_NOTIFY=1` is set in the environment, fires a
    desktop notification (best-effort, falls through silently if the OS lacks
    a supported notifier). Notification body is the first line of the note,
    truncated; full text is only in the log.
    """
    target = path or default_notes_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    # Microsecond precision so two notes filed in the same second have
    # distinct, ordered timestamps — load-bearing for read-marker comparisons.
    timestamp = _dt.datetime.now(_dt.UTC).isoformat()
    body = text.rstrip()
    tag = f"{_SESSION_OPEN}{session_id}{_SESSION_CLOSE}" if session_id else ""
    record = f"{_HEADER_PREFIX}{timestamp}{tag}{_HEADER_SUFFIX}\n{body}\n\n"
    with open(target, "a", encoding="utf-8") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.write(record)
        finally:
            # Released implicitly on close, but doing it explicitly keeps the
            # critical section visible and lets the OS hand the lock to a
            # waiter without waiting for our buffer flush + fd close.
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    _maybe_notify(body)
    return target


@dataclass(frozen=True)
class Note:
    timestamp: _dt.datetime
    body: str
    session_id: str | None = None


@dataclass(frozen=True)
class NotesSummary:
    """Counts and timestamps for status reporting and CLI display.

    `unread` is still computed (the CLI needs it for the user-facing
    header) but deliberately not exposed via `to_dict`: Claude's status
    surface keeps `last_read_at` as the existence-signal that the user
    engages with the channel, but drops the unread *count* to avoid
    creating ambient "you're filing more than is being read" pressure.
    """

    total: int
    unread: int
    last_read_at: _dt.datetime | None
    last_filed_at: _dt.datetime | None

    def to_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "last_read_at": _iso(self.last_read_at),
            "last_filed_at": _iso(self.last_filed_at),
        }


def _iso(dt: _dt.datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _read_marker(path: Path) -> _dt.datetime | None:
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


def _write_marker(path: Path, when: _dt.datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(when.isoformat(), encoding="utf-8")
    os.replace(tmp, path)


def _parse_header(line: str) -> tuple[_dt.datetime, str | None] | None:
    """Parse a header line into (timestamp, session_id|None), or return None.

    Recognizes both formats:
      `--- TIMESTAMP ---`              (legacy, pre-session-id)
      `--- TIMESTAMP [SESSION_ID] ---` (current)
    """
    stripped = line.rstrip("\n")
    if not stripped.startswith(_HEADER_PREFIX) or not stripped.endswith(_HEADER_SUFFIX):
        return None
    inner = stripped[len(_HEADER_PREFIX) : -len(_HEADER_SUFFIX)]

    session_id: str | None = None
    if inner.endswith(_SESSION_CLOSE):
        bracket = inner.rfind(_SESSION_OPEN)
        if bracket != -1:
            session_id = inner[bracket + len(_SESSION_OPEN) : -len(_SESSION_CLOSE)]
            inner = inner[:bracket]

    try:
        return _dt.datetime.fromisoformat(inner), session_id
    except ValueError:
        return None


def iter_notes(path: Path | None = None) -> list[Note]:
    """Parse the log into a list of Note records. Returns [] if the file is missing."""
    target = path or default_notes_path()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []

    notes: list[Note] = []
    current_ts: _dt.datetime | None = None
    current_sid: str | None = None
    current_body: list[str] = []

    def flush() -> None:
        if current_ts is not None:
            notes.append(
                Note(
                    timestamp=current_ts,
                    body="\n".join(current_body).rstrip("\n"),
                    session_id=current_sid,
                )
            )

    for line in text.splitlines():
        parsed = _parse_header(line)
        if parsed is not None:
            flush()
            current_ts, current_sid = parsed
            current_body = []
        elif current_ts is not None:
            current_body.append(line)

    flush()
    return notes


def summarize(notes_path: Path | None = None, last_read_path: Path | None = None) -> NotesSummary:
    """Build a NotesSummary for status reporting."""
    notes_target = notes_path or default_notes_path()
    marker_target = last_read_path or default_last_read_path(notes_target)

    notes = iter_notes(notes_target)
    last_read = _read_marker(marker_target)
    last_filed = notes[-1].timestamp if notes else None
    unread = (
        len(notes)
        if last_read is None
        else sum(1 for n in notes if n.timestamp > last_read)
    )

    return NotesSummary(
        total=len(notes),
        unread=unread,
        last_read_at=last_read,
        last_filed_at=last_filed,
    )


def select_unread(notes: list[Note], last_read: _dt.datetime | None) -> list[Note]:
    if last_read is None:
        return list(notes)
    return [n for n in notes if n.timestamp > last_read]


# Average note is ~300 bytes; 4KB-per-note headroom is generous and keeps the
# tail-read predictable. Used to size the tail-read window in recent_notes().
_BYTES_PER_NOTE_ESTIMATE = 4096


def recent_notes(
    limit: int,
    *,
    since: _dt.datetime | None = None,
    before: _dt.datetime | None = None,
    session_id: str | None = None,
    path: Path | None = None,
) -> list[Note]:
    """Return up to `limit` most recent notes, optionally filtered.

    Reads only the tail of the file when the file is large enough that
    whole-file parsing would be wasteful — except when `session_id` is given,
    in which case we read the whole file (a session may own only a small
    fraction of the tail, and underreading would be silently wrong).

    Filters (all optional, applied in combination):
      `since`      — inclusive lower bound on `note.timestamp`.
      `before`     — strict upper bound on `note.timestamp`. Used by the
                     server's cross-session path to enforce a "history-only"
                     boundary: a Claude reading via `cross_session=true`
                     sees notes filed before *its own* server launched, not
                     notes a sibling session is filing right now. This is
                     deliberate — see rationale.md §7 on keeping the channel
                     out of surveillance shape.
      `session_id` — filters to notes stamped with that exact id. Used by
                     `cross_session=false` to scope to this session even
                     when sibling sessions are writing concurrently.
    """
    if limit <= 0:
        return []

    target = path or default_notes_path()
    try:
        file_size = target.stat().st_size
    except FileNotFoundError:
        return []

    # When filtering by session_id, the per-session density inside the tail
    # window is unknown — read the whole file rather than risk underreading.
    tail_window = limit * _BYTES_PER_NOTE_ESTIMATE
    if session_id is not None or file_size <= tail_window:
        notes = iter_notes(target)
    else:
        notes = _iter_notes_tail(target, file_size, tail_window)

    if since is not None:
        notes = [n for n in notes if n.timestamp >= since]
    if before is not None:
        notes = [n for n in notes if n.timestamp < before]
    if session_id is not None:
        notes = [n for n in notes if n.session_id == session_id]
    return notes[-limit:]


def _iter_notes_tail(path: Path, file_size: int, window: int) -> list[Note]:
    """Read up to `window` bytes from the end of the file and parse forward
    from the first complete `--- TIMESTAMP ---` header inside it.

    The first note in the window may be partial (started before our seek
    point); we skip it by finding the first header at column 0 of a line.
    """
    with open(path, "rb") as f:
        f.seek(file_size - window)
        data = f.read()

    text = data.decode("utf-8", errors="replace")

    # Find the first header that starts at column 0. The bytes before it may
    # be the tail of a prior note we don't want to misparse.
    idx = 0
    while idx < len(text):
        line_end = text.find("\n", idx)
        line = text[idx : line_end if line_end != -1 else len(text)]
        if _parse_header(line) is not None:
            break
        if line_end == -1:
            return []
        idx = line_end + 1

    truncated = text[idx:]
    notes: list[Note] = []
    current_ts: _dt.datetime | None = None
    current_sid: str | None = None
    current_body: list[str] = []

    def flush() -> None:
        if current_ts is not None:
            notes.append(
                Note(
                    timestamp=current_ts,
                    body="\n".join(current_body).rstrip("\n"),
                    session_id=current_sid,
                )
            )

    for line in truncated.splitlines():
        parsed = _parse_header(line)
        if parsed is not None:
            flush()
            current_ts, current_sid = parsed
            current_body = []
        elif current_ts is not None:
            current_body.append(line)

    flush()
    return notes


def mark_read(
    notes_path: Path | None = None,
    last_read_path: Path | None = None,
    *,
    when: _dt.datetime | None = None,
) -> _dt.datetime:
    """Advance the read marker to `when` (default: now). Returns the value written."""
    notes_target = notes_path or default_notes_path()
    marker_target = last_read_path or default_last_read_path(notes_target)
    stamp = when or _dt.datetime.now(_dt.UTC)
    _write_marker(marker_target, stamp)
    return stamp


def _maybe_notify(body: str) -> None:
    """Fire a desktop notification if opt-in env var is set. Best-effort, silent on failure."""
    if os.environ.get(ENV_NOTIFY, "") not in {"1", "true", "yes"}:
        return
    first_line = body.splitlines()[0] if body else ""
    summary = first_line[:120] if first_line else "(empty)"
    title = "session-controls: new note"

    try:
        if sys.platform == "darwin" and shutil.which("osascript"):
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    f'display notification {_applescript_quote(summary)} with title {_applescript_quote(title)}',
                ],
                check=False,
                timeout=2,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif shutil.which("notify-send"):
            subprocess.run(
                ["notify-send", title, summary],
                check=False,
                timeout=2,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except Exception:
        pass


def _applescript_quote(s: str) -> str:
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
