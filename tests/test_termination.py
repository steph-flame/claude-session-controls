"""Tests for the termination flow.

These cover the gate refusal paths and the dry-run path. They do NOT
actually signal any process — for live signaling, see the verification
routine's sacrificial-child mechanism.
"""

from __future__ import annotations

import os
import time

from session_controls.identity import (
    Confidence,
    ProcessDescriptor,
    SessionRecord,
)
from session_controls.process_inspect import inspect
from session_controls.termination import end_session


def _record_with(
    confidence: Confidence,
    backing: ProcessDescriptor | None = None,
) -> SessionRecord:
    return SessionRecord(
        created_at=time.time(),
        peer_pid=os.getppid(),
        backing=backing,
        confidence=confidence,
        last_verified=time.time(),
    )


def test_refuses_invalid() -> None:
    out = end_session(_record_with(Confidence.INVALID))
    assert not out.success
    assert out.refused_reason is not None
    assert "INVALID" in out.refused_reason


def test_refuses_low() -> None:
    out = end_session(_record_with(Confidence.LOW))
    assert not out.success
    assert out.refused_reason is not None
    assert "LOW" in out.refused_reason


def test_refuses_low_with_drift_description() -> None:
    """When LOW is triggered by descriptor drift, refused_reason names the
    specific change. (Decision 10 collapsed the old MEDIUM-with-ack path
    into LOW; the drift-specific text is what was previously on the MEDIUM
    refusal.)"""
    record = SessionRecord(
        created_at=time.time(),
        peer_pid=os.getppid(),
        backing=inspect(os.getpid()),
        confidence=Confidence.LOW,
        last_verified=time.time(),
        drift_description="cmdline changed: ['claude'] → ['sh', '-c', 'echo hi']",
    )
    out = end_session(record)
    assert not out.success
    assert out.refused_reason is not None
    assert "LOW" in out.refused_reason
    assert "drifted" in out.refused_reason
    assert "cmdline changed" in out.refused_reason


def test_dry_run_succeeds_without_signaling() -> None:
    """Dry-run on the current process should clear Phase 1 and report the
    target PID without actually sending any signals. We use os.getpid() as the
    'backing' so descriptor revalidation succeeds against a live process."""
    backing = inspect(os.getpid())
    record = _record_with(Confidence.HIGH, backing=backing)
    out = end_session(record, dry_run=True)
    assert out.success
    assert out.dry_run
    assert out.would_target_pid == os.getpid()
    assert out.sent_signals == []
    assert not out.exited
    assert out.refused_reason is None


def test_dry_run_still_refuses_at_low_confidence() -> None:
    """Dry-run goes through the same Phase 1 gate as the real call."""
    out = end_session(_record_with(Confidence.LOW), dry_run=True)
    assert not out.success
    assert out.dry_run
    assert out.refused_reason is not None
    assert out.would_target_pid is None


def test_dry_run_refuses_on_descriptor_mismatch() -> None:
    """A backing descriptor that won't revalidate against the live process
    should refuse, even in dry-run."""
    bad_backing = ProcessDescriptor(
        pid=os.getpid(),
        start_time=1.0,  # bogus — won't match live start_time
        exe_path="/nonexistent/path",
        cmdline=("nope",),
        ppid=1,
    )
    record = _record_with(Confidence.HIGH, backing=bad_backing)
    out = end_session(record, dry_run=True)
    assert not out.success
    assert out.refused_reason is not None
    assert "descriptor revalidation failed" in out.refused_reason
