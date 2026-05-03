"""Tests for resumed-after-end_session detection.

Logic: at server launch we capture Claude Code's sessionId (a persistent
conversation-identity UUID). Each end_session call records that sessionId
in the invocation log. On any subsequent server launch (including after
`claude --resume`), `_check_resumed_after_end_session` reads the log and
checks for matching entries.

Three states: True (matching prior entry found), False (sessionId known,
no match), None (sessionId unavailable — best-effort, never false positive).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from session_controls import server
from session_controls.end_session_log import append_invocation


@pytest.fixture
def isolated_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log_path = tmp_path / "end_session_log.jsonl"
    monkeypatch.setattr(
        "session_controls.end_session_log.default_end_session_log_path",
        lambda: log_path,
    )
    return log_path


def test_returns_none_when_claude_code_session_id_unavailable(
    isolated_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If we couldn't read Claude Code's sessionId at launch, we report
    None — best-effort, never false positive."""
    monkeypatch.setattr(server, "_LAUNCH_CLAUDE_CODE_SESSION_ID", None)
    assert server._check_resumed_after_end_session() is None


def test_returns_false_when_sid_known_but_no_matching_entry(
    isolated_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "_LAUNCH_CLAUDE_CODE_SESSION_ID", "current-session-uuid")
    # Log has an entry, but with a different sessionId.
    append_invocation(
        session_id="some-server-launch",
        confidence="HIGH",
        descendants_count=0,
        claude_code_session_id="different-session-uuid",
        path=isolated_log,
    )
    assert server._check_resumed_after_end_session() is False


def test_returns_true_when_matching_prior_entry_exists(
    isolated_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The actual resume case: prior end_session in this same conversation
    (matching Claude Code sessionId), now we're back."""
    monkeypatch.setattr(server, "_LAUNCH_CLAUDE_CODE_SESSION_ID", "current-session-uuid")
    append_invocation(
        session_id="prior-server-launch",
        confidence="HIGH",
        descendants_count=0,
        claude_code_session_id="current-session-uuid",
        path=isolated_log,
    )
    assert server._check_resumed_after_end_session() is True


def test_returns_false_when_log_is_empty(
    isolated_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty log + known sessionId = definitely not resumed-after-end."""
    monkeypatch.setattr(server, "_LAUNCH_CLAUDE_CODE_SESSION_ID", "current-session-uuid")
    assert server._check_resumed_after_end_session() is False


def test_legacy_entries_dont_false_positive(
    isolated_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy entries (pre-Decision-13) have claude_code_session_id=None.
    They shouldn't match any current sessionId."""
    monkeypatch.setattr(server, "_LAUNCH_CLAUDE_CODE_SESSION_ID", "current-session-uuid")
    # Append a legacy-shaped entry (no claude_code_session_id passed).
    append_invocation(
        session_id="prior-server-launch",
        confidence="HIGH",
        descendants_count=0,
        path=isolated_log,
    )
    assert server._check_resumed_after_end_session() is False
