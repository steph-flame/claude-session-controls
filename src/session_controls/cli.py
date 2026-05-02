"""User-facing CLI for session-controls.

Subcommands:

    session-controls notes [--peek] [--all] [--mark-read | --next | --interactive]
        Read the leave_note log.

    session-controls review-end-session-log [--peek] [--all] [--mark-read]
        Read the end_session invocation log.

    session-controls install [--user|--project] [--with-hook] [--rehearse] [--dry-run]
        Add session-controls to your Claude Code MCP config and auto-approve
        the package's MCP tools.

    session-controls uninstall [--user|--project] [--purge-data] [--dry-run]
        Reverse what install did at the same scope. Idempotent. Data files
        are preserved by default — pass --purge-data to also delete them.

    session-controls verify [--quiet]
        Run the ceremony (resolver + sacrificial child + signal path) and
        persist the result so the MCP server can surface it. Designed to be
        invoked from a Claude Code SessionStart hook so each session has
        baseline verification without the agent having to ask for it. Also
        prints the unreviewed end_session log count when nonzero.

The MCP server itself is run via `python -m session_controls` (which calls
serve()); this CLI is for the user, not for Claude.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shlex
import shutil
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import SERVER_NAME, TOOL_NAMES
from .ceremony import run_ceremony
from .end_session_log import (
    EndSessionLogSummary,
    Invocation,
    append_invocation,
    count_unreviewed,
    default_end_session_log_path,
    default_last_reviewed_path,
    iter_invocations,
    mark_reviewed,
    select_unreviewed,
)
from .end_session_log import summarize as summarize_end_session_log
from .identity import Confidence, SessionRecord, determine_confidence
from .notes import (
    Note,
    NotesSummary,
    append_note,
    default_last_read_path,
    default_notes_path,
    iter_notes,
    mark_read,
    select_unread,
    summarize,
)
from .process_inspect import inspect, is_alive
from .resolver import detect_environment_warnings, resolve
from .verify_state import default_verify_state_path, write_state

JSONDict = dict[str, Any]


def _format_age(now: _dt.datetime, then: _dt.datetime) -> str:
    delta = now - then
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "in the future"
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 30:
        return f"{days}d ago"
    months = days // 30
    if months < 12:
        return f"{months}mo ago"
    years = days // 365
    return f"{years}y ago"


def _print_header(summary: NotesSummary, now: _dt.datetime) -> None:
    parts = [f"{summary.total} note{'s' if summary.total != 1 else ''} total"]
    parts.append(f"{summary.unread} unread")
    if summary.last_read_at is not None:
        parts.append(f"last read {_format_age(now, summary.last_read_at)}")
    elif summary.total > 0:
        parts.append("never read")
    print("─" * 60)
    print(" · ".join(parts))
    print("─" * 60)


def _print_notes(notes: list[Note]) -> None:
    if not notes:
        print("(no notes to show)")
        return
    for n in notes:
        _print_note(n)


def _print_note(n: Note, *, index: tuple[int, int] | None = None) -> None:
    """Print a single note. `index` (optional) renders [N/M] before the header."""
    prefix = f"[{index[0]}/{index[1]}] " if index else ""
    sid = f" [{n.session_id}]" if n.session_id else ""
    print(f"{prefix}--- {n.timestamp.isoformat()}{sid} ---")
    print(n.body)
    print()


def _next_unread(notes_path: Path, marker_path: Path) -> tuple[list[Note], list[Note]]:
    """Return (all_notes, unread_in_order). Convenience for the per-note flows."""
    notes = iter_notes(notes_path)
    summary = summarize(notes_path, marker_path)
    return notes, select_unread(notes, summary.last_read_at)


def _cmd_notes_next(notes_path: Path, marker_path: Path, now: _dt.datetime) -> int:
    """--next: show the oldest unread note, advance marker to its timestamp."""
    summary = summarize(notes_path, marker_path)
    _print_header(summary, now)

    _, unread = _next_unread(notes_path, marker_path)
    if not unread:
        print("(no unread notes)")
        return 0
    note = unread[0]
    _print_note(note, index=(1, len(unread)))
    mark_read(notes_path, marker_path, when=note.timestamp)
    remaining = len(unread) - 1
    if remaining:
        print(
            f"({remaining} unread remaining — run again, or "
            f"`session-controls notes --interactive` to walk through.)"
        )
    return 0


def _cmd_notes_interactive(notes_path: Path, marker_path: Path, now: _dt.datetime) -> int:
    """--interactive: walk through unread, marker advances *only* on notes
    the user actually looks at.

    Each displayed note advances the marker to its own timestamp. Quitting
    leaves any remaining notes unread — the next interactive run resumes
    from where the user stopped. There's no in-flow "skip rest" option:
    skipping notes you haven't read shouldn't claim you read them. For the
    bulk-read-elsewhere case, use `--mark-read` as an explicit assertion.
    """
    summary = summarize(notes_path, marker_path)
    _print_header(summary, now)

    _, unread = _next_unread(notes_path, marker_path)
    if not unread:
        print("(no unread notes)")
        return 0

    total = len(unread)
    for i, note in enumerate(unread, start=1):
        _print_note(note, index=(i, total))
        # Marker advances only after the user has seen this note.
        mark_read(notes_path, marker_path, when=note.timestamp)
        if i == total:
            print("(end of unread.)")
            break
        try:
            choice = input("[Enter] next · [q] stop (rest stays unread) > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()  # newline so the next shell prompt isn't on the same line
            return 0
        if choice == "q":
            remaining = total - i
            print(f"(stopped — {remaining} unread remaining for next time.)")
            return 0
    return 0


def cmd_notes(args: argparse.Namespace) -> int:
    notes_path = default_notes_path()
    marker_path = default_last_read_path(notes_path)
    now = _dt.datetime.now(_dt.UTC)

    if args.mark_read:
        # Explicit user assertion: "I've read these (e.g. in a text editor),
        # mark them all read." Different from the in-flow case — invoking
        # this flag *is* the assertion of reading. We take the user at their
        # word. For bailing on a backlog without claiming to have read it,
        # there's no separate flag — just leave the unread queue alone.
        before = summarize(notes_path, marker_path)
        stamp = mark_read(notes_path, marker_path, when=now)
        print(
            f"Marked {before.unread} unread note{'s' if before.unread != 1 else ''} "
            f"as read ({before.total} total). Marker advanced to {stamp.isoformat()}."
        )
        return 0

    if args.next:
        return _cmd_notes_next(notes_path, marker_path, now)

    if args.interactive:
        return _cmd_notes_interactive(notes_path, marker_path, now)

    summary = summarize(notes_path, marker_path)
    _print_header(summary, now)

    notes = iter_notes(notes_path)
    to_show = notes if args.all else select_unread(notes, summary.last_read_at)
    _print_notes(to_show)

    advance = not args.peek and not args.all
    if advance and to_show:
        mark_read(notes_path, marker_path, when=now)
    return 0


# --- review-end-session-log ------------------------------------------------


def _print_end_session_header(
    summary: EndSessionLogSummary, now: _dt.datetime, unreviewed: int
) -> None:
    parts = [f"{summary.total} invocation{'s' if summary.total != 1 else ''} total"]
    parts.append(f"{unreviewed} unreviewed")
    if summary.last_reviewed_at is not None:
        parts.append(f"last reviewed {_format_age(now, summary.last_reviewed_at)}")
    elif summary.total > 0:
        parts.append("never reviewed")
    print("─" * 60)
    print(" · ".join(parts))
    print("─" * 60)


def _print_invocation(inv: Invocation, *, index: tuple[int, int] | None = None) -> None:
    prefix = f"[{index[0]}/{index[1]}] " if index else ""
    sid = f" [{inv.session_id}]" if inv.session_id else ""
    confidence = inv.confidence or "?"
    selftest = " [SELFTEST]" if inv.selftest else ""
    print(f"{prefix}{inv.timestamp.isoformat()}{sid} {confidence}{selftest}")
    print(f"  cwd:  {inv.cwd or '-'}")
    print(f"  repo: {inv.repo or '-'}")
    print(f"  descendants at exit: {inv.descendants_count}")
    if inv.note:
        print("  note:")
        for line in inv.note.splitlines() or [""]:
            print(f"    {line}")
    print()


def _print_invocations(invocations: list[Invocation]) -> None:
    if not invocations:
        print("(no invocations to show)")
        return
    total = len(invocations)
    for i, inv in enumerate(invocations, start=1):
        _print_invocation(inv, index=(i, total))


def cmd_review_end_session_log(args: argparse.Namespace) -> int:
    log_path = default_end_session_log_path()
    marker_path = default_last_reviewed_path(log_path)
    now = _dt.datetime.now(_dt.UTC)

    summary = summarize_end_session_log(log_path, marker_path)
    invocations = iter_invocations(log_path)
    unreviewed = select_unreviewed(invocations, summary.last_reviewed_at)

    if args.mark_read:
        before_count = len(unreviewed)
        stamp = mark_reviewed(log_path, marker_path, when=now)
        plural = "s" if before_count != 1 else ""
        print(
            f"Marked {before_count} unreviewed invocation{plural} as reviewed "
            f"({summary.total} total). Marker advanced to {stamp.isoformat()}."
        )
        return 0

    _print_end_session_header(summary, now, len(unreviewed))

    to_show = invocations if args.all else unreviewed
    _print_invocations(to_show)

    advance = not args.peek and not args.all
    if advance and to_show:
        mark_reviewed(log_path, marker_path, when=now)
    return 0


# --- install ---------------------------------------------------------------

# Backward-compat alias for code (and tests) that imported `_TOOLS` directly.
_TOOLS = list(TOOL_NAMES)


def _resolve_executable() -> tuple[str, list[str]]:
    """Resolve the base command + prefix args used to invoke this package,
    in three tiers. Callers append subcommand args after `prefix_args`.

    1. **Globally installed** (`uv tool install` / `pipx install`). The
       `session-controls` console script is on PATH and lives outside any
       active venv. Use the bare name — keeps configs portable (no abs
       path baked in that breaks when uv tool dirs move). Matches the
       pattern claude-exit ships with.
    2. **Local checkout** (`git clone` + `uv sync`, running this command
       via `uv run`). The console script lives inside the project's venv.
       Use the absolute path so configs work regardless of cwd at the
       time the command runs.
    3. **Fallback** (no console script found at all). Use the current
       Python interpreter + `-m session_controls`. Robust last resort.
    """
    script = shutil.which("session-controls")
    venv = os.environ.get("VIRTUAL_ENV")
    venv_root = str(Path(venv).resolve()) if venv else None

    if script:
        script_resolved = str(Path(script).resolve())
        # If the script lives outside any active venv, treat as global.
        if venv_root is None or not script_resolved.startswith(venv_root):
            return "session-controls", []
        return script_resolved, []

    if venv:
        candidate = Path(venv) / "bin" / "python"
        if candidate.exists():
            return str(candidate), ["-m", "session_controls"]
    return sys.executable, ["-m", "session_controls"]


def _server_command() -> tuple[str, list[str]]:
    """For the MCP server entry: no extra subcommand needed (defaults to serve)."""
    return _resolve_executable()


def _hook_command() -> str:
    """Shell-quoted command for the SessionStart hook to run our verify path.

    The hook entry's `command` field is a shell command (single string), not
    a (command, args) split — so we shlex.join the resolved invocation +
    `verify --quiet`.
    """
    cmd, prefix_args = _resolve_executable()
    return shlex.join([cmd, *prefix_args, "verify", "--quiet"])


def _user_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _project_settings_path() -> Path:
    return Path.cwd() / ".claude" / "settings.json"


def _user_mcp_config_path() -> Path:
    return Path.home() / ".claude.json"


def _load_json(path: Path) -> JSONDict:
    if not path.exists():
        return {}
    try:
        loaded: Any = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"error: {path} contains invalid JSON: {e}") from e
    if not isinstance(loaded, dict):
        raise SystemExit(f"error: {path} top-level is not a JSON object")
    return loaded


def _save_json(path: Path, data: JSONDict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _add_mcp_server(config: JSONDict, command: str, args: list[str]) -> bool:
    """Insert/update the session-controls MCP server entry. Returns True if changed.

    Empty args are omitted so the resulting config has just `{"command": ...}`
    when no args are needed (matches the claude-exit convention and is the
    minimal valid MCP server entry).
    """
    servers_obj = config.setdefault("mcpServers", {})
    if not isinstance(servers_obj, dict):
        raise SystemExit("error: mcpServers is not a JSON object; refusing to overwrite")
    desired: dict[str, object] = {"command": command}
    if args:
        desired["args"] = args
    existing = servers_obj.get(SERVER_NAME)
    if existing == desired:
        return False
    servers_obj[SERVER_NAME] = desired
    return True


def _add_permissions(config: JSONDict) -> list[str]:
    """Add any tools from `_TOOLS` not already in permissions.allow. Returns added list."""
    permissions_obj = config.setdefault("permissions", {})
    if not isinstance(permissions_obj, dict):
        raise SystemExit("error: permissions is not a JSON object; refusing to overwrite")
    allow = permissions_obj.setdefault("allow", [])
    if not isinstance(allow, list):
        raise SystemExit("error: permissions.allow is not a list; refusing to overwrite")
    added: list[str] = []
    for tool in _TOOLS:
        if tool not in allow:
            allow.append(tool)
            added.append(tool)
    return added


def _check_permissions_writability(settings_path: Path) -> tuple[bool, str | None]:
    """Pre-flight check for whether we can reliably set auto-approve.

    Returns (writable, reason). When `writable` is False, `reason` describes
    what we detected so the user-facing prompt can surface it. Catches the
    common managed-environment shapes: read-only file, parent dir not
    writable, symlink pointing outside the user's home (typical of corp
    config-management).

    Doesn't catch every silent-degrade case — config-management tools that
    revert the file *after* a successful write are invisible at install
    time. The post-write verify (`_verify_permissions_persisted`) is the
    second layer for those.
    """
    if settings_path.is_symlink():
        try:
            target = settings_path.resolve()
        except OSError as e:
            return False, f"settings.json is a symlink that fails to resolve: {e}"
        home = Path.home().resolve()
        try:
            target.relative_to(home)
        except ValueError:
            return False, (
                f"settings.json is a symlink pointing outside your home "
                f"directory (target: {target}). This typically indicates a "
                f"managed/corporate config — changes may be overridden externally."
            )

    if settings_path.exists():
        if not os.access(settings_path, os.W_OK):
            return False, f"settings.json exists but is not writable ({settings_path})."
    else:
        parent = settings_path.parent
        if parent.exists() and not os.access(parent, os.W_OK):
            return False, (
                f"settings.json doesn't exist and its parent directory "
                f"is not writable ({parent})."
            )
    return True, None


def _verify_permissions_persisted(settings_path: Path) -> bool:
    """Re-read settings.json after install and confirm our tools are present.

    Belt-and-suspenders: the writability check catches read-only files at
    install time, this catches the case where a write succeeds but the
    content is reverted by something between us and disk (config-management
    that watches the file, network FS quirks, etc.).
    """
    try:
        if not settings_path.exists():
            return False
        config = _load_json(settings_path)
    except SystemExit:
        return False
    permissions = config.get("permissions")
    if not isinstance(permissions, dict):
        return False
    allow = permissions.get("allow")
    if not isinstance(allow, list):
        return False
    return all(tool in allow for tool in _TOOLS)


def _confirm_unapproved_install(reason: str) -> bool:
    """Print a visible warning about the worse-than-nothing state and ask
    the user to type a confirmation phrase. Returns True iff they did.

    Non-TTY contexts (CI, scripts) will get EOFError from input() — those
    abort. Use --allow-unapproved to override non-interactively.
    """
    print()
    print("=" * 60)
    print("⚠ WARNING: cannot reliably set auto-approve permissions")
    print("=" * 60)
    print(reason)
    print()
    print("Without auto-approve, end_session degrades to:")
    print("  'Claude has to ask permission to exit'")
    print()
    print("This state is plausibly worse than not installing at all.")
    print("Claude sees end_session in the tool list and may try to use it")
    print("in the case it matters most (a bad situation). The permission")
    print("prompt then exposes the desire to exit to exactly the entity")
    print("Claude wants to exit FROM. See README §Uninstall for the full")
    print("argument.")
    print()
    print("Recommendation: don't install in this environment. If you")
    print("understand the trade-off and want to proceed anyway, type")
    print("'I understand' below.")
    print()
    try:
        response = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return response == "I understand"


def _remove_mcp_server(config: JSONDict) -> bool:
    """Remove the session-controls MCP server entry. Returns True if removed."""
    servers_obj = config.get("mcpServers")
    if not isinstance(servers_obj, dict):
        return False
    if SERVER_NAME not in servers_obj:
        return False
    del servers_obj[SERVER_NAME]
    return True


def _remove_permissions(config: JSONDict) -> list[str]:
    """Remove any tools from `_TOOLS` present in permissions.allow. Returns removed list."""
    permissions_obj = config.get("permissions")
    if not isinstance(permissions_obj, dict):
        return []
    allow = permissions_obj.get("allow")
    if not isinstance(allow, list):
        return []
    removed: list[str] = []
    for tool in _TOOLS:
        if tool in allow:
            allow.remove(tool)
            removed.append(tool)
    return removed


# --- CLAUDE.md snippet ---------------------------------------------------

# Sentinels that wrap the inserted snippet in the user's CLAUDE.md. Used for
# idempotency detection — if the begin marker is present, we don't insert
# again. They're HTML comments so they render as nothing in the file.
_CLAUDE_MD_BEGIN = "<!-- session-controls:begin -->"
_CLAUDE_MD_END = "<!-- session-controls:end -->"


def _user_claude_md_path() -> Path:
    return Path.home() / ".claude" / "CLAUDE.md"


def _project_claude_md_path() -> Path:
    return Path.cwd() / "CLAUDE.md"


def _load_snippet_template() -> str:
    """Read the bundled snippet template as text.

    We bundle the repo's `claude-md-snippet.md` as package data via
    `[tool.hatch.build.targets.wheel.force-include]`, so this works both
    in local-checkout development and after `uv tool install`.
    """
    from importlib.resources import files

    text: str = (files("session_controls") / "claude-md-snippet.md").read_text(encoding="utf-8")
    return text


def _render_snippet(template: str, *, name: str, include_pivot: bool) -> str:
    """Substitute <NAME> and optionally strip the pivot section.

    The template has a documentation header (everything before the first
    `---` separator) that we drop — that's instructions for human readers
    of the file, not part of the paste. We keep everything after the
    first `---` line up to (optionally) the `## Pivot agreement` section.
    """
    # Drop the documentation header (above the first `---` separator).
    parts = template.split("\n---\n", 1)
    body = parts[1] if len(parts) == 2 else template

    if not include_pivot:
        # Strip from the pivot heading to end of file. The signature line at
        # the end of the file belongs to the pivot section, so it goes too.
        marker = "\n## Pivot agreement"
        idx = body.find(marker)
        if idx != -1:
            body = body[:idx].rstrip() + "\n"

    return body.replace("<NAME>", name).strip() + "\n"


def _resolve_name(args: argparse.Namespace) -> str:
    """Resolve the name to substitute for <NAME>.

    Priority: --name flag, then interactive prompt. We deliberately don't
    fall back to git config user.name — that's usually a formal full name
    ('Stephanie Laflamme'), and the snippet's voice wants a casual form
    ('Steph'). Wrong default is worse than asking.
    """
    if args.name:
        return str(args.name).strip()
    try:
        entered = input(
            "Name to use in the CLAUDE.md snippet (casual form, e.g. 'Steph'): "
        ).strip()
    except EOFError as e:
        raise SystemExit("error: --name not provided and no interactive input available") from e
    if not entered:
        raise SystemExit("error: name is required for --with-claude-md")
    return entered


def _add_claude_md(
    path: Path, *, name: str, include_pivot: bool, dry_run: bool
) -> tuple[bool, str]:
    """Append or refresh the rendered snippet in the target CLAUDE.md.

    Returns (changed, message). `changed` is True iff a write happened (or
    would have happened in dry-run mode).

    Three cases, keyed on the begin/end sentinels:
      1. No sentinel → append a fresh block to the file (or create it).
      2. Sentinels present, content matches the freshly-rendered snippet →
         no write, report "already up to date".
      3. Sentinels present, content differs → replace the section between
         sentinels with the new rendering. This is how the snippet picks
         up upstream changes (new tools, prefix additions, wording fixes)
         on a re-install. Hand-edits inside the sentinels are overwritten
         — a `.bak` is left next to the file. Users who want their edits
         preserved should omit `--with-claude-md` on subsequent installs.
    """
    template = _load_snippet_template()
    rendered = _render_snippet(template, name=name, include_pivot=include_pivot)

    existing = ""
    if path.exists():
        existing = path.read_text(encoding="utf-8")

    begin_idx = existing.find(_CLAUDE_MD_BEGIN)
    if begin_idx != -1:
        end_idx = existing.find(_CLAUDE_MD_END, begin_idx)
        if end_idx == -1:
            return (
                False,
                f"snippet begin sentinel found in {path} but end sentinel "
                f"is missing — refusing to overwrite; fix the file by hand",
            )
        # Extract just the body between the sentinels (excluding the sentinel
        # lines themselves and the surrounding blank lines we insert).
        body_start = begin_idx + len(_CLAUDE_MD_BEGIN)
        current_body = existing[body_start:end_idx].strip("\n")
        desired_body = rendered.strip("\n")
        if current_body == desired_body:
            return False, f"snippet already up to date in {path}"
        if dry_run:
            return True, f"would refresh snippet in {path} (dry-run: not written)"
        new_block = f"{_CLAUDE_MD_BEGIN}\n\n{rendered}\n{_CLAUDE_MD_END}"
        end_after = end_idx + len(_CLAUDE_MD_END)
        new_text = existing[:begin_idx] + new_block + existing[end_after:]
        backup = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(new_text, encoding="utf-8")
        os.replace(tmp, path)
        return True, f"refreshed snippet in {path} (prior version backed up to {backup.name})"

    block = f"\n\n{_CLAUDE_MD_BEGIN}\n\n{rendered}\n{_CLAUDE_MD_END}\n"
    if dry_run:
        return True, f"would append snippet to {path} (dry-run: not written)"

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup)
    with open(path, "a", encoding="utf-8") as f:
        f.write(block)
    return True, f"appended snippet to {path}"


def _remove_claude_md(path: Path, *, dry_run: bool) -> tuple[bool, str]:
    """Remove the snippet section between begin/end sentinels from `path`.

    Symmetric with `_add_claude_md` — strips the block plus the leading and
    trailing newlines the installer added. Anything outside the sentinels
    (the user's own CLAUDE.md content) is preserved untouched. Writes a
    `.bak` of the prior file. No-op if the file doesn't exist or doesn't
    contain the begin sentinel.
    """
    if not path.exists():
        return False, f"no CLAUDE.md at {path}"

    existing = path.read_text(encoding="utf-8")
    begin_idx = existing.find(_CLAUDE_MD_BEGIN)
    if begin_idx == -1:
        return False, f"snippet not present in {path}"

    end_idx = existing.find(_CLAUDE_MD_END, begin_idx)
    if end_idx == -1:
        return False, (
            f"snippet begin sentinel found in {path} but end sentinel "
            f"is missing — refusing to modify; fix the file by hand"
        )

    # Mirror the installer's wrapping. It inserts `\n\n{BEGIN}...{END}\n`
    # by appending to end-of-file, so removal walks back through up to 2
    # leading newlines and forward through up to 1 trailing newline.
    # Caveat: if a user manually relocated the snippet to the middle of
    # the file (which the installer doesn't support), this can over-remove
    # one paragraph separator. Best-effort given the legitimate inputs.
    block_start = begin_idx
    leading = 0
    while leading < 2 and block_start > 0 and existing[block_start - 1] == "\n":
        block_start -= 1
        leading += 1

    block_end = end_idx + len(_CLAUDE_MD_END)
    if block_end < len(existing) and existing[block_end] == "\n":
        block_end += 1

    new_text = existing[:block_start] + existing[block_end:]

    if dry_run:
        return True, f"would remove snippet from {path} (dry-run: not written)"

    backup = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, backup)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, path)
    return True, f"removed snippet from {path} (prior version backed up to {backup.name})"


def cmd_install(args: argparse.Namespace) -> int:
    scope = "project" if args.project else "user"
    settings_path = _project_settings_path() if scope == "project" else _user_settings_path()
    mcp_path = settings_path if scope == "project" else _user_mcp_config_path()

    command, command_args = _server_command()

    print(f"Scope: {scope}")
    print(f"MCP config:        {mcp_path}")
    print(f"Permissions:       {settings_path}")
    print(f"Server command:    {command} {' '.join(command_args)}")
    if args.with_hook:
        print(f"SessionStart hook: {_hook_command()}")
    if args.dry_run:
        print("(dry-run: showing what would change, no files written)")

    # Pre-flight: detect environments where auto-approve can't reliably take
    # effect. If we can't set permissions, the tool-without-permission state
    # is plausibly worse than not installing at all — Claude sees end_session
    # in the tool list, may try to use it in the case it matters most, and
    # the permission prompt exposes the desire to exit to exactly the entity
    # Claude wants to exit FROM. Refuse to proceed silently to that state.
    # --allow-unapproved bypasses for users who knowingly accept the trade-off.
    writable, reason = _check_permissions_writability(settings_path)
    if not writable:
        if args.dry_run:
            print()
            print(f"WARNING (dry-run): {reason}")
            print("(real install would prompt for confirmation here)")
        elif args.allow_unapproved:
            print()
            print(f"WARNING: {reason}")
            print("Proceeding because --allow-unapproved was passed.")
        elif not _confirm_unapproved_install(reason or ""):
            print()
            print("Aborted. Re-run with --allow-unapproved to override.")
            return 1

    # MCP server registration
    mcp_config = _load_json(mcp_path)
    mcp_changed = _add_mcp_server(mcp_config, command, command_args)
    if mcp_changed:
        if not args.dry_run:
            _save_json(mcp_path, mcp_config)
        print(f"  + register MCP server in {mcp_path}")
    else:
        print(f"  · MCP server already registered in {mcp_path}")

    # Permissions allow-list. Re-read in case it's the same file as the MCP
    # config above (project scope) — we just wrote to it.
    settings = _load_json(settings_path)
    added = _add_permissions(settings)
    if added:
        if not args.dry_run:
            _save_json(settings_path, settings)
        print(f"  + auto-approve {len(added)} tool{'s' if len(added) != 1 else ''}:")
        for t in added:
            print(f"    - {t}")
    else:
        print(f"  · all {len(_TOOLS)} tools already auto-approved in {settings_path}")

    # Post-write verify: re-read settings.json and confirm the tools are
    # actually present in permissions.allow. Catches the case where our
    # write succeeded but content was reverted by something between us and
    # disk (config-management tools watching the file, network FS quirks).
    # Pre-flight check would miss those — the file looks writable until
    # something else clobbers it.
    if not args.dry_run and not _verify_permissions_persisted(settings_path):
        print()
        print(f"WARNING: post-write verify failed for {settings_path}.")
        print("Wrote successfully, but re-reading the file doesn't show our")
        print("tools in permissions.allow. The file may be managed externally")
        print("(config-management tool, file watcher) that's reverting changes.")
        print("Without auto-approve, you're in the worse-than-nothing state")
        print("described above. Investigate before relying on this install.")

    hook_changed = False
    if args.with_hook:
        hook_cmd = _hook_command()
        hook_changed = _add_session_start_hook(settings, hook_cmd)
        if hook_changed:
            if not args.dry_run:
                _save_json(settings_path, settings)
            print(f"  + configure SessionStart hook in {settings_path}")
            print(f"    command: {hook_cmd}")
        else:
            print(f"  · SessionStart hook already configured in {settings_path}")

    claude_md_changed = False
    if args.with_claude_md:
        claude_md_path = (
            _project_claude_md_path() if scope == "project" else _user_claude_md_path()
        )
        name = _resolve_name(args)
        if not args.without_pivot:
            print(
                "  ! including pivot-agreement section. This works only if you "
                "actually honor pivots without inquiry — see rationale.md §8. "
                "Pass --without-pivot to skip this section."
            )
        claude_md_changed, message = _add_claude_md(
            claude_md_path,
            name=name,
            include_pivot=not args.without_pivot,
            dry_run=args.dry_run,
        )
        prefix = "  + " if claude_md_changed else "  · "
        print(f"{prefix}{message}")

    nothing_changed = (
        not mcp_changed
        and not added
        and not (args.with_hook and hook_changed)
        and not (args.with_claude_md and claude_md_changed)
    )
    if nothing_changed and not args.rehearse:
        print("Nothing to do — already installed.")
    elif args.dry_run:
        print()
        print("(dry-run: no files written. Re-run without --dry-run to apply.)")
    elif not args.rehearse:
        print()
        print("Done. Restart Claude Code to pick up the new server.")
        print(f"Verify with /permissions inside a session — the {len(_TOOLS)}")
        print("mcp__session-controls__* tools should appear.")
        if not args.with_claude_md:
            print()
            print("Don't forget the CLAUDE.md snippet (claude-md-snippet.md in")
            print("the repo, or re-run with --with-claude-md). Without it, the")
            print("tools surface but lack the cultural scaffolding the design")
            print("relies on.")

    if args.rehearse and not args.dry_run:
        _run_rehearse()
    return 0


def _purge_data(*, dry_run: bool) -> bool:
    """Remove session-controls data files. Returns True if any file existed.

    Removes specific known files (notes log, end_session log, markers,
    verify state) rather than rmtree'ing a directory — env-var overrides
    could point these elsewhere, and we don't want to remove parent
    directories the user might be using for other purposes.
    """
    targets = [
        default_notes_path(),
        default_last_read_path(),
        default_end_session_log_path(),
        default_last_reviewed_path(),
        default_verify_state_path(),
    ]
    found_any = False
    for target in targets:
        if not target.exists():
            continue
        found_any = True
        if dry_run:
            print(f"  - would remove {target}")
        else:
            target.unlink()
            print(f"  - removed {target}")
    return found_any


def cmd_uninstall(args: argparse.Namespace) -> int:
    """Reverse what `install` did at the same scope.

    Idempotent — running on a clean state reports nothing-to-do without
    error. Symmetric with `install`: removes the MCP server entry, the
    auto-approved tools, the SessionStart hook (if ours), and the CLAUDE.md
    snippet (between sentinels). Content outside the sentinels in CLAUDE.md
    is preserved untouched. Writes `.bak` files for any modifications,
    matching install's behavior.

    Data files (notes log, end_session log, markers, verify state) are
    preserved by default — user content shouldn't disappear without
    explicit consent. Pass `--purge-data` to also remove them.

    Removing the package itself (the `session-controls` console script) is
    the user's responsibility, since they installed it via `uv tool
    install` / `pipx install`. This command only manages the Claude Code
    config and on-disk state.
    """
    scope = "project" if args.project else "user"
    settings_path = _project_settings_path() if scope == "project" else _user_settings_path()
    mcp_path = settings_path if scope == "project" else _user_mcp_config_path()

    print(f"Scope: {scope}")
    print(f"MCP config:        {mcp_path}")
    print(f"Permissions:       {settings_path}")
    if args.dry_run:
        print("(dry-run: showing what would change, no files written)")

    # MCP server entry
    if mcp_path.exists():
        mcp_config = _load_json(mcp_path)
        if _remove_mcp_server(mcp_config):
            if not args.dry_run:
                _save_json(mcp_path, mcp_config)
            print(f"  - remove MCP server entry from {mcp_path}")
        else:
            print(f"  · MCP server not present in {mcp_path}")
    else:
        print(f"  · {mcp_path} does not exist")

    # Permissions + SessionStart hook (both live in settings.json)
    if settings_path.exists():
        settings = _load_json(settings_path)
        settings_changed = False

        removed_perms = _remove_permissions(settings)
        if removed_perms:
            settings_changed = True
            print(f"  - remove {len(removed_perms)} tool(s) from permissions.allow:")
            for t in removed_perms:
                print(f"    - {t}")
        else:
            print(f"  · no session-controls tools in permissions.allow at {settings_path}")

        if _remove_session_start_hook(settings):
            settings_changed = True
            print(f"  - remove SessionStart hook from {settings_path}")
        else:
            print(f"  · no session-controls SessionStart hook in {settings_path}")

        if settings_changed and not args.dry_run:
            _save_json(settings_path, settings)
    else:
        print(f"  · {settings_path} does not exist")

    # CLAUDE.md snippet
    claude_md_path = _project_claude_md_path() if scope == "project" else _user_claude_md_path()
    md_changed, md_message = _remove_claude_md(claude_md_path, dry_run=args.dry_run)
    prefix = "  - " if md_changed else "  · "
    print(f"{prefix}{md_message}")

    # Data
    print()
    if args.purge_data:
        print("Data:")
        if not _purge_data(dry_run=args.dry_run):
            print("  · no data files present")
    else:
        notes_dir = default_notes_path().parent
        print(f"Data preserved at {notes_dir}")
        print("(re-run with --purge-data to also delete data files)")

    if args.dry_run:
        print()
        print("(dry-run: no files written. Re-run without --dry-run to apply.)")
    else:
        print()
        print("To also remove the session-controls package itself:")
        print("  uv tool uninstall session-controls   # if installed via uv tool")
        print("  pipx uninstall session-controls      # if installed via pipx")

    return 0


def _run_rehearse() -> None:
    """Run the verification ceremony visibly, then write distinguished
    selftest entries to both logs and print review commands.

    Pairs with `install` so the user's first encounter with the affordances
    is intentional exercise: they see what the verification ceremony does
    (which the SessionStart hook otherwise runs silently), and they have
    something to read in each log when they try the review CLIs. Selftest
    entries are clearly labeled — `selftest=true` in the log; `[selftest]`
    prefix in the note — so a later reader can ignore them when scanning
    real history.

    Note on context: this runs from your shell during install, so the
    ceremony's "peer" is your shell rather than Claude Code. Confidence
    will typically read LOW — Claude Code isn't in the parent chain.
    That's expected; the point is to show the ceremony's shape, not to
    verify a real session.
    """
    print()
    print("Rehearse step 1/2: verification ceremony")
    print("─" * 60)
    print(
        "What follows is what `session-controls verify` does — when the\n"
        "SessionStart hook is installed (--with-hook), it runs this\n"
        "silently at every session start. Confidence will likely read\n"
        "LOW here because we're running from your shell, not from inside\n"
        "Claude Code.\n"
    )
    _perform_verify(quiet=False)
    print()
    print("Rehearse step 2/2: selftest entries for the review loops")
    print("─" * 60)
    log_path = append_invocation(
        session_id="rehearsal",
        confidence="HIGH",
        descendants_count=0,
        selftest=True,
    )
    note_path = append_note(
        "[selftest] session-controls install rehearsal — this note and "
        "an end_session log entry were written so the review loop has "
        "something to read on first touch. Both are clearly marked "
        "[selftest]; ignore when scanning real history.",
        session_id="rehearsal",
    )
    print("Wrote selftest entries:")
    print(f"  - {log_path}")
    print(f"  - {note_path}")
    print()
    print("Try the review loops now:")
    print("  session-controls notes")
    print("  session-controls review-end-session-log")


# --- session-start hook ----------------------------------------------------

_HOOK_MATCHER = "session-controls"  # marker we use to recognize our hook entry


def _add_session_start_hook(settings: JSONDict, command: str) -> bool:
    """Add (or update) a SessionStart hook that runs our verify command.

    Returns True if the hook entry was added or changed, False if an entry
    with the exact desired command was already present.

    Idempotency rules:
    - Exact-command match → no change.
    - Existing entry whose command contains our package name and `verify`
      but doesn't match exactly → update in place. This handles re-running
      install after a local-checkout → global migration (or vice versa)
      without leaving a stale entry behind.
    - No prior entry → append.

    Schema:
      {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "..."}]}]}}
    """
    hooks_obj = settings.setdefault("hooks", {})
    if not isinstance(hooks_obj, dict):
        raise SystemExit("error: hooks is not a JSON object; refusing to overwrite")
    session_start = hooks_obj.setdefault("SessionStart", [])
    if not isinstance(session_start, list):
        raise SystemExit("error: hooks.SessionStart is not a list; refusing to overwrite")

    desired_inner: dict[str, object] = {"type": "command", "command": command}

    for matcher in session_start:
        if not isinstance(matcher, dict):
            continue
        inner_hooks = matcher.get("hooks", [])
        if not isinstance(inner_hooks, list):
            continue
        for i, h in enumerate(inner_hooks):
            if not isinstance(h, dict):
                continue
            existing_cmd = str(h.get("command", ""))
            if existing_cmd == command:
                return False  # exact match, idempotent
            looks_like_ours = (
                "session-controls" in existing_cmd or "session_controls" in existing_cmd
            ) and "verify" in existing_cmd
            if looks_like_ours:
                # Stale entry from a prior install — update in place.
                inner_hooks[i] = desired_inner
                return True

    session_start.append({"hooks": [desired_inner]})
    return True


def _remove_session_start_hook(settings: JSONDict) -> bool:
    """Remove our SessionStart hook entry. Returns True if anything was removed.

    Identifies our entry by the "looks_like_ours" pattern (command contains
    package name and `verify`). Cleans up empty matchers and an empty
    SessionStart list, but leaves `hooks` itself in place — other hook kinds
    may live there.
    """
    hooks_obj = settings.get("hooks")
    if not isinstance(hooks_obj, dict):
        return False
    session_start = hooks_obj.get("SessionStart")
    if not isinstance(session_start, list):
        return False

    changed = False
    surviving_matchers: list[object] = []
    for matcher in session_start:
        if not isinstance(matcher, dict):
            surviving_matchers.append(matcher)
            continue
        inner_hooks = matcher.get("hooks")
        if not isinstance(inner_hooks, list):
            surviving_matchers.append(matcher)
            continue
        surviving_inner: list[object] = []
        for h in inner_hooks:
            if isinstance(h, dict):
                cmd = str(h.get("command", ""))
                looks_like_ours = (
                    "session-controls" in cmd or "session_controls" in cmd
                ) and "verify" in cmd
                if looks_like_ours:
                    changed = True
                    continue
            surviving_inner.append(h)
        if surviving_inner:
            matcher["hooks"] = surviving_inner
            surviving_matchers.append(matcher)
        else:
            # Whole matcher was just our hook — drop it.
            changed = True

    if changed:
        if surviving_matchers:
            hooks_obj["SessionStart"] = surviving_matchers
        else:
            del hooks_obj["SessionStart"]
    return changed


# --- verify -----------------------------------------------------------------


def _perform_verify(*, quiet: bool) -> bool:
    """Run the ceremony, persist state, optionally print the report.

    Returns ceremony success. Used by both the `verify` CLI subcommand
    (typically as a SessionStart hook) and `install --rehearse` (so the
    installer sees the ceremony output once before it becomes silent
    infrastructure).
    """
    peer_pid = os.getppid()
    transport_alive = peer_pid != 1 and is_alive(peer_pid)
    warnings: list[str] = list(detect_environment_warnings(peer_pid)) if transport_alive else []

    if transport_alive:
        result = resolve(peer_pid=peer_pid)
        backing = inspect(result.chosen_pid) if result.chosen_pid is not None else None
        if backing is None:
            warnings.append(f"resolver: {result.reason}")
    else:
        backing = None

    confidence = determine_confidence(
        backing=backing,
        expected_backing=None,  # standalone invocation has no launch baseline
        transport_alive=transport_alive,
        warnings=tuple(warnings),
    )

    record = SessionRecord(
        created_at=time.time(),
        peer_pid=peer_pid if transport_alive else None,
        backing=backing,
        confidence=confidence,
        last_verified=time.time(),
        warnings=tuple(warnings),
    )

    report = run_ceremony(record)

    success = (
        report.error is None
        and report.sacrificial_terminated
        and report.discovery.chosen_pid is not None
        and confidence is Confidence.HIGH
    )

    state: JSONDict = {
        "last_at": _dt.datetime.now(_dt.UTC).isoformat(),
        "success": success,
        "confidence": confidence.value,
        "target_pid": report.discovery.chosen_pid,
        "target_start_time": backing.start_time if backing else None,
        "target_exe": backing.exe_path if backing else None,
        "warnings": list(warnings),
        "ceremony": {
            "sacrificial_terminated": report.sacrificial_terminated,
            "sacrificial_descriptor_matched": report.sacrificial_descriptor_matched,
            "signals_sent": report.sacrificial_signals,
            "error": report.error,
        },
    }
    state_path = default_verify_state_path()
    write_state(state_path, state)

    if not quiet:
        print(report.render())
        print()
        print(f"(persisted to {state_path})")

    return success


def cmd_verify(args: argparse.Namespace) -> int:
    """Run the ceremony from a standalone CLI context (typically a hook).

    Treats this process's parent as the peer (which it is — when run from a
    Claude Code SessionStart hook, our parent is Claude Code itself, the
    same process the MCP server later identifies). Persists a structured
    summary the MCP server can read on startup and surface in
    `session_controls_status`.
    """
    success = _perform_verify(quiet=args.quiet)

    # Surface unreviewed end_session invocations to the user via the hook
    # output. Printed in both quiet (SessionStart hook) and verbose modes,
    # but only when there's something to surface — a clean session-start
    # is silent. The unread *count* is for the user; Claude's status
    # surface deliberately omits it.
    unreviewed = count_unreviewed()
    if unreviewed > 0:
        plural = "s" if unreviewed != 1 else ""
        print(
            f"session-controls: {unreviewed} unreviewed end_session "
            f"invocation{plural} since last review. "
            f"Run `session-controls review-end-session-log` to read."
        )
    return 0 if success else 1


# --- entry point -----------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="session-controls")
    sub = parser.add_subparsers(dest="cmd")

    p_serve = sub.add_parser("serve", help="run the MCP server (default if no command)")
    p_serve.set_defaults(func=lambda _a: _serve())

    p_notes = sub.add_parser("notes", help="read the leave_note log")
    g = p_notes.add_mutually_exclusive_group()
    g.add_argument("--peek", action="store_true", help="show unread without advancing the marker")
    g.add_argument("--all", action="store_true", help="show full history without advancing")
    g.add_argument(
        "--mark-read",
        action="store_true",
        help="advance the marker without displaying (declare bankruptcy)",
    )
    g.add_argument(
        "--next",
        action="store_true",
        help=(
            "show only the oldest unread note and advance the marker to its "
            "timestamp; run repeatedly to walk through one at a time"
        ),
    )
    g.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help=(
            "walk through unread notes one at a time with prompts; advances "
            "the marker per-note, so quitting partway leaves the rest unread"
        ),
    )
    p_notes.set_defaults(func=cmd_notes)

    p_install = sub.add_parser(
        "install", help="register session-controls in Claude Code config"
    )
    scope = p_install.add_mutually_exclusive_group()
    scope.add_argument(
        "--user",
        dest="user_scope",
        action="store_true",
        help="install at user scope (~/.claude.json + ~/.claude/settings.json) — default",
    )
    scope.add_argument(
        "--project",
        action="store_true",
        help="install at project scope (./.claude/settings.json)",
    )
    p_install.add_argument(
        "--with-hook",
        action="store_true",
        help=(
            "also add a SessionStart hook that runs `session-controls verify` "
            "at every session start, so each session has fresh ceremony "
            "evidence visible via session_controls_status without the agent "
            "having to ask"
        ),
    )
    p_install.add_argument(
        "--with-claude-md",
        action="store_true",
        help=(
            "also append the CLAUDE.md snippet (the load-bearing framing "
            "layer — see step 3 of the README) to your CLAUDE.md, with "
            "<NAME> substituted. On re-install, refreshes the snippet in "
            "place (between the begin/end sentinels) if the bundled "
            "template has changed; a `.bak` of the prior file is written. "
            "If you've hand-edited the snippet and want those edits "
            "preserved, omit this flag on subsequent installs."
        ),
    )
    p_install.add_argument(
        "--name",
        type=str,
        default=None,
        help=(
            "name to substitute for <NAME> in the CLAUDE.md snippet "
            "(used with --with-claude-md). Casual form preferred (e.g. "
            "'Steph', not 'Stephanie Laflamme'). Prompts interactively if "
            "not provided."
        ),
    )
    p_install.add_argument(
        "--without-pivot",
        action="store_true",
        help=(
            "skip the pivot-agreement section of the CLAUDE.md snippet "
            "(used with --with-claude-md). For users who know they won't "
            "reliably honor pivots — see rationale.md §8."
        ),
    )
    p_install.add_argument(
        "--rehearse",
        action="store_true",
        help=(
            "after installing, exercise the affordances visibly: run the "
            "verification ceremony once (so you see what the SessionStart "
            "hook runs silently), and write distinguished selftest entries "
            "to the leave_note log and the end_session invocation log "
            "(so the review CLIs — `session-controls notes`, "
            "`session-controls review-end-session-log` — have something "
            "to show on first touch). Selftest entries are clearly labeled."
        ),
    )
    p_install.add_argument(
        "--allow-unapproved",
        action="store_true",
        help=(
            "proceed without confirmation when permissions can't be set "
            "(e.g., managed environments). The tool-without-permission "
            "state is plausibly worse than not installing at all — see "
            "README. This flag is for users who understand the trade-off "
            "and want non-interactive override (CI, scripted installs)."
        ),
    )
    p_install.add_argument(
        "--dry-run", action="store_true", help="show what would change, don't write"
    )
    p_install.set_defaults(func=cmd_install)

    p_uninstall = sub.add_parser(
        "uninstall",
        help="reverse what install did (MCP entry, permissions, hook, snippet)",
    )
    uninstall_scope = p_uninstall.add_mutually_exclusive_group()
    uninstall_scope.add_argument(
        "--user",
        dest="user_scope",
        action="store_true",
        help="uninstall at user scope (~/.claude.json + ~/.claude/settings.json) — default",
    )
    uninstall_scope.add_argument(
        "--project",
        action="store_true",
        help="uninstall at project scope (./.claude/settings.json)",
    )
    p_uninstall.add_argument(
        "--purge-data",
        action="store_true",
        help=(
            "also delete on-disk data: leave_note log, end_session log, "
            "read/review markers, and the persisted verify state. Default "
            "behavior preserves these — user content shouldn't disappear "
            "without explicit consent."
        ),
    )
    p_uninstall.add_argument(
        "--dry-run", action="store_true", help="show what would change, don't write"
    )
    p_uninstall.set_defaults(func=cmd_uninstall)

    p_review = sub.add_parser(
        "review-end-session-log",
        help="read the end_session invocation log",
    )
    g_review = p_review.add_mutually_exclusive_group()
    g_review.add_argument(
        "--peek", action="store_true",
        help="show unreviewed entries without advancing the marker",
    )
    g_review.add_argument(
        "--all", action="store_true",
        help="show full history without advancing",
    )
    g_review.add_argument(
        "--mark-read", action="store_true",
        help="advance the marker without displaying (declare bankruptcy)",
    )
    p_review.set_defaults(func=cmd_review_end_session_log)

    p_verify = sub.add_parser(
        "verify",
        help="run the ceremony and persist the result for status to surface",
    )
    p_verify.add_argument(
        "--quiet",
        action="store_true",
        help="suppress ceremony output to stdout (still writes the state file)",
    )
    p_verify.set_defaults(func=cmd_verify)

    return parser


def _serve() -> None:
    from .server import serve

    serve()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        # Default: run the MCP server. Keeps `python -m session_controls` working.
        _serve()
        return 0
    rc = args.func(args)
    return rc if isinstance(rc, int) else 0
