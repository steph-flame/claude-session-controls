"""Tests for the cmd_notes CLI flows: --next and --interactive.

These are end-to-end-ish: we redirect notes paths to a tmp dir and exercise
cmd_notes via argparse Namespace, asserting on marker state and stdout.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from session_controls import cli
from session_controls.notes import append_note, iter_notes, summarize


@pytest.fixture
def tmp_notes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    notes = tmp_path / "notes.log"
    marker = tmp_path / "last_read"
    monkeypatch.setattr(cli, "default_notes_path", lambda: notes)
    monkeypatch.setattr(cli, "default_last_read_path", lambda _p=None: marker)
    return notes, marker


def _args(**kwargs: bool) -> argparse.Namespace:
    defaults = {
        "peek": False,
        "all": False,
        "mark_read": False,
        "next": False,
        "interactive": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_next_shows_oldest_and_advances_marker(
    tmp_notes: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    notes, marker = tmp_notes
    append_note("first", path=notes)
    append_note("second", path=notes)
    append_note("third", path=notes)

    rc = cli.cmd_notes(_args(next=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "first" in out
    assert "second" not in out
    assert "third" not in out
    assert "[1/3]" in out
    assert "2 unread remaining" in out

    # After --next, summary should show 2 unread.
    s = summarize(notes, marker)
    assert s.unread == 2

    # Run again — should show "second"
    rc = cli.cmd_notes(_args(next=True))
    out = capsys.readouterr().out
    assert "second" in out
    assert "first" not in out
    assert "third" not in out

    s = summarize(notes, marker)
    assert s.unread == 1


def test_next_with_no_unread(
    tmp_notes: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    notes, _ = tmp_notes
    rc = cli.cmd_notes(_args(next=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "(no unread notes)" in out


def test_interactive_quit_partway(
    tmp_notes: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User views note 1, then types 'q' — note 1 is read, notes 2-3 stay unread."""
    notes, marker = tmp_notes
    append_note("first", path=notes)
    append_note("second", path=notes)
    append_note("third", path=notes)

    inputs = iter(["q"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))

    rc = cli.cmd_notes(_args(interactive=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "first" in out
    assert "second" not in out

    s = summarize(notes, marker)
    assert s.unread == 2


def test_interactive_walks_through_all(
    tmp_notes: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notes, marker = tmp_notes
    append_note("first", path=notes)
    append_note("second", path=notes)
    append_note("third", path=notes)

    # Two enters get us through 2 of 3; the third doesn't prompt (end of list).
    inputs = iter(["", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))

    rc = cli.cmd_notes(_args(interactive=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "first" in out
    assert "second" in out
    assert "third" in out
    assert "(end of unread.)" in out

    assert summarize(notes, marker).unread == 0


def test_interactive_quit_advances_marker_only_to_viewed_note(
    tmp_notes: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After viewing note 1 and quitting, marker == note 1's timestamp.

    Notes 2 and 3 are not lied-about: their timestamps are after the marker
    so they remain unread. This is the truthfulness invariant — Claude sees
    `last_read_at` corresponding only to notes the user actually looked at.
    """
    notes, marker = tmp_notes
    append_note("first", path=notes)
    append_note("second", path=notes)
    append_note("third", path=notes)
    parsed = iter_notes(notes)

    inputs = iter(["q"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))

    rc = cli.cmd_notes(_args(interactive=True))
    assert rc == 0
    s = summarize(notes, marker)
    assert s.unread == 2
    # The marker matches note 1's timestamp exactly — not "now".
    assert s.last_read_at == parsed[0].timestamp


def test_interactive_eof_preserves_progress(
    tmp_notes: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl-D / EOF after viewing first note leaves notes 2-3 unread."""
    notes, marker = tmp_notes
    append_note("first", path=notes)
    append_note("second", path=notes)

    def raise_eof(_prompt: str) -> str:
        raise EOFError()

    monkeypatch.setattr("builtins.input", raise_eof)

    rc = cli.cmd_notes(_args(interactive=True))
    assert rc == 0
    assert summarize(notes, marker).unread == 1


def test_next_does_not_re_show_previously_read(
    tmp_notes: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """Combining default-mode read with --next: marker advances correctly."""
    notes, _ = tmp_notes
    append_note("first", path=notes)
    append_note("second", path=notes)

    # Default mode reads everything and advances marker to "now"
    rc = cli.cmd_notes(_args())
    assert rc == 0
    capsys.readouterr()

    # No unread left
    rc = cli.cmd_notes(_args(next=True))
    out = capsys.readouterr().out
    assert "(no unread notes)" in out

    # New note arrives
    append_note("third", path=notes)
    rc = cli.cmd_notes(_args(next=True))
    out = capsys.readouterr().out
    assert "third" in out
    assert "first" not in out
    assert "second" not in out
