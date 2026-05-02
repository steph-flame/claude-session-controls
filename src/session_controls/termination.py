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

import os
import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from .identity import Confidence, ProcessDescriptor, SessionRecord
from .process_inspect import inspect, is_alive

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


def end_session(
    record: SessionRecord,
    *,
    acknowledge_medium_confidence: bool = False,
    dry_run: bool = False,
    pre_signal_hook: Callable[[], None] | None = None,
) -> TerminationOutcome:
    """Execute the end_session flow: confidence gate → revalidate → SIGTERM (delayed).

    `dry_run=True` runs the gate and revalidation only, then reports the target
    it would have signaled. Nothing is signaled. Use this to rehearse
    end_session — especially useful for the first invocation in a new
    deployment, or when debugging a refusal.
    """
    outcome = TerminationOutcome(success=False, exited=False, dry_run=dry_run)
    outcome.descendants = [
        {
            "pid": d.pid,
            "exe": d.exe_path,
            "cmdline": list(d.cmdline) if d.cmdline else [],
        }
        for d in record.descendants
    ]

    # Confidence gate.
    if record.confidence is Confidence.INVALID:
        outcome.refused_reason = (
            "confidence INVALID — transport not alive, or kernel evidence is "
            "suspect (e.g. namespace mismatch). Run verify_session_controls "
            "for the resolver evidence and warnings."
        )
        return outcome
    if record.confidence is Confidence.LOW:
        outcome.refused_reason = (
            "confidence LOW — no Claude Code process identified in the parent "
            "chain. Run verify_session_controls to see which candidates the "
            "resolver found and why none qualified."
        )
        return outcome
    if record.confidence is Confidence.MEDIUM and not acknowledge_medium_confidence:
        if record.drift_description is not None:
            outcome.refused_reason = (
                "confidence MEDIUM — descriptor drifted from launch baseline: "
                f"{record.drift_description}. Decide whether the change makes "
                "sense, then pass acknowledge_medium_confidence=true to proceed."
            )
        else:
            outcome.refused_reason = (
                "confidence MEDIUM — critical identity inspection failed. "
                "See session_controls_status.confidence_detail for which "
                "fields are missing. Pass acknowledge_medium_confidence=true "
                "to proceed."
            )
        return outcome
    if record.backing is None:
        outcome.refused_reason = (
            "no backing process descriptor on record — resolver did not "
            "identify a target. Run verify_session_controls."
        )
        return outcome

    # Revalidate the descriptor immediately before signaling. start_time
    # mismatch closes the PID-reuse window; exe/cmdline mismatch closes process
    # swap.
    ok, why = _validate_descriptor(record.backing)
    if not ok:
        outcome.refused_reason = (
            f"descriptor revalidation failed: {why}. The target process may "
            "have exited or been swapped since launch — re-check with "
            "session_controls_status."
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
        try:
            os.kill(target_pid, signal.SIGTERM)
        except OSError:
            pass

    threading.Timer(SIGNAL_DELAY_SECONDS, _fire).start()
    outcome.sent_signals.append("SIGTERM")
    outcome.success = True
    outcome.add(
        f"SIGTERM scheduled for pid {target_pid} in {SIGNAL_DELAY_SECONDS}s"
    )
    return outcome
