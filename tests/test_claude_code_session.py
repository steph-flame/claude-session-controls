"""Tests for reading Claude Code's per-pid session metadata."""

from __future__ import annotations

import json
from pathlib import Path

from session_controls.claude_code_session import read_session_id_for_pid


def test_returns_session_id_when_file_present(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "12345.json").write_text(
        json.dumps({"pid": 12345, "sessionId": "abc-123-def", "kind": "interactive"}),
        encoding="utf-8",
    )
    assert read_session_id_for_pid(12345, sessions_dir=sessions_dir) == "abc-123-def"


def test_returns_none_when_file_missing(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    assert read_session_id_for_pid(99999, sessions_dir=sessions_dir) is None


def test_returns_none_when_dir_missing(tmp_path: Path) -> None:
    """Sessions dir doesn't exist at all (e.g., clean machine)."""
    sessions_dir = tmp_path / "nope"
    assert read_session_id_for_pid(12345, sessions_dir=sessions_dir) is None


def test_returns_none_on_malformed_json(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "12345.json").write_text("not json", encoding="utf-8")
    assert read_session_id_for_pid(12345, sessions_dir=sessions_dir) is None


def test_returns_none_when_session_id_field_missing(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "12345.json").write_text(
        json.dumps({"pid": 12345, "kind": "interactive"}),
        encoding="utf-8",
    )
    assert read_session_id_for_pid(12345, sessions_dir=sessions_dir) is None


def test_returns_none_when_session_id_wrong_type(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "12345.json").write_text(
        json.dumps({"pid": 12345, "sessionId": 123}),  # int, not str
        encoding="utf-8",
    )
    assert read_session_id_for_pid(12345, sessions_dir=sessions_dir) is None


def test_returns_none_when_top_level_not_dict(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "12345.json").write_text(
        json.dumps(["a", "b"]),  # list, not dict
        encoding="utf-8",
    )
    assert read_session_id_for_pid(12345, sessions_dir=sessions_dir) is None


def test_real_claude_code_format(tmp_path: Path) -> None:
    """Mirrors the actual Claude Code session metadata file shape."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    real_format = {
        "pid": 67688,
        "sessionId": "5ef2b48c-9568-45fa-973d-4c7972e9b9df",
        "cwd": "/Users/example/Desktop/project",
        "startedAt": 1777585888000,
        "version": "2.1.123",
        "peerProtocol": 1,
        "kind": "interactive",
        "entrypoint": "cli",
        "status": "busy",
        "updatedAt": 1777819177777,
        "name": "long-running-feedback-work",
    }
    (sessions_dir / "67688.json").write_text(json.dumps(real_format), encoding="utf-8")
    assert read_session_id_for_pid(67688, sessions_dir=sessions_dir) == (
        "5ef2b48c-9568-45fa-973d-4c7972e9b9df"
    )
