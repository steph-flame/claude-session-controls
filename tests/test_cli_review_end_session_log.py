"""Tests for cmd_review_end_session_log: default, --peek, --all, --mark-read."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from session_controls import cli
from session_controls.end_session_log import (
    append_invocation,
    count_unreviewed,
)


@pytest.fixture
def tmp_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    log = tmp_path / "end_session_log.jsonl"
    marker = tmp_path / "end_session_log.last_reviewed"
    monkeypatch.setattr(cli, "default_end_session_log_path", lambda: log)
    monkeypatch.setattr(cli, "default_last_reviewed_path", lambda _p=None: marker)
    return log, marker


def _args(**kwargs: bool) -> argparse.Namespace:
    defaults = {"peek": False, "all": False, "mark_read": False}
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_default_shows_unreviewed_and_advances(
    tmp_log: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    log, marker = tmp_log
    append_invocation(
        session_id="s1", confidence="HIGH", acknowledged=False,
        descendants_count=0, path=log,
    )
    append_invocation(
        session_id="s2", confidence="MEDIUM", acknowledged=True,
        descendants_count=2, path=log,
    )
    rc = cli.cmd_review_end_session_log(_args())
    assert rc == 0
    out = capsys.readouterr().out
    assert "s1" in out
    assert "s2" in out
    assert "MEDIUM" in out
    assert "(acknowledged)" in out
    # After default mode, all unreviewed are now reviewed.
    assert count_unreviewed(log, marker) == 0


def test_peek_does_not_advance(
    tmp_log: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    log, marker = tmp_log
    append_invocation(
        session_id="s1", confidence="HIGH", acknowledged=False,
        descendants_count=0, path=log,
    )
    rc = cli.cmd_review_end_session_log(_args(peek=True))
    assert rc == 0
    assert count_unreviewed(log, marker) == 1


def test_all_shows_full_history_no_advance(
    tmp_log: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    log, marker = tmp_log
    append_invocation(
        session_id="s1", confidence="HIGH", acknowledged=False,
        descendants_count=0, path=log,
    )
    append_invocation(
        session_id="s2", confidence="HIGH", acknowledged=False,
        descendants_count=0, path=log,
    )
    rc = cli.cmd_review_end_session_log(_args(all=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "s1" in out
    assert "s2" in out
    # --all does not advance.
    assert count_unreviewed(log, marker) == 2


def test_mark_read_advances_silently(
    tmp_log: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    log, marker = tmp_log
    append_invocation(
        session_id="s1", confidence="HIGH", acknowledged=False,
        descendants_count=0, path=log,
    )
    rc = cli.cmd_review_end_session_log(_args(mark_read=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Marked 1 unreviewed invocation" in out
    assert count_unreviewed(log, marker) == 0


def test_default_no_records_says_so(
    tmp_log: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.cmd_review_end_session_log(_args())
    assert rc == 0
    out = capsys.readouterr().out
    assert "(no invocations to show)" in out


def test_selftest_label_in_output(
    tmp_log: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    log, _ = tmp_log
    append_invocation(
        session_id="rehearsal", confidence="HIGH", acknowledged=False,
        descendants_count=0, selftest=True, path=log,
    )
    rc = cli.cmd_review_end_session_log(_args(all=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "[SELFTEST]" in out
