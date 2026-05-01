"""User-facing CLI for session-controls.

Subcommands:

    session-controls notes [--peek] [--all] [--mark-read | --next | --interactive]
        Read the leave_note log.

    session-controls review-end-session-log [--peek] [--all] [--mark-read]
        Read the end_session invocation log.

    session-controls install [--user|--project] [--with-hook] [--rehearse] [--dry-run]
        Add session-controls to your Claude Code MCP config and auto-approve
        the package's MCP tools.

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
    ack = " (acknowledged)" if inv.acknowledged else ""
    selftest = " [SELFTEST]" if inv.selftest else ""
    print(f"{prefix}{inv.timestamp.isoformat()}{sid} {confidence}{ack}{selftest}")
    print(f"  cwd:  {inv.cwd or '-'}")
    print(f"  repo: {inv.repo or '-'}")
    print(f"  descendants at exit: {inv.descendants_count}")
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

_TOOLS = [
    "mcp__session-controls__end_session",
    "mcp__session-controls__session_controls_status",
    "mcp__session-controls__verify_session_controls",
    "mcp__session-controls__leave_note",
    "mcp__session-controls__recent_notes",
    "mcp__session-controls__recent_end_sessions",
]
SERVER_NAME = "session-controls"


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


def _run_rehearse() -> None:
    """Write distinguished selftest entries to both logs and print review commands.

    Pairs with `install` so the first user-visible entry in each log is
    something the user wrote intentionally (an exercise of the review
    loop), not a real invocation. Selftest entries are clearly labeled —
    `selftest=true` in the log; `[selftest]` prefix in the note — so a
    later reader can ignore them when scanning history.
    """
    log_path = append_invocation(
        session_id="rehearsal",
        confidence="HIGH",
        acknowledged=False,
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
    print()
    print("Rehearse: wrote selftest entries to exercise the review loop.")
    print(f"  - {log_path}")
    print(f"  - {note_path}")
    print()
    print("Try them now:")
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


# --- verify -----------------------------------------------------------------


def cmd_verify(args: argparse.Namespace) -> int:
    """Run the ceremony from a standalone CLI context (typically a hook).

    Treats this process's parent as the peer (which it is — when run from a
    Claude Code SessionStart hook, our parent is Claude Code itself, the
    same process the MCP server later identifies). Builds a SessionRecord,
    runs the ceremony, and persists a structured summary the MCP server
    can read on startup and surface in `session_controls_status`.
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
        and confidence in (Confidence.HIGH, Confidence.MEDIUM)
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

    if not args.quiet:
        print(report.render())
        print()
        print(f"(persisted to {state_path})")

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
            "after installing, write distinguished selftest entries to the "
            "leave_note log and the end_session invocation log. Pairs with "
            "install so the first time you touch the review loop "
            "(`session-controls notes`, `session-controls review-end-"
            "session-log`) it has something to show — exercise rather than "
            "real history. Selftest entries are clearly labeled."
        ),
    )
    p_install.add_argument(
        "--dry-run", action="store_true", help="show what would change, don't write"
    )
    p_install.set_defaults(func=cmd_install)

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
