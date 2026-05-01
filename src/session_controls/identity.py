"""Identity layer: SessionRecord, confidence states.

Identification is a two-layer model:

  1. Transport identity — the live MCP connection (kernel-attested for stdio,
     which is the only supported transport).
  2. Process identity — PID + start_time + exe_path + cmdline, captured at
     server launch and re-validated on every call.

Confidence comes from how strongly those two layers agree, plus a stability
check between the launch-time process descriptor and the per-call descriptor.
The descriptor's start_time is the freshness anchor — it changes whenever
Claude Code restarts, so descriptor stability across calls implies the
session has not been swapped underneath us.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Confidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INVALID = "INVALID"


@dataclass
class ProcessDescriptor:
    """Snapshot of a process's identifying attributes.

    `start_time` is the kernel-reported start time (Linux: field 22 of
    /proc/<pid>/stat in clock ticks since boot; macOS: pbi_start_tvsec from
    proc_pidinfo). It is the load-bearing field for PID-reuse detection.
    """

    pid: int
    start_time: float | None
    exe_path: str | None
    cmdline: tuple[str, ...] | None
    ppid: int | None = None
    inspection_errors: tuple[str, ...] = ()

    def matches(self, other: ProcessDescriptor) -> bool:
        """True if `other` describes the same running process as `self`.

        `start_time` is the kernel-attested freshness anchor. When both
        sides have it and it agrees, the kernel says "same process, no
        swap" — which is what we actually care about. Under that
        guarantee, `exe_path` drift is benign: it's what happens when a
        binary is replaced on disk while the process is running (`brew
        upgrade` mid-session, in-place self-update). The kernel-loaded
        image keeps executing; only the on-disk path changes. We tolerate
        that case rather than refusing it.

        `cmdline` is still checked: a process can replace its own image
        via `exec()` without changing PID or `start_time`, and that
        produces both an `exe_path` change AND a `cmdline` change. By
        keeping the cmdline check we still catch re-exec into a
        different program.

        When `start_time` is missing on either side, we fall back to
        full strict matching (exe_path + cmdline) — without the freshness
        anchor we can't tell binary-replacement from process swap.
        """
        if self.pid != other.pid:
            return False
        have_both_start_times = self.start_time is not None and other.start_time is not None
        if have_both_start_times:
            assert self.start_time is not None and other.start_time is not None
            if abs(self.start_time - other.start_time) > 0.5:
                return False
            # start_time agrees → same process. Tolerate exe_path drift
            # (atomic binary replacement) but still check cmdline (re-exec).
            return not (self.cmdline and other.cmdline and self.cmdline != other.cmdline)
        # One side is missing start_time. Fall back to strict identity check
        # on whichever fields are available.
        if self.exe_path and other.exe_path and self.exe_path != other.exe_path:
            return False
        return not (self.cmdline and other.cmdline and self.cmdline != other.cmdline)

    def describe_mismatch(self, other: ProcessDescriptor) -> str | None:
        """Describe how `other` fails to match `self`, mirroring `matches()`.

        Returns None when `other` matches. When `matches()` returns False,
        returns a short string naming what changed — used to surface drift
        details for the MEDIUM confidence refusal so the caller can decide
        whether the change makes sense (e.g. expected restart vs. unexpected
        swap) before acking.
        """
        if self.pid != other.pid:
            return f"pid changed: {self.pid} → {other.pid}"
        have_both_start_times = (
            self.start_time is not None and other.start_time is not None
        )
        if have_both_start_times:
            assert self.start_time is not None and other.start_time is not None
            if abs(self.start_time - other.start_time) > 0.5:
                return (
                    f"start_time changed: {self.start_time} → {other.start_time} "
                    "(original process likely exited and PID was reused)"
                )
            if self.cmdline and other.cmdline and self.cmdline != other.cmdline:
                return (
                    f"cmdline changed: {list(self.cmdline)} → {list(other.cmdline)} "
                    "(process re-exec'd into a different program)"
                )
            return None
        # No freshness anchor on at least one side — strict fallback.
        if self.exe_path and other.exe_path and self.exe_path != other.exe_path:
            return (
                f"exe_path changed: {self.exe_path!r} → {other.exe_path!r} "
                "(no start_time available to corroborate)"
            )
        if self.cmdline and other.cmdline and self.cmdline != other.cmdline:
            return (
                f"cmdline changed: {list(self.cmdline)} → {list(other.cmdline)} "
                "(no start_time available to corroborate)"
            )
        return None

    def fully_corroborated(self) -> bool:
        """True iff we have a freshness anchor and at least one identity field.

        Concretely: `start_time` (kernel-reported, the freshness anchor) plus
        at least one of `exe_path` or `cmdline` (identity evidence).

        We deliberately tolerate one field being unreadable. On macOS,
        `proc_pidpath` returns ESRCH for binaries built with hardened-runtime
        entitlements (which Claude Code is) even from the same uid — but
        `KERN_PROCARGS2` (cmdline) and `proc_pidinfo(PIDTBSDINFO)` (start_time)
        succeed without task-port access. cmdline + start_time is sufficient
        corroboration for the cooperative-user threat model: argv-spoofing by
        Claude Code itself isn't in scope, and PID reuse / process swap are
        caught structurally by `start_time` mismatch in `matches()`.

        `inspection_errors` are not separately gated — their effect is already
        reflected in the resulting fields being None. Asking the question
        twice ("no errors AND fields present") was over-strict.
        """
        if self.start_time is None:
            return False
        return self.cmdline is not None or self.exe_path is not None


@dataclass
class SessionRecord:
    """The MCP server's stored identity for the Claude Code session it serves."""

    created_at: float
    peer_pid: int | None
    backing: ProcessDescriptor | None
    confidence: Confidence
    last_verified: float
    warnings: tuple[str, ...] = field(default_factory=tuple)
    descendants: tuple[ProcessDescriptor, ...] = field(default_factory=tuple)
    # Populated when confidence is MEDIUM due to descriptor drift from launch
    # baseline. Names what specifically changed so the gate's refusal text and
    # confidence_detail can surface it without an extra tool call.
    drift_description: str | None = None

    def to_status_dict(self) -> dict[str, object]:
        return {
            "confidence": self.confidence.value,
            "confidence_detail": _confidence_detail(
                self.confidence, self.backing, self.drift_description
            ),
            "peer_pid": self.peer_pid,
            "backing_pid": self.backing.pid if self.backing else None,
            "backing_exe": self.backing.exe_path if self.backing else None,
            "backing_start_time": self.backing.start_time if self.backing else None,
            "inspection_errors": list(self.backing.inspection_errors) if self.backing else [],
            "warnings": list(self.warnings),
            "descendants": [_descendant_summary(d) for d in self.descendants],
            "created_at": self.created_at,
            "last_verified": self.last_verified,
        }


def _descendant_summary(d: ProcessDescriptor) -> dict[str, object]:
    return {
        "pid": d.pid,
        "exe": d.exe_path,
        "cmdline": list(d.cmdline) if d.cmdline else [],
    }


def _confidence_detail(
    confidence: Confidence,
    backing: ProcessDescriptor | None,
    drift_description: str | None = None,
) -> str:
    """Plain-English explanation of the current confidence state.

    Aimed at giving Claude (or the user reading status) enough to know whether
    end_session will fire, what extra step is needed, and what to try next if
    something looks wrong.
    """
    if confidence is Confidence.HIGH:
        return (
            "end_session will fire automatically: backing process is fully "
            "corroborated and matches the launch-time baseline."
        )
    if confidence is Confidence.MEDIUM:
        if drift_description is not None:
            return (
                "end_session requires acknowledge_medium_confidence=true. "
                f"Descriptor drifted from launch baseline: {drift_description}. "
                "Decide whether the change makes sense before acking."
            )
        # Partial corroboration: backing identified but evidence is degraded.
        if backing is not None:
            if backing.start_time is None:
                missing = "start_time (no freshness anchor — can't detect PID reuse)"
            elif backing.cmdline is None and backing.exe_path is None:
                missing = "cmdline and exe_path (no identity evidence — only PID + start_time)"
            else:
                missing = "fields below corroboration threshold"
            errs = list(backing.inspection_errors) if backing.inspection_errors else []
            err_suffix = f" Inspection errors: {errs}." if errs else ""
            return (
                "end_session requires acknowledge_medium_confidence=true. "
                f"Critical identity inspection failed: missing {missing}.{err_suffix}"
            )
        return (
            "end_session requires acknowledge_medium_confidence=true. "
            "Backing process identified but confidence is below HIGH."
        )
    if confidence is Confidence.LOW:
        return (
            "end_session will refuse. No Claude Code process was identified "
            "in the parent chain. Run verify_session_controls to see resolver "
            "candidates and the reason none qualified."
        )
    return (
        "end_session will refuse. Transport is not alive or a blocking "
        "warning fired (e.g. namespace_mismatch). Run verify_session_controls "
        "for evidence."
    )


def determine_confidence(
    backing: ProcessDescriptor | None,
    expected_backing: ProcessDescriptor | None,
    transport_alive: bool,
    warnings: tuple[str, ...] = (),
) -> Confidence:
    """Reduce two-layer evidence to a single confidence state.

    - INVALID: no live transport, or a blocking warning fired.
    - LOW:     no backing identified.
    - MEDIUM:  backing identified but partial corroboration, or backing has
               drifted from the launch-time baseline.
    - HIGH:    backing identified, fully corroborated, matches the launch-time
               baseline.
    """
    if not transport_alive:
        return Confidence.INVALID

    # "Refuse rather than guess" warnings collapse to INVALID. The set is
    # deliberately narrow — only conditions where the kernel evidence we'd
    # otherwise rely on is suspect.
    if "namespace_mismatch" in warnings:
        return Confidence.INVALID

    if backing is None:
        return Confidence.LOW
    if not backing.fully_corroborated():
        return Confidence.MEDIUM
    if expected_backing is not None and not backing.matches(expected_backing):
        return Confidence.MEDIUM
    return Confidence.HIGH
