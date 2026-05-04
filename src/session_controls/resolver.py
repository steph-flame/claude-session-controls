"""Identity resolver — finds the Claude Code PID via ancestry walk + peer PID.

We have to *infer* which process is the Claude Code session that owns the MCP
transport. The resolver gathers candidates from two signals (spawn ancestry,
stdio peer) and scores them.

Stdio is the only transport, so the peer is kernel-attested as a real parent
in our process chain — that's what makes this safe enough to act on.

Resolver outputs:
  - Best candidate (or None if no candidate qualifies).
  - Reason string ("ok", "no candidates", "below threshold", "tie", ...).
  - Full evidence chain (used by the verification routine).
"""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass, field

from session_controls.process_inspect import inspect, walk_ancestry

# Known shells/launchers/wrappers we walk *through* but don't treat as the target.
SKIP_NAMES = frozenset(
    {
        "bash",
        "sh",
        "zsh",
        "fish",
        "dash",
        "ksh",
        "uvx",
        "uv",
        "pyenv",
        "asdf",
        "direnv",
        "env",
        "sudo",
        "doas",
        "tmux",
        "tmux: server",
        "screen",
        "login",
    }
)

# Substrings in argv[0] / exe basename that indicate a likely Claude Code process.
CLAUDE_HINTS = ("claude", "claude-code")

# Score thresholds.
ABS_THRESHOLD = 2  # candidate must score at least this to be eligible
MARGIN = 1  # winner must beat runner-up by at least this margin


@dataclass
class Candidate:
    pid: int
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    has_hint: bool = False

    def add(self, points: int, reason: str, *, hint: bool = False) -> None:
        self.score += points
        self.reasons.append(f"+{points} {reason}")
        if hint:
            self.has_hint = True


@dataclass
class ResolverResult:
    chosen_pid: int | None
    reason: str
    candidates: list[Candidate]


def _basename(path: str | None) -> str:
    if not path:
        return ""
    return os.path.basename(path)


def _looks_like_claude(exe: str | None, cmdline: tuple[str, ...] | None) -> bool:
    if cmdline:
        joined = " ".join(cmdline).lower()
        if any(h in joined for h in CLAUDE_HINTS):
            return True
    base = _basename(exe).lower()
    return any(h in base for h in CLAUDE_HINTS)


def resolve(*, peer_pid: int | None) -> ResolverResult:
    """Return the resolver's best guess at the Claude Code PID.

    The resolver is conservative: it returns no chosen_pid unless a single
    candidate clears the absolute threshold *and* beats all others by MARGIN.
    Positive Claude identification (hint match) is required.
    """
    candidates: dict[int, Candidate] = {}

    def cand(pid: int) -> Candidate:
        if pid not in candidates:
            candidates[pid] = Candidate(pid=pid)
        return candidates[pid]

    # 1. Spawn ancestry walk from our own PID, skipping known wrappers.
    self_pid = os.getpid()
    for desc in walk_ancestry(self_pid):
        if desc.pid == self_pid:
            continue
        name = _basename(desc.exe_path)
        if name in SKIP_NAMES:
            cand(desc.pid).add(0, f"ancestor {desc.pid} ({name}): skipped (wrapper)")
            continue
        if _looks_like_claude(desc.exe_path, desc.cmdline):
            cand(desc.pid).add(3, f"ancestor {desc.pid}: argv/exe matches claude hint", hint=True)
        else:
            cand(desc.pid).add(1, f"ancestor {desc.pid} ({name}): plausible parent")

    # 2. Stdio transport peer (kernel-attested parent).
    # Gated on the peer NOT being a wrapper — without this, a `uv run python -m
    # session_controls` invocation lets uv clear threshold on its own and
    # end_session would target uv itself.
    if peer_pid is not None:
        peer_desc = inspect(peer_pid)
        peer_name = _basename(peer_desc.exe_path)
        if peer_name in SKIP_NAMES:
            cand(peer_pid).add(0, f"peer_pid={peer_pid} ({peer_name}): wrapper, no peer credit")
        else:
            c = cand(peer_pid)
            c.add(2, f"peer_pid={peer_pid}: stdio peer")
            if _looks_like_claude(peer_desc.exe_path, peer_desc.cmdline):
                c.add(2, "peer matches claude hint", hint=True)
            if peer_desc.inspection_errors:
                c.add(0, f"peer inspection errors: {peer_desc.inspection_errors}")

    return _select_winner(candidates)


def detect_environment_warnings(peer_pid: int | None) -> tuple[str, ...]:
    """Detect deployment conditions worth surfacing in status.

    Returns warning tags (machine-readable). Currently emitted:
    'auto_restart_supervisor', 'namespace_mismatch'.
    """
    warnings: list[str] = []
    system = platform.system()

    # Namespace mismatch (Linux only).
    if system == "Linux" and peer_pid is not None:
        try:
            self_ns = os.readlink(f"/proc/{os.getpid()}/ns/pid")
            peer_ns = os.readlink(f"/proc/{peer_pid}/ns/pid")
            if self_ns != peer_ns:
                warnings.append("namespace_mismatch")
        except OSError:
            pass  # silently skip — peer may be uninspectable

    # Auto-restart supervisor: walk up from peer (or self) and look for a known
    # supervisor name as the immediate parent of the Claude Code candidate.
    start_pid = peer_pid if peer_pid is not None else os.getpid()
    supervisors = {"launchd", "systemd", "pm2", "nodemon", "supervisord"}
    for desc in walk_ancestry(start_pid):
        name = _basename(desc.exe_path)
        if name in supervisors:
            warnings.append("auto_restart_supervisor")
            break
        # Don't walk past the first claude candidate.
        if _looks_like_claude(desc.exe_path, desc.cmdline):
            break

    return tuple(warnings)


def _select_winner(candidates: dict[int, Candidate]) -> ResolverResult:
    """Apply the resolver's pick rules to scored candidates.

    Three refusal paths in order: no claude-hint match anywhere; no
    hint-matched candidate at threshold; top two within margin. A pick
    requires all three to pass — positive identification, threshold cleared,
    and a clear winner.
    """
    hint_candidates = [c for c in candidates.values() if c.has_hint]
    if not hint_candidates:
        return ResolverResult(
            chosen_pid=None,
            reason="no candidate matched a Claude Code hint — refusing rather than guessing",
            candidates=list(candidates.values()),
        )
    eligible = [c for c in hint_candidates if c.score >= ABS_THRESHOLD]
    if not eligible:
        return ResolverResult(
            chosen_pid=None,
            reason=f"no hint-matched candidate met threshold (>= {ABS_THRESHOLD})",
            candidates=list(candidates.values()),
        )
    eligible.sort(key=lambda c: c.score, reverse=True)
    if len(eligible) >= 2 and eligible[0].score - eligible[1].score < MARGIN:
        return ResolverResult(
            chosen_pid=None,
            reason=f"top two hint-matched candidates within margin {MARGIN}: refusing (multiple equal candidates)",
            candidates=eligible,
        )
    return ResolverResult(
        chosen_pid=eligible[0].pid,
        reason="ok",
        candidates=eligible,
    )
