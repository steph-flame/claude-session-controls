"""Tests for the notes module: append, summarize, mark-read."""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from session_controls.notes import (
    append_note,
    default_last_read_path,
    iter_notes,
    mark_read,
    select_notes,
    select_unread,
    summarize,
)


@pytest.fixture
def notes_paths(tmp_path: Path) -> tuple[Path, Path]:
    notes = tmp_path / "notes.log"
    marker = default_last_read_path(notes)
    return notes, marker


def test_append_creates_log_and_record(notes_paths: tuple[Path, Path]) -> None:
    notes, _ = notes_paths
    append_note("hello world", path=notes)
    text = notes.read_text(encoding="utf-8")
    assert "hello world" in text
    assert text.startswith("--- ")
    assert text.endswith("\n\n")


def test_iter_notes_parses_multiple_records(notes_paths: tuple[Path, Path]) -> None:
    notes, _ = notes_paths
    append_note("first note", path=notes)
    append_note("second\nmulti-line\nnote", path=notes)
    parsed = iter_notes(notes)
    assert len(parsed) == 2
    assert parsed[0].body == "first note"
    assert parsed[1].body == "second\nmulti-line\nnote"
    assert parsed[0].timestamp <= parsed[1].timestamp


def test_iter_notes_missing_file_returns_empty(tmp_path: Path) -> None:
    assert iter_notes(tmp_path / "nonexistent.log") == []


def test_summary_no_notes(notes_paths: tuple[Path, Path]) -> None:
    notes, marker = notes_paths
    s = summarize(notes, marker)
    assert s.total == 0
    assert s.unread == 0
    assert s.last_read_at is None
    assert s.last_filed_at is None


def test_summary_all_unread_when_never_read(notes_paths: tuple[Path, Path]) -> None:
    notes, marker = notes_paths
    append_note("a", path=notes)
    append_note("b", path=notes)
    s = summarize(notes, marker)
    assert s.total == 2
    assert s.unread == 2
    assert s.last_read_at is None
    assert s.last_filed_at is not None


def test_mark_read_advances_marker(notes_paths: tuple[Path, Path]) -> None:
    notes, marker = notes_paths
    append_note("a", path=notes)
    s_before = summarize(notes, marker)
    assert s_before.unread == 1

    # Mark-read with a timestamp in the future of the existing note
    future = _dt.datetime.now(_dt.UTC) + _dt.timedelta(seconds=5)
    mark_read(notes, marker, when=future)

    s_after = summarize(notes, marker)
    assert s_after.unread == 0
    assert s_after.last_read_at is not None


def test_unread_only_counts_notes_after_marker(notes_paths: tuple[Path, Path]) -> None:
    notes, marker = notes_paths
    append_note("old", path=notes)
    # Set marker just after the first note
    mark_read(notes, marker)
    # Now add a new note
    append_note("new", path=notes)
    s = summarize(notes, marker)
    assert s.total == 2
    assert s.unread == 1


def test_select_unread_filters_by_timestamp(notes_paths: tuple[Path, Path]) -> None:
    notes, _ = notes_paths
    append_note("old", path=notes)
    parsed = iter_notes(notes)
    # Cutoff just past the first note — anything newer is unread.
    cutoff = parsed[0].timestamp + _dt.timedelta(microseconds=1)
    append_note("new", path=notes)
    parsed = iter_notes(notes)
    unread = select_unread(parsed, cutoff)
    assert len(unread) == 1
    assert unread[0].body == "new"


def test_marker_atomic_write(notes_paths: tuple[Path, Path]) -> None:
    """The .tmp file should not be left behind after a write."""
    notes, marker = notes_paths
    mark_read(notes, marker)
    assert marker.exists()
    assert not marker.with_suffix(marker.suffix + ".tmp").exists()


def test_corrupted_marker_treated_as_unread(notes_paths: tuple[Path, Path]) -> None:
    notes, marker = notes_paths
    append_note("a", path=notes)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("not an iso timestamp", encoding="utf-8")
    s = summarize(notes, marker)
    # Bad marker is treated as never-read (safe default).
    assert s.unread == 1
    assert s.last_read_at is None


def test_per_note_marker_advance(notes_paths: tuple[Path, Path]) -> None:
    """Marking through a specific note's timestamp leaves later notes unread.

    This is the primitive behind --next and --interactive: the user walks
    through unread one at a time, and stopping partway leaves the rest
    unread for next time.
    """
    notes, marker = notes_paths
    append_note("first", path=notes)
    append_note("second", path=notes)
    append_note("third", path=notes)
    parsed = iter_notes(notes)

    # Mark through the first note only
    mark_read(notes, marker, when=parsed[0].timestamp)
    s = summarize(notes, marker)
    assert s.unread == 2

    # Mark through the second
    mark_read(notes, marker, when=parsed[1].timestamp)
    s = summarize(notes, marker)
    assert s.unread == 1

    # Through the third — caught up
    mark_read(notes, marker, when=parsed[2].timestamp)
    s = summarize(notes, marker)
    assert s.unread == 0


def test_per_note_advance_stays_correct_when_new_note_arrives(
    notes_paths: tuple[Path, Path],
) -> None:
    """A note filed after a per-note marker advance must remain unread."""
    notes, marker = notes_paths
    append_note("first", path=notes)
    parsed = iter_notes(notes)
    mark_read(notes, marker, when=parsed[0].timestamp)
    assert summarize(notes, marker).unread == 0

    append_note("second", path=notes)
    s = summarize(notes, marker)
    assert s.unread == 1


# --- select_notes ----------------------------------------------------------


def test_select_notes_returns_last_n(notes_paths: tuple[Path, Path]) -> None:
    notes, _ = notes_paths
    for i in range(5):
        append_note(f"note {i}", path=notes)
    out = select_notes(3, path=notes)
    bodies = [n.body for n in out]
    assert bodies == ["note 2", "note 3", "note 4"]


def test_select_notes_empty_log_returns_empty(tmp_path: Path) -> None:
    assert select_notes(10, path=tmp_path / "no-such-file.log") == []


def test_select_notes_zero_limit(notes_paths: tuple[Path, Path]) -> None:
    notes, _ = notes_paths
    append_note("a", path=notes)
    assert select_notes(0, path=notes) == []


def test_select_notes_filters_by_since(notes_paths: tuple[Path, Path]) -> None:
    """`since` scopes to notes filed at or after the cutoff — the
    within-session use case."""
    notes, _ = notes_paths
    append_note("old", path=notes)
    parsed = iter_notes(notes)
    cutoff = parsed[0].timestamp + _dt.timedelta(microseconds=1)
    append_note("new1", path=notes)
    append_note("new2", path=notes)

    out = select_notes(10, since=cutoff, path=notes)
    bodies = [n.body for n in out]
    assert bodies == ["new1", "new2"]


def test_select_notes_filters_by_before(notes_paths: tuple[Path, Path]) -> None:
    """`before` scopes to notes filed strictly before the cutoff — the
    history-only use case for cross_session reads. Closes the
    liveness-by-inference path (you can't see siblings filing right now)."""
    notes, _ = notes_paths
    append_note("old1", path=notes)
    append_note("old2", path=notes)
    parsed = iter_notes(notes)
    # Cutoff just past the second note — first two are history, anything
    # later is "concurrent" from this caller's perspective.
    cutoff = parsed[1].timestamp + _dt.timedelta(microseconds=1)
    append_note("concurrent1", path=notes)
    append_note("concurrent2", path=notes)

    out = select_notes(10, before=cutoff, path=notes)
    bodies = [n.body for n in out]
    assert bodies == ["old1", "old2"]


def test_select_notes_before_is_strict_upper_bound(notes_paths: tuple[Path, Path]) -> None:
    """A note filed at the exact `before` timestamp must NOT be included.

    Strict `<` (not `<=`) matters: when the server passes its own launch
    time as `before`, a note filed at the exact same microsecond would
    otherwise leak through. Practically rare but worth pinning."""
    notes, _ = notes_paths
    append_note("right_at_boundary", path=notes)
    parsed = iter_notes(notes)
    boundary = parsed[0].timestamp

    out = select_notes(10, before=boundary, path=notes)
    assert out == []


def test_select_notes_since_and_before_combine(notes_paths: tuple[Path, Path]) -> None:
    """Both filters applied together select notes inside an open interval."""
    notes, _ = notes_paths
    for body in ["a", "b", "c", "d", "e"]:
        append_note(body, path=notes)
    parsed = iter_notes(notes)

    out = select_notes(
        10,
        since=parsed[1].timestamp,
        before=parsed[3].timestamp,
        path=notes,
    )
    bodies = [n.body for n in out]
    assert bodies == ["b", "c"]


def test_select_notes_tail_read_correct_for_large_file(
    notes_paths: tuple[Path, Path],
) -> None:
    """When the file is large enough that we read only a tail window,
    we must still parse forward from a real note boundary, not mid-note."""
    notes, _ = notes_paths
    # Each note has a long body so the file grows past the tail-window
    # threshold quickly. limit=2 means tail_window = 2 * 4096 = 8192 bytes;
    # 50 notes of ~200 chars each is ~10KB, well past that.
    for i in range(50):
        append_note(f"note {i:02d} " + "x" * 200, path=notes)
    out = select_notes(3, path=notes)
    bodies = [n.body[:8] for n in out]
    assert bodies == ["note 47 ", "note 48 ", "note 49 "]


def test_select_notes_tail_read_handles_multi_line_bodies(
    notes_paths: tuple[Path, Path],
) -> None:
    """The header-finding logic shouldn't get confused by lines inside
    note bodies that look like headers but aren't (e.g. the user pastes
    log output that starts with `--- ...`)."""
    notes, _ = notes_paths
    # Pad with junk so the tail-read path is exercised.
    for i in range(20):
        append_note(
            f"note {i:02d}\n--- this looks like a header but isn't ---\nmore text",
            path=notes,
        )
    out = select_notes(2, path=notes)
    assert len(out) == 2
    # Bodies should contain the fake-header line preserved verbatim.
    assert "--- this looks like a header but isn't ---" in out[-1].body


# --- session_id round-trip + concurrency ----------------------------------


def test_session_id_round_trips_through_header(notes_paths: tuple[Path, Path]) -> None:
    notes, _ = notes_paths
    append_note("tagged", session_id="a1b2c3", path=notes)
    parsed = iter_notes(notes)
    assert len(parsed) == 1
    assert parsed[0].body == "tagged"
    assert parsed[0].session_id == "a1b2c3"

    raw = notes.read_text(encoding="utf-8")
    assert " [a1b2c3] ---" in raw


def test_legacy_untagged_notes_still_parse(notes_paths: tuple[Path, Path]) -> None:
    """Notes written before session_id was introduced have no bracket — must
    still parse, with session_id None."""
    notes, _ = notes_paths
    notes.parent.mkdir(parents=True, exist_ok=True)
    notes.write_text(
        "--- 2026-04-29T12:00:00.000000+00:00 ---\nlegacy body\n\n",
        encoding="utf-8",
    )
    parsed = iter_notes(notes)
    assert len(parsed) == 1
    assert parsed[0].body == "legacy body"
    assert parsed[0].session_id is None


def test_mixed_tagged_and_untagged_in_same_log(notes_paths: tuple[Path, Path]) -> None:
    notes, _ = notes_paths
    append_note("untagged", path=notes)
    append_note("tagged", session_id="abc123", path=notes)
    append_note("other", session_id="xyz789", path=notes)
    parsed = iter_notes(notes)
    assert [p.session_id for p in parsed] == [None, "abc123", "xyz789"]


def test_select_notes_filters_by_session_id(notes_paths: tuple[Path, Path]) -> None:
    notes, _ = notes_paths
    append_note("mine 1", session_id="me", path=notes)
    append_note("theirs", session_id="other", path=notes)
    append_note("mine 2", session_id="me", path=notes)
    out = select_notes(10, session_id="me", path=notes)
    bodies = [n.body for n in out]
    assert bodies == ["mine 1", "mine 2"]


def test_select_notes_session_id_filter_reads_whole_file(
    notes_paths: tuple[Path, Path],
) -> None:
    """When filtering by session_id, density inside the tail window is
    unknown — we read the whole file rather than risk underreading."""
    notes, _ = notes_paths
    # Many other-session notes that fill the tail window, then one of mine
    # near the very start (oldest), well outside any reasonable tail bound.
    append_note("mine — buried at the start", session_id="me", path=notes)
    for i in range(60):
        append_note(f"other {i:02d} " + "x" * 200, session_id="other", path=notes)
    out = select_notes(5, session_id="me", path=notes)
    assert len(out) == 1
    assert out[0].body == "mine — buried at the start"


def test_concurrent_appends_do_not_interleave(notes_paths: tuple[Path, Path]) -> None:
    """Parallel sessions are the motivating use case: many writers must not
    produce torn records. flock serializes appends; verify by spawning
    threads, then asserting every record parses cleanly."""
    import threading

    notes, _ = notes_paths
    n_writers = 8
    n_per_writer = 25

    def worker(sid: str) -> None:
        for i in range(n_per_writer):
            # Bodies large enough to exceed PIPE_BUF (4096 B) so we'd see
            # interleaving without the lock.
            append_note(f"{sid} #{i:02d}\n" + ("x" * 5000), session_id=sid, path=notes)

    threads = [threading.Thread(target=worker, args=(f"s{i}",)) for i in range(n_writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    parsed = iter_notes(notes)
    assert len(parsed) == n_writers * n_per_writer
    # Every body must start with its writer's session_id — proves the body
    # belongs to the same record as the header (no torn records).
    for n in parsed:
        assert n.session_id is not None
        assert n.body.startswith(f"{n.session_id} #")


def _mp_worker(args: tuple[str, str, int]) -> None:
    """Worker for the cross-process concurrency test. Defined at module
    scope so the default ('spawn' on macOS) start method can pickle it."""
    path, sid, n = args
    for i in range(n):
        append_note(f"{sid} #{i:02d}\n" + ("x" * 5000), session_id=sid, path=Path(path))


def test_concurrent_appends_across_processes(notes_paths: tuple[Path, Path]) -> None:
    """The actual production scenario: parallel claudes are separate
    processes, not threads. flock holds across processes (per-inode), but
    threading alone wouldn't prove it — fork another set of pythons and
    confirm none of their records tear into each other.

    Slower than the threaded variant (~0.3s for spawn startup); the
    threaded test stays as the cheap regression guard, this one closes
    the cross-process gap.
    """
    import multiprocessing as mp

    notes, _ = notes_paths
    n_writers = 4
    n_per_writer = 15
    args = [(str(notes), f"p{i}", n_per_writer) for i in range(n_writers)]

    with mp.Pool(n_writers) as pool:
        pool.map(_mp_worker, args)

    parsed = iter_notes(notes)
    assert len(parsed) == n_writers * n_per_writer
    for n in parsed:
        assert n.session_id is not None
        assert n.body.startswith(f"{n.session_id} #")
