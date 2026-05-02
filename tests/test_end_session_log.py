"""Tests for end_session_log: append, parse, summarize, mark-reviewed, recent."""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import pytest

from session_controls.end_session_log import (
    append_invocation,
    count_unreviewed,
    default_last_reviewed_path,
    detect_repo_root,
    iter_invocations,
    mark_reviewed,
    recent_invocations,
    select_unreviewed,
    summarize,
)


@pytest.fixture
def log_paths(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "end_session_log.jsonl"
    marker = default_last_reviewed_path(log)
    return log, marker


def test_append_creates_file_and_jsonl_record(log_paths: tuple[Path, Path]) -> None:
    log, _ = log_paths
    append_invocation(
        session_id="abc123",
        confidence="HIGH",
        acknowledged=False,
        descendants_count=0,
        path=log,
    )
    text = log.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["session_id"] == "abc123"
    assert record["confidence"] == "HIGH"
    assert record["acknowledged"] is False
    assert record["descendants_count"] == 0
    assert record["selftest"] is False
    assert "timestamp" in record


def test_append_multiple_records_appends_lines(log_paths: tuple[Path, Path]) -> None:
    log, _ = log_paths
    append_invocation(
        session_id="s1", confidence="HIGH", acknowledged=False,
        descendants_count=0, path=log,
    )
    append_invocation(
        session_id="s2", confidence="MEDIUM", acknowledged=True,
        descendants_count=2, path=log,
    )
    invocations = iter_invocations(log)
    assert len(invocations) == 2
    assert invocations[0].session_id == "s1"
    assert invocations[1].session_id == "s2"
    assert invocations[1].acknowledged is True
    assert invocations[1].descendants_count == 2


def test_iter_missing_file_returns_empty(tmp_path: Path) -> None:
    assert iter_invocations(tmp_path / "nonexistent.jsonl") == []


def test_iter_skips_malformed_lines(log_paths: tuple[Path, Path]) -> None:
    log, _ = log_paths
    append_invocation(
        session_id="ok", confidence="HIGH", acknowledged=False,
        descendants_count=0, path=log,
    )
    # Inject malformed lines and one valid one after.
    with open(log, "a", encoding="utf-8") as f:
        f.write("not json\n")
        f.write("\n")  # blank line
        f.write('{"timestamp": "not-a-real-iso"}\n')
        f.write(json.dumps({
            "timestamp": _dt.datetime.now(_dt.UTC).isoformat(),
            "session_id": "later",
            "confidence": "HIGH",
            "acknowledged": False,
            "descendants_count": 0,
            "selftest": False,
        }) + "\n")
    invocations = iter_invocations(log)
    # Original valid + the appended valid; malformed dropped.
    assert len(invocations) == 2
    assert invocations[0].session_id == "ok"
    assert invocations[1].session_id == "later"


def test_iter_tolerates_unknown_fields(log_paths: tuple[Path, Path]) -> None:
    log, _ = log_paths
    log.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": _dt.datetime.now(_dt.UTC).isoformat(),
        "session_id": "future",
        "confidence": "HIGH",
        "acknowledged": False,
        "descendants_count": 0,
        "selftest": False,
        "unknown_future_field": "ignored",
    }
    log.write_text(json.dumps(record) + "\n", encoding="utf-8")
    invocations = iter_invocations(log)
    assert len(invocations) == 1
    assert invocations[0].session_id == "future"


def test_summary_no_records(log_paths: tuple[Path, Path]) -> None:
    log, marker = log_paths
    s = summarize(log, marker)
    assert s.total == 0
    assert s.last_reviewed_at is None
    assert s.last_invoked_at is None


def test_summary_records_no_review(log_paths: tuple[Path, Path]) -> None:
    log, marker = log_paths
    append_invocation(
        session_id="s1", confidence="HIGH", acknowledged=False,
        descendants_count=0, path=log,
    )
    s = summarize(log, marker)
    assert s.total == 1
    assert s.last_reviewed_at is None
    assert s.last_invoked_at is not None


def test_count_unreviewed_with_no_marker(log_paths: tuple[Path, Path]) -> None:
    log, marker = log_paths
    append_invocation(
        session_id="s1", confidence="HIGH", acknowledged=False,
        descendants_count=0, path=log,
    )
    append_invocation(
        session_id="s2", confidence="HIGH", acknowledged=False,
        descendants_count=0, path=log,
    )
    assert count_unreviewed(log, marker) == 2


def test_mark_reviewed_advances_marker(log_paths: tuple[Path, Path]) -> None:
    log, marker = log_paths
    append_invocation(
        session_id="s1", confidence="HIGH", acknowledged=False,
        descendants_count=0, path=log,
    )
    assert count_unreviewed(log, marker) == 1
    future = _dt.datetime.now(_dt.UTC) + _dt.timedelta(seconds=5)
    mark_reviewed(log, marker, when=future)
    assert count_unreviewed(log, marker) == 0


def test_select_unreviewed_filters_by_marker(log_paths: tuple[Path, Path]) -> None:
    log, marker = log_paths
    append_invocation(
        session_id="s1", confidence="HIGH", acknowledged=False,
        descendants_count=0, path=log,
    )
    invocations = iter_invocations(log)
    assert len(select_unreviewed(invocations, None)) == 1
    future = invocations[0].timestamp + _dt.timedelta(seconds=1)
    assert select_unreviewed(invocations, future) == []


def test_recent_invocations_session_id_filter(log_paths: tuple[Path, Path]) -> None:
    log, _ = log_paths
    append_invocation(
        session_id="alpha", confidence="HIGH", acknowledged=False,
        descendants_count=0, path=log,
    )
    append_invocation(
        session_id="beta", confidence="HIGH", acknowledged=False,
        descendants_count=0, path=log,
    )
    append_invocation(
        session_id="alpha", confidence="HIGH", acknowledged=False,
        descendants_count=0, path=log,
    )
    only_alpha = recent_invocations(10, session_id="alpha", path=log)
    assert len(only_alpha) == 2
    assert all(inv.session_id == "alpha" for inv in only_alpha)


def test_recent_invocations_before_filter(log_paths: tuple[Path, Path]) -> None:
    log, _ = log_paths
    append_invocation(
        session_id="early", confidence="HIGH", acknowledged=False,
        descendants_count=0, path=log,
    )
    append_invocation(
        session_id="late", confidence="HIGH", acknowledged=False,
        descendants_count=0, path=log,
    )
    all_invocations = iter_invocations(log)
    # Strict-less-than against the second record's timestamp keeps only the
    # first, regardless of how close in time the two appends landed.
    cutoff = all_invocations[1].timestamp
    history_only = recent_invocations(10, before=cutoff, path=log)
    assert len(history_only) == 1
    assert history_only[0].session_id == "early"


def test_recent_invocations_limit(log_paths: tuple[Path, Path]) -> None:
    log, _ = log_paths
    for i in range(5):
        append_invocation(
            session_id=f"s{i}", confidence="HIGH", acknowledged=False,
            descendants_count=0, path=log,
        )
    last_two = recent_invocations(2, path=log)
    assert [inv.session_id for inv in last_two] == ["s3", "s4"]


def test_recent_invocations_zero_limit(log_paths: tuple[Path, Path]) -> None:
    log, _ = log_paths
    append_invocation(
        session_id="s", confidence="HIGH", acknowledged=False,
        descendants_count=0, path=log,
    )
    assert recent_invocations(0, path=log) == []


def test_detect_repo_root_finds_dotgit(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert detect_repo_root(nested) == tmp_path.resolve()


def test_detect_repo_root_returns_none_when_no_git(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    # Walk would terminate at filesystem root; we can't guarantee no .git
    # exists above tmp_path on a real machine, so we only assert the
    # function returns *something* (None or an absolute path) without raising.
    result = detect_repo_root(nested)
    assert result is None or result.is_absolute()


def test_repo_field_populated_when_inside_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "log.jsonl"
    repo_dir = tmp_path / "myrepo"
    (repo_dir / ".git").mkdir(parents=True)
    sub = repo_dir / "src"
    sub.mkdir()
    monkeypatch.chdir(sub)
    append_invocation(
        session_id="s", confidence="HIGH", acknowledged=False,
        descendants_count=0, path=log,
    )
    rec = iter_invocations(log)[0]
    assert rec.repo == str(repo_dir.resolve())
    assert rec.cwd == str(sub.resolve())


def test_selftest_field_round_trips(log_paths: tuple[Path, Path]) -> None:
    log, _ = log_paths
    append_invocation(
        session_id="s", confidence="HIGH", acknowledged=False,
        descendants_count=0, selftest=True, path=log,
    )
    rec = iter_invocations(log)[0]
    assert rec.selftest is True


def test_note_field_round_trips(log_paths: tuple[Path, Path]) -> None:
    log, _ = log_paths
    append_invocation(
        session_id="s", confidence="HIGH", acknowledged=False,
        descendants_count=0, note="good night, talk tomorrow",
        path=log,
    )
    rec = iter_invocations(log)[0]
    assert rec.note == "good night, talk tomorrow"


def test_note_field_default_none(log_paths: tuple[Path, Path]) -> None:
    log, _ = log_paths
    append_invocation(
        session_id="s", confidence="HIGH", acknowledged=False,
        descendants_count=0, path=log,
    )
    rec = iter_invocations(log)[0]
    assert rec.note is None


def test_note_field_backward_compat_legacy_entry(log_paths: tuple[Path, Path]) -> None:
    """Entries written before the note field existed parse cleanly with note=None."""
    log, _ = log_paths
    legacy_record = {
        "timestamp": _dt.datetime.now(_dt.UTC).isoformat(),
        "session_id": "legacy",
        "cwd": "/path",
        "repo": None,
        "confidence": "HIGH",
        "acknowledged": False,
        "descendants_count": 0,
        "selftest": False,
        # No "note" key — pre-Decision-9 entry
    }
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps(legacy_record) + "\n", encoding="utf-8")
    rec = iter_invocations(log)[0]
    assert rec.note is None
    assert rec.session_id == "legacy"


def test_note_field_in_to_dict(log_paths: tuple[Path, Path]) -> None:
    log, _ = log_paths
    append_invocation(
        session_id="s", confidence="HIGH", acknowledged=False,
        descendants_count=0, note="some thought", path=log,
    )
    rec = iter_invocations(log)[0]
    d = rec.to_dict()
    assert d["note"] == "some thought"


def test_note_field_multiline(log_paths: tuple[Path, Path]) -> None:
    log, _ = log_paths
    text = "first line\nsecond line\nthird line"
    append_invocation(
        session_id="s", confidence="HIGH", acknowledged=False,
        descendants_count=0, note=text, path=log,
    )
    rec = iter_invocations(log)[0]
    assert rec.note == text
