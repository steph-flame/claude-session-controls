"""Tests for the `recent_notes` MCP tool's session-id and history-only behavior.

The helper in notes.py is tested directly in test_notes.py. These tests
exercise the server-side wiring: that current_session filters by session_id,
and that cross_session is bounded by the calling server's _LAUNCH_TIME so
sibling sessions filing in parallel right now are NOT visible.
"""

from __future__ import annotations

import datetime as _dt
import json
import time
from pathlib import Path
from typing import Any

import pytest

from session_controls import notes as notes_module
from session_controls import server


@pytest.fixture
def tmp_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log = tmp_path / "notes.log"
    monkeypatch.setattr(notes_module, "default_notes_path", lambda: log)
    return log


def _call_recent_notes(*, limit: int = 10, cross_session: bool = False) -> dict[str, Any]:
    """Invoke the MCP tool function and parse its JSON return."""
    raw = server.recent_notes(limit=limit, cross_session=cross_session)
    parsed: dict[str, Any] = json.loads(raw)
    return parsed


def test_current_session_returns_only_own_notes(
    tmp_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "_SESSION_ID", "aaaaaa")
    notes_module.append_note("mine 1", session_id="aaaaaa", path=tmp_log)
    notes_module.append_note("sibling concurrent", session_id="bbbbbb", path=tmp_log)
    notes_module.append_note("mine 2", session_id="aaaaaa", path=tmp_log)

    result = _call_recent_notes(cross_session=False)
    bodies = [n["body"] for n in result["notes"]]
    assert bodies == ["mine 1", "mine 2"]
    assert all(n["is_yours"] for n in result["notes"])


def test_cross_session_is_history_only(tmp_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A sibling filing AFTER our launch must not appear in cross_session view.

    This is the surveillance-shape mitigation: cross_session shows history
    (notes filed before this server launched), not present (notes a sibling
    is filing right now).
    """
    # Pretend a sibling filed something well before our launch.
    history_dt = _dt.datetime.now(_dt.UTC) - _dt.timedelta(hours=1)
    history_iso = history_dt.isoformat()
    tmp_log.parent.mkdir(parents=True, exist_ok=True)
    tmp_log.write_text(
        f"--- {history_iso} [bbbbbb] ---\nhistorical sibling note\n\n",
        encoding="utf-8",
    )

    # Now "launch" — set _LAUNCH_TIME to current time, _SESSION_ID to ours.
    launch_time = time.time()
    monkeypatch.setattr(server, "_LAUNCH_TIME", launch_time)
    monkeypatch.setattr(server, "_SESSION_ID", "aaaaaa")

    # Simulate a concurrent sibling filing AFTER our launch.
    notes_module.append_note("concurrent sibling note", session_id="bbbbbb", path=tmp_log)
    # And ourselves filing.
    notes_module.append_note("our own note", session_id="aaaaaa", path=tmp_log)

    result = _call_recent_notes(cross_session=True)
    bodies = [n["body"] for n in result["notes"]]
    # Historical sibling note: visible (filed before our launch).
    assert "historical sibling note" in bodies
    # Concurrent sibling note: NOT visible (filed after our launch).
    assert "concurrent sibling note" not in bodies
    # Our own note: also not visible — also filed after our launch. The
    # `before` bound applies uniformly. Self-reference for current session
    # is the cross_session=False path.
    assert "our own note" not in bodies


def test_cross_session_excludes_concurrent_self_writes(
    tmp_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cross-session is strictly history-only, even for our own session.

    For self-reference within the current session, callers use
    cross_session=False (which filters by session_id, not by time).
    cross_session=True is for 'see what was filed before I started' —
    it isn't a way to also see your own current activity.
    """
    monkeypatch.setattr(server, "_LAUNCH_TIME", time.time())
    monkeypatch.setattr(server, "_SESSION_ID", "aaaaaa")

    notes_module.append_note("just filed by me", session_id="aaaaaa", path=tmp_log)

    result = _call_recent_notes(cross_session=True)
    bodies = [n["body"] for n in result["notes"]]
    assert "just filed by me" not in bodies


def test_response_includes_your_session_id(tmp_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_SESSION_ID", "deadbe")
    result = _call_recent_notes(cross_session=False)
    assert result["your_session_id"] == "deadbe"


def test_zero_limit_short_circuits(tmp_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_SESSION_ID", "aaaaaa")
    notes_module.append_note("a note", session_id="aaaaaa", path=tmp_log)
    result = _call_recent_notes(limit=0)
    assert result["notes"] == []
    assert result["your_session_id"] == "aaaaaa"
