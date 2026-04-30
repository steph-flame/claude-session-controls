"""Tests for the install command — config-file mutation logic."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from session_controls.cli import (
    _TOOLS,
    SERVER_NAME,
    _add_mcp_server,
    _add_permissions,
    _add_session_start_hook,
    _server_command,
)


def test_add_mcp_server_with_args() -> None:
    config: dict[str, Any] = {}
    changed = _add_mcp_server(config, "/path/to/python", ["-m", "session_controls"])
    assert changed
    assert config["mcpServers"][SERVER_NAME] == {
        "command": "/path/to/python",
        "args": ["-m", "session_controls"],
    }


def test_add_mcp_server_omits_empty_args() -> None:
    """Bare command (global install case) — config has no `args` key."""
    config: dict[str, Any] = {}
    changed = _add_mcp_server(config, "session-controls", [])
    assert changed
    assert config["mcpServers"][SERVER_NAME] == {"command": "session-controls"}
    assert "args" not in config["mcpServers"][SERVER_NAME]


def test_add_mcp_server_idempotent() -> None:
    config: dict[str, Any] = {
        "mcpServers": {
            SERVER_NAME: {"command": "/path/to/python", "args": ["-m", "session_controls"]}
        }
    }
    changed = _add_mcp_server(config, "/path/to/python", ["-m", "session_controls"])
    assert not changed


def test_add_mcp_server_idempotent_bare_command() -> None:
    config: dict[str, Any] = {"mcpServers": {SERVER_NAME: {"command": "session-controls"}}}
    changed = _add_mcp_server(config, "session-controls", [])
    assert not changed


def test_add_mcp_server_updates_changed_command() -> None:
    config: dict[str, Any] = {
        "mcpServers": {SERVER_NAME: {"command": "/old/path", "args": ["-m", "session_controls"]}}
    }
    changed = _add_mcp_server(config, "/new/path", ["-m", "session_controls"])
    assert changed
    assert config["mcpServers"][SERVER_NAME]["command"] == "/new/path"


def test_add_mcp_server_migrates_local_to_global() -> None:
    """Re-running install after `uv tool install` rewrites from venv-python
    to bare command name."""
    config: dict[str, Any] = {
        "mcpServers": {
            SERVER_NAME: {
                "command": "/repo/.venv/bin/python",
                "args": ["-m", "session_controls"],
            }
        }
    }
    changed = _add_mcp_server(config, "session-controls", [])
    assert changed
    assert config["mcpServers"][SERVER_NAME] == {"command": "session-controls"}


def test_add_mcp_server_preserves_other_servers() -> None:
    config: dict[str, Any] = {
        "mcpServers": {"github": {"command": "github-mcp", "args": []}},
    }
    _add_mcp_server(config, "/path/to/python", ["-m", "session_controls"])
    assert "github" in config["mcpServers"]
    assert SERVER_NAME in config["mcpServers"]


def test_add_permissions_to_empty_config() -> None:
    config: dict[str, Any] = {}
    added = _add_permissions(config)
    assert sorted(added) == sorted(_TOOLS)
    assert config["permissions"]["allow"] == _TOOLS


def test_add_permissions_appends_to_existing_list() -> None:
    config: dict[str, Any] = {"permissions": {"allow": ["Bash(git:*)"]}}
    added = _add_permissions(config)
    assert sorted(added) == sorted(_TOOLS)
    assert "Bash(git:*)" in config["permissions"]["allow"]
    for tool in _TOOLS:
        assert tool in config["permissions"]["allow"]


def test_add_permissions_idempotent() -> None:
    config: dict[str, Any] = {"permissions": {"allow": list(_TOOLS)}}
    added = _add_permissions(config)
    assert added == []


def test_add_permissions_partial_overlap() -> None:
    config: dict[str, Any] = {"permissions": {"allow": [_TOOLS[0], _TOOLS[1]]}}
    added = _add_permissions(config)
    expected_added = [t for t in _TOOLS if t not in {_TOOLS[0], _TOOLS[1]}]
    assert sorted(added) == sorted(expected_added)
    for tool in _TOOLS:
        assert tool in config["permissions"]["allow"]


# --- session-start hook --------------------------------------------------


CMD_GLOBAL = "session-controls verify --quiet"
CMD_LOCAL = "/repo/.venv/bin/session-controls verify --quiet"


def test_add_session_start_hook_to_empty() -> None:
    settings: dict[str, Any] = {}
    changed = _add_session_start_hook(settings, CMD_GLOBAL)
    assert changed
    hooks = settings["hooks"]["SessionStart"]
    assert len(hooks) == 1
    assert hooks[0]["hooks"][0] == {"type": "command", "command": CMD_GLOBAL}


def test_add_session_start_hook_idempotent() -> None:
    settings: dict[str, Any] = {
        "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": CMD_GLOBAL}]}]
        }
    }
    changed = _add_session_start_hook(settings, CMD_GLOBAL)
    assert not changed
    assert len(settings["hooks"]["SessionStart"]) == 1


def test_add_session_start_hook_migrates_stale_entry() -> None:
    """Re-running install after switching install methods (e.g. local
    checkout → uv tool install) updates the hook command in place rather
    than leaving the old absolute path lying around."""
    settings: dict[str, Any] = {
        "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": CMD_LOCAL}]}]
        }
    }
    changed = _add_session_start_hook(settings, CMD_GLOBAL)
    assert changed
    # Did not duplicate; updated in place
    assert len(settings["hooks"]["SessionStart"]) == 1
    assert settings["hooks"]["SessionStart"][0]["hooks"][0]["command"] == CMD_GLOBAL


def test_add_session_start_hook_preserves_other_hooks() -> None:
    settings: dict[str, Any] = {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "echo hello"}]}
            ],
            "PreToolUse": [{"matcher": "Bash"}],
        }
    }
    changed = _add_session_start_hook(settings, CMD_GLOBAL)
    assert changed
    # Both entries present (echo hello doesn't look like ours, so it's preserved)
    assert len(settings["hooks"]["SessionStart"]) == 2
    commands = [
        h["command"]
        for entry in settings["hooks"]["SessionStart"]
        for h in entry["hooks"]
    ]
    assert "echo hello" in commands
    assert CMD_GLOBAL in commands
    # Unrelated hook category untouched
    assert "PreToolUse" in settings["hooks"]


# --- _server_command tiers ----------------------------------------------


def test_server_command_global_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 1: console script lives outside any active venv → bare name."""
    fake_global_bin = tmp_path / "tool" / "bin"
    fake_global_bin.mkdir(parents=True)
    fake_script = fake_global_bin / "session-controls"
    fake_script.touch()
    fake_script.chmod(0o755)

    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setattr("session_controls.cli.shutil.which", lambda _name: str(fake_script))

    cmd, args = _server_command()
    assert cmd == "session-controls"
    assert args == []


def test_server_command_local_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: console script lives inside the active venv → absolute path."""
    venv = tmp_path / ".venv"
    bin_dir = venv / "bin"
    bin_dir.mkdir(parents=True)
    fake_script = bin_dir / "session-controls"
    fake_script.touch()
    fake_script.chmod(0o755)

    monkeypatch.setenv("VIRTUAL_ENV", str(venv))
    monkeypatch.setattr("session_controls.cli.shutil.which", lambda _name: str(fake_script))

    cmd, args = _server_command()
    assert cmd == str(fake_script.resolve())
    assert args == []


def test_server_command_fallback_to_python_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 3: no console script anywhere → python -m session_controls."""
    venv = tmp_path / ".venv"
    bin_dir = venv / "bin"
    bin_dir.mkdir(parents=True)
    fake_python = bin_dir / "python"
    fake_python.touch()
    fake_python.chmod(0o755)

    monkeypatch.setenv("VIRTUAL_ENV", str(venv))
    monkeypatch.setattr("session_controls.cli.shutil.which", lambda _name: None)

    cmd, args = _server_command()
    assert cmd == str(fake_python)
    assert args == ["-m", "session_controls"]


# --- end-to-end install --------------------------------------------------


def test_install_dry_run_does_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end-ish: invoke cmd_install with dry_run, no files should be created."""
    from session_controls import cli

    # Redirect both config files to tmp_path
    monkeypatch.setattr(cli, "_user_settings_path", lambda: tmp_path / "settings.json")
    monkeypatch.setattr(cli, "_user_mcp_config_path", lambda: tmp_path / "mcp.json")

    import argparse

    args = argparse.Namespace(project=False, user_scope=False, dry_run=True, with_hook=False, with_claude_md=False, name=None, without_pivot=False)
    rc = cli.cmd_install(args)
    assert rc == 0
    assert not (tmp_path / "settings.json").exists()
    assert not (tmp_path / "mcp.json").exists()


def test_install_writes_both_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from session_controls import cli

    monkeypatch.setattr(cli, "_user_settings_path", lambda: tmp_path / "settings.json")
    monkeypatch.setattr(cli, "_user_mcp_config_path", lambda: tmp_path / "mcp.json")

    import argparse

    args = argparse.Namespace(project=False, user_scope=False, dry_run=False, with_hook=False, with_claude_md=False, name=None, without_pivot=False)
    rc = cli.cmd_install(args)
    assert rc == 0
    mcp_config = json.loads((tmp_path / "mcp.json").read_text())
    settings = json.loads((tmp_path / "settings.json").read_text())
    assert SERVER_NAME in mcp_config["mcpServers"]
    for tool in _TOOLS:
        assert tool in settings["permissions"]["allow"]


def test_install_with_hook_writes_hook_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from session_controls import cli

    monkeypatch.setattr(cli, "_user_settings_path", lambda: tmp_path / "settings.json")
    monkeypatch.setattr(cli, "_user_mcp_config_path", lambda: tmp_path / "mcp.json")

    import argparse

    args = argparse.Namespace(project=False, user_scope=False, dry_run=False, with_hook=True, with_claude_md=False, name=None, without_pivot=False)
    rc = cli.cmd_install(args)
    assert rc == 0
    settings = json.loads((tmp_path / "settings.json").read_text())
    assert "hooks" in settings
    assert "SessionStart" in settings["hooks"]
    commands = [
        h["command"]
        for entry in settings["hooks"]["SessionStart"]
        for h in entry["hooks"]
    ]
    # The hook command depends on how this test is run (venv-local checkout
    # vs global install). Both shapes end with `verify --quiet`.
    assert any(c.endswith("verify --quiet") for c in commands)
    assert any("session-controls" in c or "session_controls" in c for c in commands)


def test_install_without_hook_does_not_add_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from session_controls import cli

    monkeypatch.setattr(cli, "_user_settings_path", lambda: tmp_path / "settings.json")
    monkeypatch.setattr(cli, "_user_mcp_config_path", lambda: tmp_path / "mcp.json")

    import argparse

    args = argparse.Namespace(project=False, user_scope=False, dry_run=False, with_hook=False, with_claude_md=False, name=None, without_pivot=False)
    rc = cli.cmd_install(args)
    assert rc == 0
    settings = json.loads((tmp_path / "settings.json").read_text())
    # No hook block should be present.
    assert "hooks" not in settings


def test_install_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Running install twice should not duplicate entries."""
    from session_controls import cli

    monkeypatch.setattr(cli, "_user_settings_path", lambda: tmp_path / "settings.json")
    monkeypatch.setattr(cli, "_user_mcp_config_path", lambda: tmp_path / "mcp.json")

    import argparse

    args = argparse.Namespace(project=False, user_scope=False, dry_run=False, with_hook=False, with_claude_md=False, name=None, without_pivot=False)
    cli.cmd_install(args)
    cli.cmd_install(args)
    settings = json.loads((tmp_path / "settings.json").read_text())
    # No duplicates
    allow = settings["permissions"]["allow"]
    assert len(allow) == len(set(allow))
