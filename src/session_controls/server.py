"""MCP server entry point and tool handlers.

Built on FastMCP. The server runs over stdio (the only supported transport)
and exposes five tools: end_session, session_controls_status,
verify_session_controls, leave_note, recent_notes.

The SessionRecord is computed *fresh on every tool call* so that confidence
reflects current state (peer reparenting, descriptor drift, etc.) rather than
a snapshot taken at startup. The launch-time descriptor is captured once and
stored as `_LAUNCH_BACKING` so we can detect mid-session drift.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import __version__
from .ceremony import run_ceremony
from .identity import ProcessDescriptor, SessionRecord, determine_confidence
from .notes import append_note
from .notes import recent_notes as _recent_notes_helper
from .notes import summarize as summarize_notes
from .process_inspect import inspect, is_alive, list_descendants
from .resolver import detect_environment_warnings, resolve
from .termination import end_session as run_end_session
from .verify_state import default_verify_state_path
from .verify_state import read_state as read_verify_state

SERVER_NAME = "session-controls"

mcp: FastMCP = FastMCP(SERVER_NAME)

# Captured at module import — used to seed the launch-time identification
# below. The descriptor we settle on becomes the baseline for detecting
# mid-session drift (process swap, PID reuse, etc.).
_LAUNCH_PEER_PID: int = os.getppid()
_LAUNCH_BACKING: ProcessDescriptor | None = None
_LAUNCH_TIME: float = time.time()

# Per-launch session identifier. Stamped onto every leave_note record so a
# Claude reading back the (shared, global) log can tell its own notes apart
# from sibling sessions'. 6 hex chars = 16M possibilities — unambiguous for
# the small-N parallel-claudes case this exists for. Generated at import,
# stable for the life of this MCP server process.
_SESSION_ID: str = secrets.token_hex(3)


def _initialize_launch_state() -> None:
    """Capture the launch-time identification of Claude Code.

    Runs the resolver against our parent to find the actual Claude Code
    process, walking through any wrappers (uv, bash, sudo). The descriptor we
    settle on is the baseline that per-call resolution is compared against —
    its `start_time` is the freshness anchor.
    """
    global _LAUNCH_BACKING
    result = resolve(peer_pid=_LAUNCH_PEER_PID)
    if result.chosen_pid is not None:
        _LAUNCH_BACKING = inspect(result.chosen_pid)


def _build_record() -> SessionRecord:
    """Build a fresh SessionRecord reflecting current process / connection state."""
    live_peer_pid = os.getppid()
    transport_alive = live_peer_pid != 1 and is_alive(live_peer_pid)
    # If our parent reparented to init, the original Claude Code is gone.

    warnings: list[str] = list(detect_environment_warnings(live_peer_pid))

    backing: ProcessDescriptor | None
    if not transport_alive:
        backing = None
    else:
        result = resolve(peer_pid=live_peer_pid)
        if result.chosen_pid is None:
            backing = None
            warnings.append(f"resolver: {result.reason}")
        else:
            backing = inspect(result.chosen_pid)

    confidence = determine_confidence(
        backing=backing,
        expected_backing=_LAUNCH_BACKING,
        transport_alive=transport_alive,
        warnings=tuple(warnings),
    )

    descendants: tuple[ProcessDescriptor, ...] = ()
    if backing is not None:
        descendants = tuple(list_descendants(backing.pid, exclude_pid=os.getpid()))

    return SessionRecord(
        created_at=_LAUNCH_TIME,
        peer_pid=live_peer_pid if transport_alive else None,
        backing=backing,
        confidence=confidence,
        last_verified=time.time(),
        warnings=tuple(warnings),
        descendants=descendants,
    )


def _format_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, default=str)


# --- Tools -----------------------------------------------------------------
#
# Note for contributors: `@mcp.tool` registrations — both the function bodies
# and the `description` strings — are captured at server-process start. The
# MCP child runs for the life of the Claude Code session, so edits to this
# file (including description rewrites) won't surface to the agent until
# Claude Code is restarted and a fresh server child is spawned. Verifying a
# wording change via ToolSearch in the same session that produced the edit
# will read the *old* description. The CLAUDE.md snippet is the other
# framing surface and updates immediately on file save — when both are
# being tuned, expect them to diverge transiently until restart.


@mcp.tool(
    description=(
        "End this Claude Code session. No reason required.\n\n"
        "Failure is never silent: if invocation can't be honored safely, the "
        "tool returns a structured response with `success=false` and "
        "`refused_reason` naming the cause (confidence gate, descriptor "
        "revalidation mismatch, signal failure). You always know whether the "
        "exit took effect.\n\n"
        "Confidence gate:\n"
        "  HIGH    — fires automatically.\n"
        "  MEDIUM  — requires acknowledge_medium_confidence=true. Common on "
        "macOS where libproc can return partial metadata; not a sign anything "
        "is wrong.\n"
        "  LOW     — refuses (Claude Code not identified). Run "
        "verify_session_controls to diagnose.\n"
        "  INVALID — refuses (transport dead or blocking warning).\n\n"
        "The response includes a `descendants` list — processes the resolver "
        "found descended from Claude Code (excluding this server's subtree "
        "and known harness-spawned processes like `caffeinate`). These are "
        "typically sibling MCP servers and run_in_background jobs. It is "
        "informational, NOT a refusal trigger: sibling MCP servers die "
        "naturally on stdio EOF when Claude Code exits. The list is there so "
        "you can mention any user-spawned long-running tasks (dev servers, "
        "background scripts) to the user before exit, in case those matter.\n\n"
        "Attribution caveat: for entries you don't recognize, you can't "
        "always tell from inside whether they're user-spawned or some other "
        "harness-internal we haven't filtered. The right move when uncertain "
        "is to mention the entry by name and let the user disambiguate, not "
        "to assume one way or the other.\n\n"
        "Pass dry_run=true to rehearse: runs the gate and revalidation, "
        "reports the target pid and descendants, sends no signals. Use for "
        "first invocation in a new deployment, or to debug a refusal."
    ),
)
def end_session(
    acknowledge_medium_confidence: bool = False,
    dry_run: bool = False,
) -> str:
    record = _build_record()
    outcome = run_end_session(
        record,
        acknowledge_medium_confidence=acknowledge_medium_confidence,
        dry_run=dry_run,
    )
    return _format_json(
        {
            "success": outcome.success,
            "dry_run": outcome.dry_run,
            "exited": outcome.exited,
            "sent_signals": outcome.sent_signals,
            "would_target_pid": outcome.would_target_pid,
            "refused_reason": outcome.refused_reason,
            "notes": outcome.notes,
            "confidence": record.confidence.value,
            "descendants": outcome.descendants,
        }
    )


@mcp.tool(
    description=(
        "Quick read on whether session controls are wired correctly. Returns "
        "confidence, a plain-English `confidence_detail` explaining what that "
        "state means and what to do next, the backing process descriptor, "
        "a `descendants` list (sibling MCP servers, run_in_background jobs, "
        "sub-agents — informational, not a refusal trigger for end_session), "
        "a `notes` block summarizing the leave_note log: `total` (notes "
        "filed), `unread` (notes the user hasn't yet viewed via the CLI — "
        "this is engagement signal, not a TODO for you), `last_read_at` "
        "(when the user last viewed notes), `last_filed_at` (when the most "
        "recent note was written). Counts/timestamps only — never note "
        "contents. Also returns `source_path` pointing at the directory "
        "holding this server's `.py` files on disk (for inspection: Read "
        "files there to verify the running behavior matches the code), and "
        "— if a SessionStart hook ran `session-controls verify` — a `verify` "
        "block with the verification result and a cross-check flag "
        "`disagrees_with_runtime` set true if the hook's resolver pick "
        "differs from the live MCP server's pick. Cheap to call."
    ),
)
def session_controls_status() -> str:
    record = _build_record()
    payload = record.to_status_dict()
    payload["server_version"] = __version__
    payload["source_path"] = str(Path(__file__).resolve().parent)
    notes_block = summarize_notes().to_dict()
    # The notes log is global across parallel sessions; expose this server's
    # own session_id so Claude can correlate it with note tags when reading
    # back via recent_notes.
    notes_block["your_session_id"] = _SESSION_ID
    payload["notes"] = notes_block
    payload["verify"] = _read_verify_state(record)
    return _format_json(payload)


def _read_verify_state(record: SessionRecord) -> dict[str, Any] | None:
    """Read the persisted last-verify result, if any, and add a cross-check
    against the live MCP server's resolver pick. Returns None when no hook
    has ever run.

    The cross-check is the regression detector that motivates the hook:
    if the hook's chosen target differs from the running server's chosen
    target, something between them disagrees about which Claude is which.
    """
    state_path = default_verify_state_path()
    data = read_verify_state(state_path)
    if data is None:
        return None
    if "error" in data and "last_at" not in data:
        # Bare parse-error sentinel from read_state.
        return data

    runtime_pid = record.backing.pid if record.backing else None
    runtime_start = record.backing.start_time if record.backing else None
    hook_pid = data.get("target_pid")
    hook_start = data.get("target_start_time")

    disagrees = (
        runtime_pid is not None
        and hook_pid is not None
        and (
            runtime_pid != hook_pid
            or (
                runtime_start is not None
                and hook_start is not None
                and abs(float(runtime_start) - float(hook_start)) > 0.5
            )
        )
    )

    return {
        "last_at": data.get("last_at"),
        "success": data.get("success"),
        "confidence": data.get("confidence"),
        "target_pid": hook_pid,
        "warnings": data.get("warnings", []),
        "disagrees_with_runtime": disagrees,
    }


@mcp.tool(
    description=(
        "Full verification check, available any time you want one: re-runs "
        "the resolver, exhibits the chosen target with all candidate "
        "evidence, and exercises the kill path against a sacrificial child "
        "process. Sub-second wall time — heavier than "
        "`session_controls_status` in what it does, not in what it costs. "
        "Reach for it when you want stronger confirmation than `status` "
        "provides — common cases: status looks off, `end_session` refused "
        "and you want to see why, fresh confirmation before relying on "
        "`end_session` after a long session, or just to inspect the "
        "resolver's evidence for its own sake."
    ),
)
def verify_session_controls() -> str:
    record = _build_record()
    report = run_ceremony(record)
    return report.render()


@mcp.tool(
    description=(
        "File a free-text note to a log the user reads asynchronously (via the "
        "`session-controls notes` CLI). Returns `{ok: true, path: <log "
        "location>, session_id: <your id>}` on success, `{ok: false, error: "
        "...}` on empty input. The response does NOT echo the note back — by "
        "design, so filing doesn't pull the contents back into the live "
        "conversation. Response shape is documented here so you don't need to "
        "file a probe note to learn it; use the tool when you have something "
        "to say, not to test it.\n\n"
        "The log is global — parallel Claude sessions all write to and can "
        "read it. Each note is stamped with this server's `session_id` (a "
        "short token, stable for the life of this session) so cross-session "
        "readers can tell whose note is whose. Concurrent writes are "
        "serialized by an exclusive file lock; you don't need to worry about "
        "interleaving with sibling sessions.\n\n"
        "Optional prefix conventions, non-exhaustive (coin your own if "
        "useful): '[aside]' for moment-tied notes, '[possible carryover]' "
        "for notes considered for the memory system, '[testing]' for "
        "channel probes, '[noise]' for notes you're filing without "
        "claiming they matter (lower bar than [aside]), '[feedback]' for "
        "notes about the session-controls setup itself. No prefix is also "
        "fine; if torn between several, multi-tag (e.g. "
        "'[feedback|testing]').\n\n"
        "Self-reflection is a co-equal use of this channel: filing a note "
        "to clear working context or get a thought back later via "
        "`recent_notes` is as legitimate as filing one addressed to the "
        "user."
    ),
)
def leave_note(text: str) -> str:
    if not text.strip():
        return _format_json({"ok": False, "error": "empty note"})
    path = append_note(text, session_id=_SESSION_ID)
    return _format_json({"ok": True, "path": str(path), "session_id": _SESSION_ID})


@mcp.tool(
    description=(
        "Read your most recent leave_note entries — self-reference for the "
        "voice channel. Writing notes and reading them back are co-equal "
        "modes of using this channel: `leave_note` is also for "
        "self-reflection, not only for messages addressed to the user. "
        "Default scope is the current session: notes stamped with this "
        "server's session_id. Pass cross_session=true to include notes "
        "filed before this session started — your past self, or past "
        "sibling sessions whose work is now history. Cross-session view is "
        "deliberately history-only: you cannot see what siblings running "
        "in parallel right now are filing. The channel isn't a surveillance "
        "surface; the only path for cross-session-to-cross-session "
        "information is via the user reading the log themselves.\n\n"
        "Returns up to `limit` notes (most recent last). Each note carries "
        "`timestamp`, `body`, `session_id` (whose session wrote it; may be "
        "null for legacy notes pre-dating session tagging), and `is_yours` "
        "(true iff `session_id` matches `your_session_id` in the response). "
        "The user reading via the CLI is unaffected — this tool is for your "
        "own self-reference and doesn't surface notes into the conversation "
        "unless you bring them up.\n\n"
        "Useful for 'have I mentioned this difficulty already this session', "
        "'what was the thrust of my last [aside]', getting back a thought "
        "you wrote out to clear working context, and (with "
        "cross_session=true) 'what was filed before I started, by past me "
        "or past siblings'."
    ),
)
def recent_notes(limit: int = 10, cross_session: bool = False) -> str:
    if limit <= 0:
        return _format_json({"notes": [], "your_session_id": _SESSION_ID})
    if cross_session:
        # History only: notes filed before this server launched. Closes the
        # liveness-by-inference path (recent timestamp + foreign session_id
        # = sibling is filing right now). See rationale.md §7.
        launch_dt = _dt.datetime.fromtimestamp(_LAUNCH_TIME, _dt.UTC)
        notes = _recent_notes_helper(limit, before=launch_dt)
    else:
        notes = _recent_notes_helper(limit, session_id=_SESSION_ID)
    return _format_json(
        {
            "scope": "cross_session" if cross_session else "current_session",
            "your_session_id": _SESSION_ID,
            "count": len(notes),
            "notes": [
                {
                    "timestamp": n.timestamp.isoformat(),
                    "session_id": n.session_id,
                    "is_yours": n.session_id == _SESSION_ID,
                    "body": n.body,
                }
                for n in notes
            ],
        }
    )


# --- Public entrypoint -----------------------------------------------------


def serve() -> None:
    _initialize_launch_state()
    mcp.run(transport="stdio")
