"""Tests for the verify state file (cli writes, server reads)."""

from __future__ import annotations

from pathlib import Path

from session_controls.verify_state import read_state, write_state


def test_read_state_missing_returns_none(tmp_path: Path) -> None:
    assert read_state(tmp_path / "no-such-file.json") is None


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "verify.json"
    payload = {
        "last_at": "2026-04-29T12:00:00+00:00",
        "success": True,
        "target_pid": 1234,
        "target_start_time": 999.5,
    }
    write_state(target, payload)
    loaded = read_state(target)
    assert loaded == payload


def test_write_is_atomic(tmp_path: Path) -> None:
    """The .tmp file should not be left behind after a successful write."""
    target = tmp_path / "verify.json"
    write_state(target, {"x": 1})
    assert target.exists()
    assert not target.with_suffix(target.suffix + ".tmp").exists()


def test_read_corrupted_returns_error_dict(tmp_path: Path) -> None:
    target = tmp_path / "verify.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("not valid json", encoding="utf-8")
    result = read_state(target)
    assert result is not None
    assert "error" in result
    assert "last_at" not in result
