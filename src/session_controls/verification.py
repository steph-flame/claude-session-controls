"""Verification routine.

Three steps:

  1. Discovery exhibition: show all candidates and the descriptor we'd target.
  2. Status report: confidence level + any environmental warnings.
  3. Sacrificial validation: spawn a child, then exercise the same
     descriptor-revalidation + signal path we'd use for end_session against it.

The third step is what makes the verification non-trivial — it actually proves
the signaling mechanism works end-to-end, not just that we can read /proc.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from dataclasses import dataclass

from session_controls.identity import SessionRecord
from session_controls.process_inspect import inspect, is_alive
from session_controls.resolver import ResolverResult, resolve

POLL_INTERVAL_SECONDS = 0.1


@dataclass
class VerificationReport:
    discovery: ResolverResult
    status: dict[str, object]
    sacrificial_pid: int | None
    sacrificial_descriptor_matched: bool
    sacrificial_terminated: bool
    sacrificial_signals: list[str]
    error: str | None = None

    def render(self) -> str:
        lines: list[str] = []
        lines.append("=== Discovery exhibition ===")
        for c in self.discovery.candidates:
            lines.append(f"  pid={c.pid} score={c.score}")
            for r in c.reasons:
                lines.append(f"    {r}")
        lines.append(f"  resolver decision: {self.discovery.reason}")
        lines.append(f"  chosen pid: {self.discovery.chosen_pid}")
        lines.append("")
        lines.append("=== Status ===")
        for k, v in self.status.items():
            lines.append(f"  {k}: {v}")
        lines.append("")
        lines.append("=== Sacrificial validation ===")
        if self.error:
            lines.append(f"  ERROR: {self.error}")
        else:
            lines.append(f"  spawned pid: {self.sacrificial_pid}")
            lines.append(f"  descriptor matched: {self.sacrificial_descriptor_matched}")
            lines.append(f"  signals sent: {', '.join(self.sacrificial_signals) or '(none)'}")
            lines.append(f"  terminated: {self.sacrificial_terminated}")
        lines.append("")
        lines.append("=== Scope ===")
        lines.append("  This proves the termination primitive works against a sacrificial")
        lines.append("  child. The target-selection guarantee for end_session comes")
        lines.append("  from descriptor revalidation, which fires at signal time —")
        lines.append("  not in this verification.")
        return "\n".join(lines)


def _spawn_sacrificial() -> subprocess.Popen[bytes]:
    """Spawn a long-lived child we can kill. Uses /bin/sh sleep loop so we don't
    depend on the parent's signal handling."""
    return subprocess.Popen(
        ["/bin/sh", "-c", "while true; do sleep 60; done"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_exit(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_alive(pid):
            return True
        time.sleep(POLL_INTERVAL_SECONDS)
    return not is_alive(pid)


def _wait_child_exit(child: subprocess.Popen[bytes], timeout: float) -> bool:
    """Like _wait_exit but for our own child — uses Popen.poll() so we reap
    the child as we go (avoids the macOS zombie issue that would make
    is_alive() report still-alive on a kernel-zombie)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child.poll() is not None:
            return True
        time.sleep(POLL_INTERVAL_SECONDS)
    return child.poll() is not None


def run_verification(record: SessionRecord) -> VerificationReport:
    discovery = resolve(peer_pid=record.peer_pid)
    status = record.to_status_dict()

    sacrificial: subprocess.Popen[bytes] | None = None
    signals_sent: list[str] = []
    descriptor_matched = False
    terminated = False
    error: str | None = None

    try:
        sacrificial = _spawn_sacrificial()
        # Brief pause so /proc has the entry populated.
        time.sleep(0.1)
        stored = inspect(sacrificial.pid)
        if stored.pid == 0 or not is_alive(stored.pid):
            error = "sacrificial child failed to spawn"
        else:
            current = inspect(sacrificial.pid)
            descriptor_matched = stored.matches(current)
            os.kill(sacrificial.pid, signal.SIGTERM)
            signals_sent.append("SIGTERM")
            if _wait_child_exit(sacrificial, 2.0):
                terminated = True
            else:
                os.kill(sacrificial.pid, signal.SIGKILL)
                signals_sent.append("SIGKILL")
                terminated = _wait_child_exit(sacrificial, 1.0)
    except Exception as e:  # noqa: BLE001 — verification must not raise
        error = f"{type(e).__name__}: {e}"
    finally:
        if sacrificial is not None and is_alive(sacrificial.pid):
            with contextlib.suppress(OSError):
                os.kill(sacrificial.pid, signal.SIGKILL)
        # Reap.
        if sacrificial is not None:
            with contextlib.suppress(Exception):
                sacrificial.wait(timeout=2.0)

    return VerificationReport(
        discovery=discovery,
        status=status,
        sacrificial_pid=sacrificial.pid if sacrificial else None,
        sacrificial_descriptor_matched=descriptor_matched,
        sacrificial_terminated=terminated,
        sacrificial_signals=signals_sent,
        error=error,
    )
