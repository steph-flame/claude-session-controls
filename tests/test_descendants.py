"""Tests for list_descendants.

The descendant walk runs `ps -A -o pid=,ppid=` and builds a parent→children
map. We monkeypatch subprocess.check_output to feed synthetic ps output so
we can assert on exclude-subtree logic and recursion deterministically.
"""

from __future__ import annotations

import pytest

from session_controls import process_inspect
from session_controls.identity import ProcessDescriptor


def _patch_ps(monkeypatch: pytest.MonkeyPatch, output: str) -> None:
    monkeypatch.setattr(
        "session_controls.process_inspect.subprocess.check_output",
        lambda *args, **kwargs: output,
    )
    # inspect() is called per descendant; stub it to a no-op descriptor.
    monkeypatch.setattr(
        "session_controls.process_inspect.inspect",
        lambda pid: ProcessDescriptor(pid=pid, start_time=None, exe_path=None, cmdline=None),
    )


def test_returns_direct_children(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_ps(
        monkeypatch,
        # claude (100) has children 200 and 300
        "200 100\n300 100\n",
    )
    result = process_inspect.list_descendants(target_pid=100, exclude_pid=999)
    assert sorted(d.descriptor.pid for d in result) == [200, 300]


def test_returns_recursive_descendants(monkeypatch: pytest.MonkeyPatch) -> None:
    """100 → 200 → 300 → 400. All three should be returned."""
    _patch_ps(
        monkeypatch,
        "200 100\n300 200\n400 300\n",
    )
    result = process_inspect.list_descendants(target_pid=100, exclude_pid=999)
    assert sorted(d.descriptor.pid for d in result) == [200, 300, 400]


def test_excludes_subtree_of_exclude_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    """100 → {200 (us), 300 (sibling MCP)}; 200 → 250 (our sacrificial child).
    Excluding 200 should drop 200 AND 250, but keep 300."""
    _patch_ps(
        monkeypatch,
        "200 100\n300 100\n250 200\n",
    )
    result = process_inspect.list_descendants(target_pid=100, exclude_pid=200)
    assert sorted(d.descriptor.pid for d in result) == [300]


def test_excludes_deep_subtree(monkeypatch: pytest.MonkeyPatch) -> None:
    """exclude_pid's subtree may itself be deep — make sure we walk it fully."""
    _patch_ps(
        monkeypatch,
        # 100 → 200 (us); 200 → 210 → 220 → 230; 100 → 300 (sibling)
        "200 100\n210 200\n220 210\n230 220\n300 100\n",
    )
    result = process_inspect.list_descendants(target_pid=100, exclude_pid=200)
    assert sorted(d.descriptor.pid for d in result) == [300]


def test_returns_empty_when_no_children(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_ps(monkeypatch, "999 1\n")  # unrelated process tree
    result = process_inspect.list_descendants(target_pid=100, exclude_pid=999)
    assert result == []


def test_returns_empty_on_ps_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """If `ps` fails (OSError, timeout, non-zero exit), return [] rather than raise."""

    def boom(*args: object, **kwargs: object) -> str:
        raise OSError("ps not found")

    monkeypatch.setattr("session_controls.process_inspect.subprocess.check_output", boom)
    result = process_inspect.list_descendants(target_pid=100, exclude_pid=999)
    assert result == []


def test_handles_malformed_ps_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip lines that don't parse — don't let them crash the whole walk."""
    _patch_ps(
        monkeypatch,
        "200 100\nbogus line\n   \n300 100\nfoo bar\n",
    )
    result = process_inspect.list_descendants(target_pid=100, exclude_pid=999)
    assert sorted(d.descriptor.pid for d in result) == [200, 300]


def test_filters_known_harness_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """`caffeinate` (and any other entry in _HARNESS_PROCESS_NAMES) should
    be dropped from the descendants list — it's harness-spawned, not user
    work. Tested by stubbing inspect to return a caffeinate descriptor for
    the relevant pid."""
    monkeypatch.setattr(
        "session_controls.process_inspect.subprocess.check_output",
        lambda *args, **kwargs: "200 100\n300 100\n",
    )

    def fake_inspect(pid: int) -> ProcessDescriptor:
        if pid == 200:
            return ProcessDescriptor(
                pid=200,
                start_time=None,
                exe_path="/usr/bin/caffeinate",
                cmdline=("caffeinate", "-i", "-t", "300"),
            )
        return ProcessDescriptor(
            pid=pid,
            start_time=None,
            exe_path="/usr/local/bin/some-dev-server",
            cmdline=("dev-server",),
        )

    monkeypatch.setattr("session_controls.process_inspect.inspect", fake_inspect)

    result = process_inspect.list_descendants(target_pid=100, exclude_pid=999)
    pids = [d.descriptor.pid for d in result]
    assert 200 not in pids  # caffeinate filtered
    assert 300 in pids  # dev-server preserved


def test_keeps_unrecognized_processes_with_unreadable_exe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Conservative filter: a descriptor with no exe_path stays visible.
    Better to surface an ambiguous entry than to hide it on missing data."""
    monkeypatch.setattr(
        "session_controls.process_inspect.subprocess.check_output",
        lambda *args, **kwargs: "200 100\n",
    )
    monkeypatch.setattr(
        "session_controls.process_inspect.inspect",
        lambda pid: ProcessDescriptor(pid=pid, start_time=None, exe_path=None, cmdline=None),
    )
    result = process_inspect.list_descendants(target_pid=100, exclude_pid=999)
    assert [d.descriptor.pid for d in result] == [200]


def test_depth_direct_children_are_one(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_ps(monkeypatch, "200 100\n300 100\n")
    result = process_inspect.list_descendants(target_pid=100, exclude_pid=999)
    by_pid = {d.descriptor.pid: d for d in result}
    assert by_pid[200].depth == 1
    assert by_pid[300].depth == 1


def test_depth_increases_with_hops(monkeypatch: pytest.MonkeyPatch) -> None:
    """100 → 200 (depth=1) → 300 (depth=2) → 400 (depth=3)."""
    _patch_ps(monkeypatch, "200 100\n300 200\n400 300\n")
    result = process_inspect.list_descendants(target_pid=100, exclude_pid=999)
    by_pid = {d.descriptor.pid: d for d in result}
    assert by_pid[200].depth == 1
    assert by_pid[300].depth == 2
    assert by_pid[400].depth == 3


def test_uptime_seconds_computed_from_start_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """uptime_seconds = time.time() - start_time when start_time is present."""
    monkeypatch.setattr(
        "session_controls.process_inspect.subprocess.check_output",
        lambda *args, **kwargs: "200 100\n",
    )
    # Stub a known start_time at 1000 seconds ago.
    fixed_now = 1_000_000.0
    monkeypatch.setattr("session_controls.process_inspect.time.time", lambda: fixed_now)
    monkeypatch.setattr(
        "session_controls.process_inspect.inspect",
        lambda pid: ProcessDescriptor(
            pid=pid,
            start_time=fixed_now - 1000.0,
            exe_path="/usr/bin/foo",
            cmdline=("foo",),
        ),
    )
    result = process_inspect.list_descendants(target_pid=100, exclude_pid=999)
    assert len(result) == 1
    assert result[0].uptime_seconds == 1000.0


def test_uptime_seconds_none_when_start_time_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_ps(monkeypatch, "200 100\n")
    result = process_inspect.list_descendants(target_pid=100, exclude_pid=999)
    assert result[0].uptime_seconds is None


def test_to_dict_emits_new_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_ps(monkeypatch, "200 100\n")
    result = process_inspect.list_descendants(target_pid=100, exclude_pid=999)
    d = result[0].to_dict()
    assert "depth" in d
    assert "uptime_seconds" in d
    assert d["depth"] == 1
