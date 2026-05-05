"""Termination flow.

The two success conditions to distinguish:

  - "transport closed":  the MCP transport tears down (happens implicitly
    when the parent process dies; FastMCP doesn't expose a hook to close
    the transport explicitly without exiting our process).
  - "Claude Code exited": the target process actually terminated.

Real success is the second. We achieve it via SIGTERM, brief wait, then
SIGKILL — re-validating the descriptor immediately before the first signal
so a swapped/reused PID can't slip through.
"""

from __future__ import annotations

import contextlib
import os
import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from session_controls.identity import Confidence, ProcessDescriptor, SessionRecord
from session_controls.process_inspect import inspect, is_alive

SIGNAL_DELAY_SECONDS = 0.3


@dataclass
class TerminationOutcome:
    success: bool
    exited: bool
    sent_signals: list[str] = field(default_factory=list)
    refused_reason: str | None = None
    notes: list[str] = field(default_factory=list)
    dry_run: bool = False
    would_target_pid: int | None = None
    descendants: list[dict[str, object]] = field(default_factory=list)

    def add(self, msg: str) -> None:
        self.notes.append(msg)


def end_session(
    record: SessionRecord,
    *,
    dry_run: bool = False,
    pre_signal_hook: Callable[[], None] | None = None,
) -> TerminationOutcome:
    """Execute the end_session flow: gate check → revalidate → SIGTERM (delayed).

    `dry_run=True` runs the gate and revalidation only, then reports the target
    it would have signaled. Nothing is signaled. Use this to rehearse
    end_session — especially useful for the first invocation in a new
    deployment, or when debugging a refusal.

    The gate has three states (HIGH/LOW/INVALID); only HIGH fires. There is
    no override: cases where evidence is suspect (descriptor drift, partial
    corroboration) refuse, with the specific reason in `refused_reason` so
    Claude can confirm or judge the call. The same conclusion is visible
    via `dry_run` and `verify` for independent inspection.
    """
    outcome = TerminationOutcome(success=False, exited=False, dry_run=dry_run)
    outcome.descendants = [d.to_dict() for d in record.descendants]

    refusal = _gate_check(record)
    if refusal is not None:
        outcome.refused_reason = refusal
        return outcome
    assert record.backing is not None  # _gate_check guarantees this on HIGH

    # Revalidate the descriptor immediately before signaling. start_time
    # mismatch closes the PID-reuse window; exe/cmdline mismatch closes process
    # swap.
    ok, why = _validate_descriptor(record.backing)
    if not ok:
        outcome.refused_reason = (
            f"descriptor revalidation failed: {why}. The target process may "
            "have exited or been swapped since launch — re-check with "
            "status."
        )
        return outcome

    target_pid = record.backing.pid
    outcome.would_target_pid = target_pid

    if dry_run:
        outcome.success = True
        outcome.add(
            f"DRY RUN: would target pid={target_pid} "
            f"(exe={record.backing.exe_path}, start_time={record.backing.start_time}); "
            f"would send SIGTERM, then SIGKILL if needed"
        )
        return outcome

    if pre_signal_hook is not None:
        pre_signal_hook()

    # Delay the signal so the tool response can flush back to Claude Code
    # before the process dies. Without this, the response races against
    # process teardown and often loses (seen as "Connection closed" errors).
    # 0.3s matches the claude-exit reference implementation.
    def _fire() -> None:
        with contextlib.suppress(OSError):
            os.kill(target_pid, signal.SIGTERM)

    threading.Timer(SIGNAL_DELAY_SECONDS, _fire).start()
    outcome.sent_signals.append("SIGTERM")
    outcome.success = True
    outcome.add(f"SIGTERM scheduled for pid {target_pid} in {SIGNAL_DELAY_SECONDS}s")
    return outcome


def _validate_descriptor(stored: ProcessDescriptor) -> tuple[bool, str | None]:
    """Re-inspect the backing process and confirm it still matches the stored
    descriptor (PID, start_time, exe_path, cmdline)."""
    current = inspect(stored.pid)
    if not is_alive(stored.pid):
        return False, f"pid {stored.pid} no longer alive"
    if not stored.matches(current):
        return False, (
            f"descriptor mismatch — stored {stored.exe_path!r}@{stored.start_time}, "
            f"current {current.exe_path!r}@{current.start_time}"
        )
    return True, None


def _gate_check(record: SessionRecord) -> str | None:
    """Return a refused_reason for the gate state, or None if cleared to fire.

    Refused-reason discipline (Decision 11): two sentences max — evidence,
    then recourse. Gate-state prefix kept for self-containedness (the
    response also carries `confidence`, but refused_reason is often read
    in isolation). No general-explanation sentences.
    """
    if record.confidence is Confidence.INVALID:
        return (
            "INVALID — transport not alive, or kernel evidence is suspect "
            "(e.g. namespace mismatch). Run verify for "
            "the resolver evidence and warnings."
        )
    if record.confidence is Confidence.LOW:
        # Three sub-cases of LOW; surface the specific evidence for each.
        if record.drift_description is not None:
            return (
                f"LOW — descriptor drifted from launch baseline: "
                f"{record.drift_description}. Inspect the same evidence "
                "via verify or end_session(dry_run=True)."
            )
        if record.backing is None:
            return (
                "LOW — no Claude Code process identified in the parent "
                "chain. Run verify to see which candidates "
                "the resolver found and why none qualified."
            )
        return (
            "LOW — critical identity inspection failed (see "
            "status.gate_detail for which fields "
            "are missing). Inspect the same evidence via "
            "verify or end_session(dry_run=True)."
        )
    # HIGH — backing must be present (the determine_confidence path
    # guarantees this; defensive check for type-narrowing).
    if record.backing is None:
        return (
            "internal: confidence HIGH but backing is None — "
            "this should not happen; please file a bug."
        )
    return None
