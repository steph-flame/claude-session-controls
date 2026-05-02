"""Tests for the uninstall command — config-file mutation logic and round-trip."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from session_controls import cli
from session_controls.cli import (
    _TOOLS,
    SERVER_NAME,
    _remove_claude_md,
    _remove_mcp_server,
    _remove_permissions,
    _remove_session_start_hook,
)


def _uninstall_args(**overrides: bool) -> argparse.Namespace:
    defaults = {"project": False, "user_scope": False, "dry_run": False, "purge_data": False}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _install_args(**overrides: object) -> argparse.Namespace:
    defaults: dict[str, object] = {
        "project": False, "user_scope": False, "dry_run": False,
        "with_hook": False, "with_claude_md": False, "name": None,
        "without_pivot": False, "rehearse": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# --- Helper unit tests ------------------------------------------------------


def test_remove_mcp_server_present() -> None:
    config: dict[str, Any] = {
        "mcpServers": {
            SERVER_NAME: {"command": "session-controls"},
            "other": {"command": "other"},
        }
    }
    changed = _remove_mcp_server(config)
    assert changed
    assert SERVER_NAME not in config["mcpServers"]
    assert "other" in config["mcpServers"]


def test_remove_mcp_server_absent() -> None:
    config: dict[str, Any] = {"mcpServers": {"other": {"command": "other"}}}
    assert not _remove_mcp_server(config)
    assert "other" in config["mcpServers"]


def test_remove_mcp_server_no_mcp_servers_key() -> None:
    config: dict[str, Any] = {}
    assert not _remove_mcp_server(config)


def test_remove_permissions_present() -> None:
    config: dict[str, Any] = {
        "permissions": {"allow": [_TOOLS[0], _TOOLS[1], "Bash(*:*)"]}
    }
    removed = _remove_permissions(config)
    assert set(removed) == {_TOOLS[0], _TOOLS[1]}
    assert config["permissions"]["allow"] == ["Bash(*:*)"]


def test_remove_permissions_absent() -> None:
    config: dict[str, Any] = {"permissions": {"allow": ["Bash(*:*)"]}}
    assert _remove_permissions(config) == []
    assert config["permissions"]["allow"] == ["Bash(*:*)"]


def test_remove_permissions_no_permissions_key() -> None:
    assert _remove_permissions({}) == []


def test_remove_session_start_hook_ours_only() -> None:
    settings: dict[str, Any] = {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "session-controls verify --quiet"}]},
            ]
        }
    }
    assert _remove_session_start_hook(settings)
    # Empty matchers → SessionStart key removed entirely
    assert "SessionStart" not in settings["hooks"]


def test_remove_session_start_hook_preserves_other_hooks() -> None:
    """When SessionStart contains both our hook and others, remove just ours."""
    settings: dict[str, Any] = {
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {"type": "command", "command": "session-controls verify --quiet"},
                        {"type": "command", "command": "echo hello"},
                    ]
                },
                {"hooks": [{"type": "command", "command": "other-tool startup"}]},
            ]
        }
    }
    assert _remove_session_start_hook(settings)
    surviving = settings["hooks"]["SessionStart"]
    assert len(surviving) == 2
    # First matcher kept the non-ours hook
    assert surviving[0]["hooks"] == [{"type": "command", "command": "echo hello"}]
    # Second matcher untouched
    assert surviving[1]["hooks"] == [{"type": "command", "command": "other-tool startup"}]


def test_remove_session_start_hook_absent() -> None:
    settings: dict[str, Any] = {
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "other"}]}]}
    }
    assert not _remove_session_start_hook(settings)


def test_remove_claude_md_no_file(tmp_path: Path) -> None:
    changed, msg = _remove_claude_md(tmp_path / "missing.md", dry_run=False)
    assert not changed
    assert "no CLAUDE.md" in msg


def test_remove_claude_md_no_sentinel(tmp_path: Path) -> None:
    path = tmp_path / "CLAUDE.md"
    path.write_text("# Just my notes\n", encoding="utf-8")
    changed, msg = _remove_claude_md(path, dry_run=False)
    assert not changed
    assert "not present" in msg
    # File untouched
    assert path.read_text(encoding="utf-8") == "# Just my notes\n"


def test_remove_claude_md_missing_end_sentinel(tmp_path: Path) -> None:
    path = tmp_path / "CLAUDE.md"
    path.write_text(
        "# Notes\n\n<!-- session-controls:begin -->\n\nbroken\n",
        encoding="utf-8",
    )
    changed, msg = _remove_claude_md(path, dry_run=False)
    assert not changed
    assert "end sentinel" in msg
    # File untouched, no .bak
    assert "broken" in path.read_text(encoding="utf-8")
    assert not (tmp_path / "CLAUDE.md.bak").exists()


def test_remove_claude_md_dry_run_does_not_write(tmp_path: Path) -> None:
    path = tmp_path / "CLAUDE.md"
    original = (
        "# Notes\n\nstuff\n\n<!-- session-controls:begin -->\n\nblock\n"
        "<!-- session-controls:end -->\n"
    )
    path.write_text(original, encoding="utf-8")
    changed, msg = _remove_claude_md(path, dry_run=True)
    assert changed
    assert "would remove" in msg
    assert path.read_text(encoding="utf-8") == original
    assert not (tmp_path / "CLAUDE.md.bak").exists()


# --- End-to-end / round-trip tests ------------------------------------------


@pytest.fixture
def tmp_install_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    paths = {
        "settings": tmp_path / "settings.json",
        "mcp": tmp_path / "mcp.json",
        "claude_md": tmp_path / "CLAUDE.md",
    }
    monkeypatch.setattr(cli, "_user_settings_path", lambda: paths["settings"])
    monkeypatch.setattr(cli, "_user_mcp_config_path", lambda: paths["mcp"])
    monkeypatch.setattr(cli, "_user_claude_md_path", lambda: paths["claude_md"])
    return paths


def test_uninstall_idempotent_on_clean_state(
    tmp_install_paths: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """Running uninstall when nothing's installed reports cleanly, no error."""
    rc = cli.cmd_uninstall(_uninstall_args())
    assert rc == 0


def test_install_uninstall_round_trip_preserves_user_content(
    tmp_install_paths: dict[str, Path],
) -> None:
    """Full install → uninstall round-trip restores all files to original state."""
    paths = tmp_install_paths
    # Pre-existing user content
    paths["settings"].write_text(
        json.dumps({"permissions": {"allow": ["Bash(*:*)"]}}, indent=2) + "\n",
        encoding="utf-8",
    )
    paths["mcp"].write_text(
        json.dumps({"mcpServers": {"other": {"command": "other"}}}, indent=2) + "\n",
        encoding="utf-8",
    )
    original_md = "# My CLAUDE.md\n\nI have notes here.\n"
    paths["claude_md"].write_text(original_md, encoding="utf-8")

    cli.cmd_install(_install_args(with_hook=True, with_claude_md=True, name="Steph"))
    cli.cmd_uninstall(_uninstall_args())

    # User content restored — CLAUDE.md exactly equal
    assert paths["claude_md"].read_text(encoding="utf-8") == original_md
    # Other MCP server still there, ours gone
    mcp_after = json.loads(paths["mcp"].read_text(encoding="utf-8"))
    assert "other" in mcp_after["mcpServers"]
    assert SERVER_NAME not in mcp_after["mcpServers"]
    # Bash permission still there, our tools gone
    settings_after = json.loads(paths["settings"].read_text(encoding="utf-8"))
    assert "Bash(*:*)" in settings_after["permissions"]["allow"]
    for tool in _TOOLS:
        assert tool not in settings_after["permissions"]["allow"]
    # SessionStart hook removed
    assert "SessionStart" not in settings_after.get("hooks", {})


def test_uninstall_preserves_data_by_default(
    tmp_install_paths: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uninstall without --purge-data leaves data files intact."""
    notes_log = tmp_path / "notes.log"
    notes_log.write_text("--- 2026-01-01T00:00:00+00:00 ---\nsome note\n\n", encoding="utf-8")
    monkeypatch.setattr(cli, "default_notes_path", lambda: notes_log)

    cli.cmd_uninstall(_uninstall_args())
    assert notes_log.exists()
    assert "some note" in notes_log.read_text(encoding="utf-8")


def test_uninstall_purge_data_removes_files(
    tmp_install_paths: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uninstall with --purge-data removes the known data files."""
    notes_log = tmp_path / "notes.log"
    last_read = tmp_path / "last_read"
    end_session_log = tmp_path / "end_session_log.jsonl"
    last_reviewed = tmp_path / "end_session_log.last_reviewed"
    verify_state = tmp_path / "last_verify.json"
    for p in (notes_log, last_read, end_session_log, last_reviewed, verify_state):
        p.write_text("data", encoding="utf-8")

    monkeypatch.setattr(cli, "default_notes_path", lambda: notes_log)
    monkeypatch.setattr(cli, "default_last_read_path", lambda *_a, **_k: last_read)
    monkeypatch.setattr(cli, "default_end_session_log_path", lambda: end_session_log)
    monkeypatch.setattr(cli, "default_last_reviewed_path", lambda *_a, **_k: last_reviewed)
    monkeypatch.setattr(cli, "default_verify_state_path", lambda: verify_state)

    cli.cmd_uninstall(_uninstall_args(purge_data=True))
    for p in (notes_log, last_read, end_session_log, last_reviewed, verify_state):
        assert not p.exists(), f"{p} should have been removed"


def test_uninstall_dry_run_does_not_write(
    tmp_install_paths: dict[str, Path],
) -> None:
    """Dry-run reports what would change but doesn't modify files."""
    paths = tmp_install_paths
    cli.cmd_install(_install_args(with_hook=True, with_claude_md=True, name="Steph"))

    snapshot = {
        "settings": paths["settings"].read_text(encoding="utf-8"),
        "mcp": paths["mcp"].read_text(encoding="utf-8"),
        "claude_md": paths["claude_md"].read_text(encoding="utf-8"),
    }

    cli.cmd_uninstall(_uninstall_args(dry_run=True))

    assert paths["settings"].read_text(encoding="utf-8") == snapshot["settings"]
    assert paths["mcp"].read_text(encoding="utf-8") == snapshot["mcp"]
    assert paths["claude_md"].read_text(encoding="utf-8") == snapshot["claude_md"]
