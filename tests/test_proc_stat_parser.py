"""Tests for the /proc/<pid>/stat parser.

The parser is the documented trap: the `comm` field is wrapped in parens but
may itself contain parens or whitespace, so a naive split breaks. The parser
must scan to the *last* ')' rather than the first.
"""

from __future__ import annotations

import os
import platform

import pytest

from session_controls.process_inspect import inspect, parse_proc_stat


def test_simple_comm() -> None:
    raw = "1234 (bash) S 1 1 1 0 -1 4194304 100 0 0 0 0 0 0 0 20 0 1 0 5000"
    comm, after = parse_proc_stat(raw)
    assert comm == "bash"
    # field 3 (state) lives at index 0
    assert after[0] == "S"
    # field 22 (starttime) is at index 19
    assert after[19] == "5000"


def test_comm_with_parens() -> None:
    raw = "42 (weird(proc)) S 1 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 9999"
    comm, after = parse_proc_stat(raw)
    assert comm == "weird(proc)"
    assert after[0] == "S"
    assert after[19] == "9999"


def test_comm_with_spaces() -> None:
    raw = "7 (my proc) R 1 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 1"
    comm, after = parse_proc_stat(raw)
    assert comm == "my proc"
    assert after[0] == "R"


def test_comm_with_parens_and_spaces() -> None:
    raw = "9 (a (b) c) S 1 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 1"
    comm, after = parse_proc_stat(raw)
    assert comm == "a (b) c"
    assert after[0] == "S"


@pytest.mark.skipif(platform.system() != "Linux", reason="Linux-only /proc test")
def test_inspect_self_linux() -> None:
    desc = inspect(os.getpid())
    assert desc.pid == os.getpid()
    assert desc.start_time is not None
    assert desc.exe_path is not None
    assert desc.cmdline is not None
    assert not desc.inspection_errors
