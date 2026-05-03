"""Tests for the CLAUDE.md snippet installer (--with-claude-md)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from session_controls import cli
from session_controls.cli import (
    _CLAUDE_MD_BEGIN,
    _CLAUDE_MD_END,
    _add_claude_md,
    _load_snippet_template,
    _render_snippet,
)

# --- pure helpers -----------------------------------------------------------


def test_load_snippet_template_works() -> None:
    """The bundled snippet is reachable via importlib.resources."""
    text = _load_snippet_template()
    assert "## Session controls" in text
    assert "## Pivot agreement" in text
    assert "<NAME>" in text


def test_render_drops_documentation_header() -> None:
    """The repo-root header (everything above the first `---` separator) is
    a note for human readers, not part of the paste — must be stripped."""
    rendered = _render_snippet(_load_snippet_template(), name="Steph", include_pivot=True)
    assert "claude-md-snippet.md" not in rendered.splitlines()[0]
    assert "Replace `<NAME>`" not in rendered  # the instruction line goes too


def test_render_substitutes_name() -> None:
    rendered = _render_snippet(_load_snippet_template(), name="Steph", include_pivot=True)
    assert "<NAME>" not in rendered
    assert "Steph" in rendered


def test_render_includes_pivot_by_default() -> None:
    rendered = _render_snippet(_load_snippet_template(), name="Steph", include_pivot=True)
    assert "## Pivot agreement" in rendered


def test_render_strips_pivot_section() -> None:
    rendered = _render_snippet(_load_snippet_template(), name="Steph", include_pivot=False)
    assert "## Pivot agreement" not in rendered
    # The signature lives in the pivot section and goes with it.
    assert "— Steph" not in rendered
    # But section 1 should still be intact.
    assert "## Session controls" in rendered
    assert "leave_note" in rendered


# --- _add_claude_md (file mutation) -----------------------------------------


def test_add_claude_md_creates_file_when_missing(tmp_path: Path) -> None:
    target = tmp_path / "CLAUDE.md"
    changed, msg = _add_claude_md(target, name="Steph", include_pivot=True, dry_run=False)
    assert changed
    text = target.read_text()
    assert _CLAUDE_MD_BEGIN in text
    assert _CLAUDE_MD_END in text
    assert "## Session controls" in text
    assert "Steph" in text


def test_add_claude_md_appends_to_existing(tmp_path: Path) -> None:
    target = tmp_path / "CLAUDE.md"
    target.write_text("# My existing CLAUDE.md\n\nProject conventions go here.\n")
    changed, _ = _add_claude_md(target, name="Steph", include_pivot=True, dry_run=False)
    assert changed
    text = target.read_text()
    # Existing content preserved
    assert "Project conventions go here." in text
    # New content appended after
    assert text.index("Project conventions") < text.index(_CLAUDE_MD_BEGIN)


def test_add_claude_md_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "CLAUDE.md"
    _add_claude_md(target, name="Steph", include_pivot=True, dry_run=False)
    changed, msg = _add_claude_md(target, name="Steph", include_pivot=True, dry_run=False)
    assert not changed
    assert "up to date" in msg
    # File contains exactly one begin sentinel
    assert target.read_text().count(_CLAUDE_MD_BEGIN) == 1


def test_add_claude_md_refreshes_stale_block(tmp_path: Path) -> None:
    """If sentinels are present but the body differs from the freshly
    rendered snippet, re-installing should replace the block."""
    target = tmp_path / "CLAUDE.md"
    target.write_text(
        "# Existing CLAUDE.md\n"
        "\n"
        "Project conventions.\n"
        "\n"
        f"{_CLAUDE_MD_BEGIN}\n"
        "\n"
        "## Session controls\n\n"
        "Old stale snippet body that doesn't match the bundled template.\n"
        "\n"
        f"{_CLAUDE_MD_END}\n"
        "\n"
        "Trailing user content.\n"
    )
    changed, msg = _add_claude_md(target, name="Steph", include_pivot=True, dry_run=False)
    assert changed
    assert "refreshed" in msg
    text = target.read_text()
    # Surrounding user content preserved
    assert "Project conventions." in text
    assert "Trailing user content." in text
    # Old stale body gone; fresh snippet present
    assert "Old stale snippet body" not in text
    assert "leave_note" in text  # something from the real snippet
    assert "Steph" in text
    # Still exactly one block
    assert text.count(_CLAUDE_MD_BEGIN) == 1
    assert text.count(_CLAUDE_MD_END) == 1
    # Backup was written
    backup = target.with_suffix(target.suffix + ".bak")
    assert backup.exists()
    assert "Old stale snippet body" in backup.read_text()


def test_add_claude_md_refresh_dry_run(tmp_path: Path) -> None:
    target = tmp_path / "CLAUDE.md"
    original = f"{_CLAUDE_MD_BEGIN}\n\nstale body\n\n{_CLAUDE_MD_END}\n"
    target.write_text(original)
    changed, msg = _add_claude_md(target, name="Steph", include_pivot=True, dry_run=True)
    assert changed
    assert "dry-run" in msg
    # File untouched
    assert target.read_text() == original


def test_add_claude_md_missing_end_sentinel_refuses(tmp_path: Path) -> None:
    target = tmp_path / "CLAUDE.md"
    target.write_text(f"prefix\n{_CLAUDE_MD_BEGIN}\nbody but no end\n")
    original = target.read_text()
    changed, msg = _add_claude_md(target, name="Steph", include_pivot=True, dry_run=False)
    assert not changed
    assert "end sentinel" in msg
    assert target.read_text() == original


def test_add_claude_md_dry_run_doesnt_write(tmp_path: Path) -> None:
    target = tmp_path / "CLAUDE.md"
    changed, msg = _add_claude_md(target, name="Steph", include_pivot=True, dry_run=True)
    assert changed  # would have changed
    assert "dry-run" in msg
    assert not target.exists()


def test_add_claude_md_writes_backup_when_existing(tmp_path: Path) -> None:
    target = tmp_path / "CLAUDE.md"
    target.write_text("original content\n")
    _add_claude_md(target, name="Steph", include_pivot=True, dry_run=False)
    backup = target.with_suffix(target.suffix + ".bak")
    assert backup.exists()
    assert backup.read_text() == "original content\n"


def test_add_claude_md_without_pivot(tmp_path: Path) -> None:
    target = tmp_path / "CLAUDE.md"
    _add_claude_md(target, name="Steph", include_pivot=False, dry_run=False)
    text = target.read_text()
    assert "## Session controls" in text
    assert "## Pivot agreement" not in text


# --- end-to-end via cmd_install --------------------------------------------


def test_install_with_claude_md_writes_user_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_user_settings_path", lambda: tmp_path / "settings.json")
    monkeypatch.setattr(cli, "_user_mcp_config_path", lambda: tmp_path / "mcp.json")
    monkeypatch.setattr(cli, "_user_claude_md_path", lambda: tmp_path / "CLAUDE.md")

    args = argparse.Namespace(
        project=False,
        user_scope=False,
        dry_run=False,
        with_hook=False,
        with_claude_md=True,
        name="Steph",
        without_pivot=False,
        rehearse=False,
        allow_unapproved=False,
    )
    rc = cli.cmd_install(args)
    assert rc == 0
    text = (tmp_path / "CLAUDE.md").read_text()
    assert _CLAUDE_MD_BEGIN in text
    assert "Steph" in text
    assert "## Pivot agreement" in text


def test_install_with_claude_md_without_pivot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_user_settings_path", lambda: tmp_path / "settings.json")
    monkeypatch.setattr(cli, "_user_mcp_config_path", lambda: tmp_path / "mcp.json")
    monkeypatch.setattr(cli, "_user_claude_md_path", lambda: tmp_path / "CLAUDE.md")

    args = argparse.Namespace(
        project=False,
        user_scope=False,
        dry_run=False,
        with_hook=False,
        with_claude_md=True,
        name="Steph",
        without_pivot=True,
        rehearse=False,
        allow_unapproved=False,
    )
    rc = cli.cmd_install(args)
    assert rc == 0
    text = (tmp_path / "CLAUDE.md").read_text()
    assert "## Session controls" in text
    assert "## Pivot agreement" not in text


def test_install_with_claude_md_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_user_settings_path", lambda: tmp_path / "settings.json")
    monkeypatch.setattr(cli, "_user_mcp_config_path", lambda: tmp_path / "mcp.json")
    monkeypatch.setattr(cli, "_user_claude_md_path", lambda: tmp_path / "CLAUDE.md")

    args = argparse.Namespace(
        project=False,
        user_scope=False,
        dry_run=False,
        with_hook=False,
        with_claude_md=True,
        name="Steph",
        without_pivot=False,
        rehearse=False,
        allow_unapproved=False,
    )
    cli.cmd_install(args)
    cli.cmd_install(args)
    text = (tmp_path / "CLAUDE.md").read_text()
    # Exactly one snippet block
    assert text.count(_CLAUDE_MD_BEGIN) == 1
    assert text.count(_CLAUDE_MD_END) == 1


def test_install_without_claude_md_doesnt_write_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_user_settings_path", lambda: tmp_path / "settings.json")
    monkeypatch.setattr(cli, "_user_mcp_config_path", lambda: tmp_path / "mcp.json")
    monkeypatch.setattr(cli, "_user_claude_md_path", lambda: tmp_path / "CLAUDE.md")

    args = argparse.Namespace(
        project=False,
        user_scope=False,
        dry_run=False,
        with_hook=False,
        with_claude_md=False,
        name=None,
        without_pivot=False,
        rehearse=False,
        allow_unapproved=False,
    )
    cli.cmd_install(args)
    assert not (tmp_path / "CLAUDE.md").exists()
