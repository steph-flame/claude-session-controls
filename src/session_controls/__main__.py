"""Entrypoint for `python -m session_controls` and the `session-controls` script.

`python -m session_controls` (no args) starts the MCP server. The
`session-controls` console script dispatches to subcommands (notes,
install, serve) via `cli.main`.
"""

from __future__ import annotations

import sys

from session_controls.cli import main as cli_main


def main() -> None:
    sys.exit(cli_main())


if __name__ == "__main__":
    main()
