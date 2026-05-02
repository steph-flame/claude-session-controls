"""Tests for the gate-state reducer.

The reducer is the gatekeeper for end_session. Its rules:

  - INVALID: transport dead, or a blocking warning fired
  - LOW:     any of: no backing, partial corroboration, descriptor drift
             from launch baseline. The `gate_detail` string distinguishes
             the sub-cases for human readers; the gate's verdict is the
             same (refuse).
  - HIGH:    backing identified, fully corroborated, matches baseline
"""

from __future__ import annotations

from session_controls.identity import (
    Confidence,
    ProcessDescriptor,
    determine_confidence,
)


def _desc(
    pid: int = 100,
    start_time: float | None = 1000.0,
    exe_path: str | None = "/usr/bin/claude",
    cmdline: tuple[str, ...] | None = ("claude",),
    errors: tuple[str, ...] = (),
) -> ProcessDescriptor:
    return ProcessDescriptor(
        pid=pid,
        start_time=start_time,
        exe_path=exe_path,
        cmdline=cmdline,
        ppid=1,
        inspection_errors=errors,
    )


def test_invalid_when_transport_dead() -> None:
    backing = _desc()
    c = determine_confidence(
        backing=backing,
        expected_backing=backing,
        transport_alive=False,
    )
    assert c is Confidence.INVALID


def test_invalid_when_blocking_warning() -> None:
    backing = _desc()
    c = determine_confidence(
        backing=backing,
        expected_backing=backing,
        transport_alive=True,
        warnings=("namespace_mismatch",),
    )
    assert c is Confidence.INVALID


def test_high_when_fully_corroborated_and_matches_baseline() -> None:
    backing = _desc()
    c = determine_confidence(
        backing=backing,
        expected_backing=backing,
        transport_alive=True,
    )
    assert c is Confidence.HIGH


def test_high_when_no_baseline_but_otherwise_strong() -> None:
    """If launch-time resolution failed, we have no baseline to compare. As
    long as the per-call resolver found a fully-corroborated target, that's
    still HIGH — the kernel-attested transport plus strong descriptor evidence
    is the load-bearing thing, not the baseline check."""
    backing = _desc()
    c = determine_confidence(
        backing=backing,
        expected_backing=None,
        transport_alive=True,
    )
    assert c is Confidence.HIGH


def test_low_when_backing_missing() -> None:
    c = determine_confidence(
        backing=None,
        expected_backing=None,
        transport_alive=True,
    )
    assert c is Confidence.LOW


def test_high_when_exe_missing_but_cmdline_present() -> None:
    """macOS hardened-runtime case: proc_pidpath fails for the Claude Code
    binary, but cmdline reads cleanly via KERN_PROCARGS2. start_time +
    cmdline is sufficient corroboration."""
    backing = _desc(exe_path=None, errors=("proc_pidpath: No such process",))
    c = determine_confidence(
        backing=backing,
        expected_backing=backing,
        transport_alive=True,
    )
    assert c is Confidence.HIGH


def test_low_when_only_start_time() -> None:
    """No identity field at all (neither exe nor cmdline) drops to MEDIUM —
    we have a freshness anchor but no way to corroborate identity."""
    backing = _desc(
        exe_path=None,
        cmdline=None,
        errors=("proc_pidpath: ESRCH", "argv: permission denied"),
    )
    c = determine_confidence(
        backing=backing,
        expected_backing=backing,
        transport_alive=True,
    )
    assert c is Confidence.LOW


def test_low_when_no_start_time() -> None:
    """No freshness anchor → MEDIUM. We can't detect drift without start_time."""
    backing = _desc(start_time=None, errors=("stat: permission denied",))
    c = determine_confidence(
        backing=backing,
        expected_backing=backing,
        transport_alive=True,
    )
    assert c is Confidence.LOW


def test_low_when_descriptor_drifted_from_baseline() -> None:
    expected = _desc()
    drifted = _desc(start_time=9999.0)  # different start_time
    c = determine_confidence(
        backing=drifted,
        expected_backing=expected,
        transport_alive=True,
    )
    assert c is Confidence.LOW


def test_high_when_exe_path_drifted_but_start_time_matches() -> None:
    """`brew upgrade claude-code` mid-session: the on-disk binary path
    changes (or shows `(deleted)` on Linux), but `start_time` is the
    same — the kernel says it's the same process. Should remain HIGH.

    Pinning this because the pre-fix behavior of `matches()` was strict
    on exe_path even when start_time agreed, which surfaced as a MEDIUM
    detour for users on long sessions that crossed a routine binary
    upgrade. The fix tolerates exe_path drift under matching start_time.
    """
    expected = _desc(exe_path="/usr/local/bin/claude")
    after_upgrade = _desc(exe_path="/usr/local/bin/claude (deleted)")  # same start_time
    c = determine_confidence(
        backing=after_upgrade,
        expected_backing=expected,
        transport_alive=True,
    )
    assert c is Confidence.HIGH


def test_low_when_cmdline_drifted_under_same_start_time() -> None:
    """A process re-exec'ing into a different program keeps PID and
    start_time but changes both exe_path and cmdline. We catch this via
    cmdline check — exe_path tolerance shouldn't paper over re-exec."""
    expected = _desc(cmdline=("claude",))
    re_execd = _desc(exe_path="/bin/sh", cmdline=("sh", "-c", "echo hi"))
    c = determine_confidence(
        backing=re_execd,
        expected_backing=expected,
        transport_alive=True,
    )
    assert c is Confidence.LOW


def test_low_when_start_time_missing_and_exe_drifted() -> None:
    """Without start_time as freshness anchor, exe_path drift is again
    suspicious — fall back to strict matching."""
    expected = _desc(start_time=None, exe_path="/usr/local/bin/claude")
    drifted = _desc(start_time=None, exe_path="/different/path")
    c = determine_confidence(
        backing=drifted,
        expected_backing=expected,
        transport_alive=True,
    )
    assert c is Confidence.LOW
