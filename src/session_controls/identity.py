"""Identity layer: SessionRecord, gate states.

Identification is a two-layer model:

  1. Transport identity — the live MCP connection (kernel-attested for stdio,
     which is the only supported transport).
  2. Process identity — PID + start_time + exe_path + cmdline, captured at
     server launch and re-validated on every call.

The gate state reflects how strongly those two layers agree, plus a stability
check between the launch-time process descriptor and the per-call descriptor.
The descriptor's start_time is the freshness anchor — it changes whenever
Claude Code restarts, so descriptor stability across calls implies the
session has not been swapped underneath us.

The gate has three states: HIGH (fire), LOW (refuse, with a specific
reason), INVALID (refuse, transport-level failure). There is no override.
The cases that previously fired MEDIUM-with-acknowledgment now refuse —
under adversarial conditions the acknowledgment functioned as ceremony, and
the asymmetric error structure (false-fire on PID reuse vs failed exit)
favors refusing on suspect identity. Claude inspects the refusal reason
via the gate_detail string and can independently confirm the same
conclusion via `dry_run` or `verify_session_controls`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Confidence(StrEnum):
    """The gate's verdict on whether end_session can fire.

    HIGH:    fire automatically. Backing process fully corroborated and
             matches launch-time baseline.
    LOW:     refuse. Either no backing was identified, or the backing's
             identity evidence is degraded (missing freshness anchor or
             both identity fields), or the backing has drifted from the
             launch-time baseline. The `gate_detail` string and (where
             applicable) `drift_description` name the specific reason.
    INVALID: refuse. Transport-level failure — peer reparented to init,
             namespace mismatch, etc.
    """

    HIGH = "HIGH"
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
        details in the refusal text and `gate_detail` so Claude can read
        the specific evidence and judge whether the gate's refusal makes
        sense.
        """
        if self.pid != other.pid:
            return f"pid changed: {self.pid} → {other.pid}"
        have_both_start_times = self.start_time is not None and other.start_time is not None
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
        `proc_pidpath` *often* returns ESRCH for Claude Code (which ships
        with hardened-runtime entitlements: allow-jit,
        allow-unsigned-executable-memory, disable-library-validation), even
        from the same uid — but `KERN_PROCARGS2` (cmdline) and
        `proc_pidinfo(PIDTBSDINFO)` (start_time) succeed without
        task-port access. cmdline + start_time is sufficient corroboration
        for the cooperative-user threat model: argv-spoofing by Claude Code
        itself isn't in scope, and PID reuse / process swap are caught
        structurally by `start_time` mismatch in `matches()`.

        The "often" qualifier is intentional — the proc_pidpath behavior on
        Claude Code isn't fully deterministic in practice. User-research
        evidence on 2026-05-03 surfaced two sessions on the same conversation
        where one returned ESRCH and the other returned the path
        successfully. The entitlement is part of the cause but doesn't fully
        determine the outcome; binary-replacement timing (kernel-tracked
        launch-inode no longer matching the on-disk inode after a brew
        upgrade) is a plausible additional factor, and there may be others
        (launch-context, system state). Both outcomes (ESRCH and successful
        read) are handled gracefully here — when proc_pidpath succeeds, we
        use the path; when it fails, the cmdline+start_time path still
        corroborates.

        `inspection_errors` are not separately gated — their effect is already
        reflected in the resulting fields being None. Asking the question
        twice ("no errors AND fields present") was over-strict.
        """
        if self.start_time is None:
            return False
        return self.cmdline is not None or self.exe_path is not None


@dataclass(frozen=True)
class DescendantInfo:
    """Per-descendant info surfaced in status and end_session responses.

    Bundles the ProcessDescriptor with structural and timing context that
    helps Claude attribute the descendant. The two added signals over a
    bare descriptor:

    - `depth`: hops from the target process. Direct children of Claude Code
      are depth=1 (typically sibling MCP servers, run_in_background bash
      jobs); deeper descendants are usually grandchildren spawned by those.
    - `uptime_seconds`: how long the process has been running. Long uptime
      relative to the Claude Code session is a soft signal that the process
      is user-managed work that pre-dates or outlives this session;
      short uptime is consistent with sibling-spawn-during-session.

    Combined with the descriptor's exe/cmdline, these let Claude make
    better attribution calls without asking — distinguishing "the user's
    30-minute load test" from "a sibling MCP server that started with the
    session" without having to interrogate the user.
    """

    descriptor: ProcessDescriptor
    depth: int
    uptime_seconds: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "pid": self.descriptor.pid,
            "exe": self.descriptor.exe_path,
            "cmdline": list(self.descriptor.cmdline) if self.descriptor.cmdline else [],
            "depth": self.depth,
            "uptime_seconds": self.uptime_seconds,
        }


@dataclass
class SessionRecord:
    """The MCP server's stored identity for the Claude Code session it serves."""

    created_at: float
    peer_pid: int | None
    backing: ProcessDescriptor | None
    confidence: Confidence
    last_verified: float
    warnings: tuple[str, ...] = field(default_factory=tuple)
    descendants: tuple[DescendantInfo, ...] = field(default_factory=tuple)
    # Populated when LOW is triggered by descriptor drift from the launch
    # baseline. Names what specifically changed so the refusal text and
    # gate_detail can surface it without an extra tool call.
    drift_description: str | None = None

    def to_status_dict(self) -> dict[str, object]:
        return {
            "confidence": self.confidence.value,
            "gate_detail": _gate_detail(self.confidence, self.backing, self.drift_description),
            "peer_pid": self.peer_pid,
            "backing_pid": self.backing.pid if self.backing else None,
            "backing_exe": self.backing.exe_path if self.backing else None,
            "backing_start_time": self.backing.start_time if self.backing else None,
            "inspection_errors": list(self.backing.inspection_errors) if self.backing else [],
            "warnings": list(self.warnings),
            "descendants": [d.to_dict() for d in self.descendants],
            "created_at": self.created_at,
            "last_verified": self.last_verified,
        }


def _gate_detail(
    confidence: Confidence,
    backing: ProcessDescriptor | None,
    drift_description: str | None = None,
) -> str:
    """The why behind the current gate state, in a predictable shape.

    Discipline (per Decision 11): two sentences max — evidence, then
    recourse. No general-explanation sentences. The `confidence` field
    carries the verdict (HIGH/LOW/INVALID); this field carries what
    specifically happened and what Claude can do to confirm or judge it.
    HIGH has no recourse to name — one evidence sentence.
    """
    if confidence is Confidence.HIGH:
        return "Backing process is fully corroborated and matches the launch-time baseline."
    if confidence is Confidence.LOW:
        # LOW has three sub-cases — distinguish them so Claude can read
        # the specific evidence rather than a generic "low confidence" line.
        if drift_description is not None:
            return (
                f"Descriptor drifted from launch baseline: {drift_description}. "
                "Inspect the same evidence via `verify_session_controls` or "
                "`end_session(dry_run=True)`."
            )
        if backing is None:
            return (
                "No Claude Code process was identified in the parent chain. "
                "Run `verify_session_controls` to see resolver candidates "
                "and the reason none qualified."
            )
        # Partial-corroboration sub-case: backing identified but evidence
        # is degraded enough that we can't safely target it.
        if backing.start_time is None:
            missing = "start_time (no freshness anchor — can't detect PID reuse)"
        elif backing.cmdline is None and backing.exe_path is None:
            missing = "cmdline and exe_path (no identity evidence — only PID + start_time)"
        else:
            missing = "fields below corroboration threshold"
        errs = list(backing.inspection_errors) if backing.inspection_errors else []
        err_suffix = f"; inspection errors: {errs}" if errs else ""
        return (
            f"Critical identity inspection failed: missing {missing}{err_suffix}. "
            "Inspect the same evidence via `verify_session_controls` or "
            "`end_session(dry_run=True)`."
        )
    return (
        "Transport is not alive or a blocking warning fired "
        "(e.g. namespace_mismatch). Run `verify_session_controls` for evidence."
    )


def determine_confidence(
    backing: ProcessDescriptor | None,
    expected_backing: ProcessDescriptor | None,
    transport_alive: bool,
    warnings: tuple[str, ...] = (),
) -> Confidence:
    """Reduce two-layer evidence to a gate state.

    - INVALID: no live transport, or a blocking warning fired.
    - LOW:     any of:
                 - no backing identified
                 - backing identified but partial corroboration
                 - backing has drifted from the launch-time baseline
               Refusal text names which sub-case fired.
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
        return Confidence.LOW
    if expected_backing is not None and not backing.matches(expected_backing):
        return Confidence.LOW
    return Confidence.HIGH
