"""Tests for managed-environment detection during install.

Covers the writability pre-flight check, post-write verify, and the
behavior of `cmd_install` when permissions can't reliably be set —
the install refuses to proceed silently to the worse-than-nothing
state where session-controls is registered as an MCP server but
end_session isn't auto-approved.
"""

from __future__ import annotations

import argparse
import json
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from session_controls import TOOL_NAMES, cli
from session_controls.cli import (
    _check_permissions_writability,
    _verify_permissions_persisted,
)


def _install_args(**overrides: object) -> argparse.Namespace:
    defaults: dict[str, object] = {
        "project": False,
        "user_scope": False,
        "dry_run": False,
        "with_hook": False,
        "with_claude_md": False,
        "name": None,
        "without_pivot": False,
        "rehearse": False,
        "allow_unapproved": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# --- _check_permissions_writability ----------------------------------------


def test_writability_normal_writable_file(tmp_path: Path) -> None:
    p = tmp_path / "settings.json"
    p.write_text("{}", encoding="utf-8")
    writable, reason = _check_permissions_writability(p)
    assert writable
    assert reason is None


def test_writability_nonexistent_file_writable_parent(tmp_path: Path) -> None:
    p = tmp_path / "settings.json"
    writable, reason = _check_permissions_writability(p)
    assert writable
    assert reason is None


def test_writability_readonly_file(tmp_path: Path) -> None:
    p = tmp_path / "settings.json"
    p.write_text("{}", encoding="utf-8")
    p.chmod(stat.S_IRUSR)
    try:
        writable, reason = _check_permissions_writability(p)
        assert not writable
        assert reason is not None
        assert "not writable" in reason
    finally:
        # Restore so pytest can clean up tmp_path.
        p.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_writability_readonly_parent_dir(tmp_path: Path) -> None:
    parent = tmp_path / "locked"
    parent.mkdir()
    p = parent / "settings.json"
    parent.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        writable, reason = _check_permissions_writability(p)
        assert not writable
        assert reason is not None
        assert "parent directory" in reason
    finally:
        parent.chmod(stat.S_IRWXU)


def test_writability_symlink_inside_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Symlink whose target is inside HOME shouldn't trigger the warning."""
    # Pretend HOME is tmp_path so the symlink target is "inside" it.
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    target = tmp_path / "real-settings.json"
    target.write_text("{}", encoding="utf-8")
    symlink = tmp_path / "settings.json"
    symlink.symlink_to(target)
    writable, reason = _check_permissions_writability(symlink)
    assert writable
    assert reason is None


def test_writability_symlink_outside_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Symlink to a target outside HOME triggers the managed-env warning."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))
    # Target outside the fake home dir.
    external = tmp_path / "etc-settings.json"
    external.write_text("{}", encoding="utf-8")
    symlink = fake_home / "settings.json"
    symlink.symlink_to(external)
    writable, reason = _check_permissions_writability(symlink)
    assert not writable
    assert reason is not None
    assert "symlink" in reason
    assert "outside" in reason


def test_writability_broken_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A symlink whose target doesn't exist doesn't crash; some shape of
    refusal/warn is acceptable. Resolution behaviour is platform-dependent
    (strict resolution raises, lenient returns the dangling target), so we
    just assert no crash and a non-None response."""
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    symlink = tmp_path / "settings.json"
    symlink.symlink_to(tmp_path / "nonexistent-target")
    # Either reports unwritable (strict resolve raised) or writable (lenient
    # resolve, target inside home). Both are acceptable; what matters is it
    # doesn't blow up.
    _check_permissions_writability(symlink)


# --- _verify_permissions_persisted -----------------------------------------


def test_verify_persisted_all_tools_present(tmp_path: Path) -> None:
    p = tmp_path / "settings.json"
    p.write_text(
        json.dumps({"permissions": {"allow": list(TOOL_NAMES)}}),
        encoding="utf-8",
    )
    assert _verify_permissions_persisted(p)


def test_verify_persisted_missing_file(tmp_path: Path) -> None:
    p = tmp_path / "settings.json"
    assert not _verify_permissions_persisted(p)


def test_verify_persisted_no_permissions_key(tmp_path: Path) -> None:
    p = tmp_path / "settings.json"
    p.write_text("{}", encoding="utf-8")
    assert not _verify_permissions_persisted(p)


def test_verify_persisted_partial_tools(tmp_path: Path) -> None:
    p = tmp_path / "settings.json"
    p.write_text(
        json.dumps({"permissions": {"allow": [TOOL_NAMES[0]]}}),
        encoding="utf-8",
    )
    assert not _verify_permissions_persisted(p)


# --- cmd_install behavior under managed-env conditions ---------------------


@pytest.fixture
def unwritable_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Path]]:
    """Set up an install scenario where settings.json is read-only."""
    paths = {
        "settings": tmp_path / "settings.json",
        "mcp": tmp_path / "mcp.json",
        "claude_md": tmp_path / "CLAUDE.md",
    }
    paths["settings"].write_text("{}", encoding="utf-8")
    paths["settings"].chmod(stat.S_IRUSR)
    monkeypatch.setattr(cli, "_user_settings_path", lambda: paths["settings"])
    monkeypatch.setattr(cli, "_user_mcp_config_path", lambda: paths["mcp"])
    monkeypatch.setattr(cli, "_user_claude_md_path", lambda: paths["claude_md"])
    yield paths
    paths["settings"].chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_install_aborts_when_settings_unwritable_and_no_confirm(
    unwritable_settings: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without --allow-unapproved, non-interactive context aborts (EOFError)."""

    def _no_input(_prompt: str) -> str:
        raise EOFError()

    monkeypatch.setattr("builtins.input", _no_input)
    rc = cli.cmd_install(_install_args())
    assert rc == 1
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "Aborted" in out
    # Critical: MCP config was never written because we aborted before any writes.
    assert not unwritable_settings["mcp"].exists()


def test_install_proceeds_with_user_confirmation(
    unwritable_settings: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """User typing 'I understand' lets install proceed despite the warning."""
    monkeypatch.setattr("builtins.input", lambda _prompt: "I understand")
    rc = cli.cmd_install(_install_args())
    # Install proceeded. The actual permissions write will fail (file is
    # read-only) but cmd_install accepts that — the user accepted the
    # trade-off. We assert it didn't abort early.
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "Aborted" not in out
    # MCP config was written
    assert unwritable_settings["mcp"].exists()
    # rc may be 0 or non-zero depending on downstream OSError; what matters
    # is we didn't short-circuit at the pre-flight.
    assert rc in (0, 1)


def test_install_proceeds_with_allow_unapproved_flag(
    unwritable_settings: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--allow-unapproved bypasses the prompt entirely."""

    def _no_input(_prompt: str) -> str:
        raise AssertionError("input() should not be called with --allow-unapproved")

    monkeypatch.setattr("builtins.input", _no_input)
    cli.cmd_install(_install_args(allow_unapproved=True))
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "--allow-unapproved" in out
    # MCP config was written
    assert unwritable_settings["mcp"].exists()


def test_install_dry_run_warns_but_does_not_prompt(
    unwritable_settings: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Dry-run prints the warning but doesn't prompt and doesn't abort."""

    def _no_input(_prompt: str) -> str:
        raise AssertionError("input() should not be called in dry-run")

    monkeypatch.setattr("builtins.input", _no_input)
    rc = cli.cmd_install(_install_args(dry_run=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "dry-run" in out
    # No real writes
    assert not unwritable_settings["mcp"].exists()


def test_install_normal_writable_skips_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Normal writable case: no warning, no prompt."""
    paths = {
        "settings": tmp_path / "settings.json",
        "mcp": tmp_path / "mcp.json",
        "claude_md": tmp_path / "CLAUDE.md",
    }
    monkeypatch.setattr(cli, "_user_settings_path", lambda: paths["settings"])
    monkeypatch.setattr(cli, "_user_mcp_config_path", lambda: paths["mcp"])
    monkeypatch.setattr(cli, "_user_claude_md_path", lambda: paths["claude_md"])

    def _no_input(_prompt: str) -> str:
        raise AssertionError("input() should not be called when writable")

    monkeypatch.setattr("builtins.input", _no_input)
    rc = cli.cmd_install(_install_args())
    assert rc == 0
    out = capsys.readouterr().out
    assert "WARNING" not in out


def test_install_post_write_verify_warns_when_perms_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Simulate a write-then-revert: stub _add_permissions to no-op so the
    file ends up without our tools, then check that the post-write verify
    warning fires."""
    paths = {
        "settings": tmp_path / "settings.json",
        "mcp": tmp_path / "mcp.json",
        "claude_md": tmp_path / "CLAUDE.md",
    }
    monkeypatch.setattr(cli, "_user_settings_path", lambda: paths["settings"])
    monkeypatch.setattr(cli, "_user_mcp_config_path", lambda: paths["mcp"])
    monkeypatch.setattr(cli, "_user_claude_md_path", lambda: paths["claude_md"])
    # Stub _add_permissions to no-op (file ends up empty, simulating revert).
    monkeypatch.setattr(cli, "_add_permissions", lambda _config: [])

    cli.cmd_install(_install_args())
    out = capsys.readouterr().out
    assert "post-write verify failed" in out
