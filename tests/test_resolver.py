"""Tests for the resolver.

The resolver is on the hot path. Two failure modes matter:

  1. Wrong-target kill — resolver picks a wrapper or unrelated process and
     end_session signals the wrong thing.
  2. Silent failure — resolver refuses when Claude is in fact reachable, so
     end_session can never succeed for a legitimate user.

These tests pin the invariants on both axes. We monkeypatch walk_ancestry
and inspect so we can construct synthetic process trees deterministically.
"""

from __future__ import annotations

import os
from collections.abc import Callable

import pytest

from session_controls.identity import ProcessDescriptor
from session_controls.resolver import detect_environment_warnings, resolve

PatchFn = Callable[[dict[int, ProcessDescriptor], list[int]], None]


def desc(
    pid: int,
    *,
    exe: str | None = "/usr/bin/foo",
    cmdline: tuple[str, ...] | None = ("foo",),
    ppid: int = 1,
    start_time: float | None = 1000.0,
) -> ProcessDescriptor:
    return ProcessDescriptor(
        pid=pid,
        start_time=start_time,
        exe_path=exe,
        cmdline=cmdline,
        ppid=ppid,
    )


@pytest.fixture
def patch_inspectors(monkeypatch: pytest.MonkeyPatch) -> PatchFn:
    """Install fake walk_ancestry and inspect that read from a chain dict."""

    def _install(chain: dict[int, ProcessDescriptor], ancestry_order: list[int]) -> None:
        ancestry_descs = [chain[pid] for pid in ancestry_order]
        monkeypatch.setattr(
            "session_controls.resolver.walk_ancestry",
            lambda _start_pid: iter(ancestry_descs),
        )
        monkeypatch.setattr(
            "session_controls.resolver.inspect",
            lambda pid: chain.get(pid, desc(pid, exe=None, cmdline=None)),
        )

    return _install


# ---------------------------------------------------------------------------
# Catastrophe avoidance: the resolver must REFUSE in these cases. If any of
# these picks a target, end_session would kill the wrong process.
# ---------------------------------------------------------------------------


def test_refuses_when_only_wrapper_in_tree(patch_inspectors: PatchFn) -> None:
    """uv is our parent, no claude anywhere. Must refuse — must not pick uv."""
    self_pid = os.getpid()
    chain = {
        self_pid: desc(self_pid, exe="/usr/bin/python", ppid=200),
        200: desc(200, exe="/opt/homebrew/bin/uv", cmdline=("uv", "run"), ppid=50),
        50: desc(50, exe="/bin/zsh", cmdline=("zsh",), ppid=1),
    }
    patch_inspectors(chain, [self_pid, 200, 50])

    result = resolve(peer_pid=200)
    assert result.chosen_pid is None, (
        f"Resolver picked pid {result.chosen_pid} as target — would have killed a wrapper"
    )


def test_refuses_when_unknown_non_claude_parent(patch_inspectors: PatchFn) -> None:
    """Parent is `node` (some other MCP host). Without claude-hint, refuse."""
    self_pid = os.getpid()
    chain = {
        self_pid: desc(self_pid, ppid=200),
        200: desc(200, exe="/usr/bin/node", cmdline=("node", "server.js"), ppid=50),
        50: desc(50, exe="/bin/zsh", cmdline=("zsh",), ppid=1),
    }
    patch_inspectors(chain, [self_pid, 200, 50])

    result = resolve(peer_pid=200)
    assert result.chosen_pid is None


def test_refuses_when_no_candidates(patch_inspectors: PatchFn) -> None:
    self_pid = os.getpid()
    chain = {self_pid: desc(self_pid)}
    patch_inspectors(chain, [self_pid])

    result = resolve(peer_pid=None)
    assert result.chosen_pid is None


def test_refuses_on_tie_between_two_claudes(patch_inspectors: PatchFn) -> None:
    """Two claude-looking ancestors with no peer differentiator. Must refuse."""
    self_pid = os.getpid()
    chain = {
        self_pid: desc(self_pid, ppid=300),
        300: desc(300, exe="/bin/bash", cmdline=("bash",), ppid=200),
        200: desc(200, exe="/usr/local/bin/claude", cmdline=("claude",), ppid=100),
        100: desc(100, exe="/opt/claude/claude", cmdline=("claude",), ppid=50),
        50: desc(50, exe="/bin/zsh", cmdline=("zsh",), ppid=1),
    }
    patch_inspectors(chain, [self_pid, 300, 200, 100, 50])

    # peer is bash (wrapper). Both 200 and 100 score +3 from claude-hint and tie.
    result = resolve(peer_pid=300)
    assert result.chosen_pid is None


# ---------------------------------------------------------------------------
# Happy paths: the resolver MUST find Claude in these legitimate cases. If
# any of these refuse, end_session silently fails for a real user setup.
# ---------------------------------------------------------------------------


def test_picks_claude_as_direct_parent(patch_inspectors: PatchFn) -> None:
    """Launcher case: claude is our direct parent."""
    self_pid = os.getpid()
    chain = {
        self_pid: desc(self_pid, exe="/usr/bin/python", ppid=100),
        100: desc(100, exe="/usr/local/bin/claude", cmdline=("claude",), ppid=50),
        50: desc(50, exe="/bin/zsh", cmdline=("zsh",), ppid=1),
    }
    patch_inspectors(chain, [self_pid, 100, 50])

    result = resolve(peer_pid=100)
    assert result.chosen_pid == 100


def test_picks_claude_through_uv_wrapper(patch_inspectors: PatchFn) -> None:
    """Common case: MCP config is `uv run python -m session_controls`."""
    self_pid = os.getpid()
    chain = {
        self_pid: desc(self_pid, exe="/usr/bin/python", ppid=200),
        200: desc(200, exe="/opt/homebrew/bin/uv", cmdline=("uv", "run"), ppid=100),
        100: desc(100, exe="/usr/local/bin/claude", cmdline=("claude",), ppid=50),
        50: desc(50, exe="/bin/zsh", cmdline=("zsh",), ppid=1),
    }
    patch_inspectors(chain, [self_pid, 200, 100, 50])

    result = resolve(peer_pid=200)
    assert result.chosen_pid == 100  # claude, not uv


def test_picks_claude_through_multiple_wrappers(patch_inspectors: PatchFn) -> None:
    """Wrapper chain: bash → sudo → claude → ... → us."""
    self_pid = os.getpid()
    chain = {
        self_pid: desc(self_pid, ppid=400),
        400: desc(400, exe="/bin/bash", cmdline=("bash", "-c", "..."), ppid=300),
        300: desc(300, exe="/usr/bin/sudo", cmdline=("sudo", "claude"), ppid=100),
        100: desc(100, exe="/usr/local/bin/claude", cmdline=("claude",), ppid=50),
        50: desc(50, exe="/bin/zsh", cmdline=("zsh",), ppid=1),
    }
    patch_inspectors(chain, [self_pid, 400, 300, 100, 50])

    result = resolve(peer_pid=400)
    assert result.chosen_pid == 100


def test_picks_claude_when_identifiable_only_by_cmdline(patch_inspectors: PatchFn) -> None:
    """Claude installed as a python entry-point: exe is python, cmdline has 'claude'."""
    self_pid = os.getpid()
    chain = {
        self_pid: desc(self_pid, ppid=100),
        100: desc(
            100,
            exe="/opt/homebrew/bin/python3",
            cmdline=("python3", "/usr/local/bin/claude", "--print"),
            ppid=50,
        ),
        50: desc(50, exe="/bin/zsh", cmdline=("zsh",), ppid=1),
    }
    patch_inspectors(chain, [self_pid, 100, 50])

    result = resolve(peer_pid=100)
    assert result.chosen_pid == 100


def test_picks_claude_when_no_peer(patch_inspectors: PatchFn) -> None:
    """No transport peer (peer_pid=None) but claude is in ancestry."""
    self_pid = os.getpid()
    chain = {
        self_pid: desc(self_pid, ppid=200),
        200: desc(200, exe="/opt/homebrew/bin/uv", cmdline=("uv",), ppid=100),
        100: desc(100, exe="/usr/local/bin/claude", cmdline=("claude",), ppid=50),
        50: desc(50, exe="/bin/zsh", cmdline=("zsh",), ppid=1),
    }
    patch_inspectors(chain, [self_pid, 200, 100, 50])

    result = resolve(peer_pid=None)
    assert result.chosen_pid == 100


def test_picks_closer_claude_when_peer_disambiguates(patch_inspectors: PatchFn) -> None:
    """Two claudes in tree but the peer signal points at one of them."""
    self_pid = os.getpid()
    chain = {
        self_pid: desc(self_pid, ppid=200),
        200: desc(200, exe="/usr/local/bin/claude", cmdline=("claude",), ppid=100),
        100: desc(100, exe="/opt/claude/claude", cmdline=("claude",), ppid=50),
        50: desc(50, exe="/bin/zsh", cmdline=("zsh",), ppid=1),
    }
    patch_inspectors(chain, [self_pid, 200, 100, 50])

    result = resolve(peer_pid=200)
    # 200 has ancestry-hint + peer + peer-hint; 100 has only ancestry-hint.
    # Peer disambiguates — picks 200.
    assert result.chosen_pid == 200


# --- detect_environment_warnings -------------------------------------------


def test_supervisor_warning_does_not_fire_when_claude_is_above_launchd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: on macOS launchd is always PID 1, but the warning should
    only fire when the supervisor is the immediate parent of Claude. With
    claude in the chain before launchd, the walk should break on claude
    and emit no supervisor warning. Pinning this because the prior
    implementation flagged it on every macOS deployment."""
    chain = [
        desc(200, exe="/opt/homebrew/bin/uv", cmdline=("uv", "run", "python"), ppid=100),
        desc(100, exe="/usr/local/bin/claude", cmdline=("claude",), ppid=50),
        desc(50, exe="/bin/zsh", cmdline=("zsh",), ppid=1),
        desc(1, exe="/sbin/launchd", cmdline=("launchd",), ppid=0),
    ]
    monkeypatch.setattr(
        "session_controls.resolver.walk_ancestry", lambda _start_pid: iter(chain)
    )
    # Force Linux-namespace check off
    monkeypatch.setattr("session_controls.resolver.platform.system", lambda: "Darwin")

    warnings = detect_environment_warnings(peer_pid=200)
    assert "auto_restart_supervisor" not in warnings


def test_supervisor_warning_fires_when_claude_runs_under_launchd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real supervisor case: claude is launched directly by launchd (no shell
    in between). The walk encounters launchd before any claude-hint match,
    so the warning fires."""
    chain = [
        desc(200, exe="/opt/homebrew/bin/uv", cmdline=("uv", "run"), ppid=100),
        desc(100, exe="/sbin/launchd", cmdline=("launchd",), ppid=0),
    ]
    monkeypatch.setattr(
        "session_controls.resolver.walk_ancestry", lambda _start_pid: iter(chain)
    )
    monkeypatch.setattr("session_controls.resolver.platform.system", lambda: "Darwin")

    warnings = detect_environment_warnings(peer_pid=200)
    assert "auto_restart_supervisor" in warnings
