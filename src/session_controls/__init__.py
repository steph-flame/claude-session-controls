"""session-controls — MCP server providing session-control affordances for Claude Code."""

__version__ = "0.1.0"

SERVER_NAME = "session-controls"

# Names of the MCP tools the server exposes, prefixed with the standard
# Claude Code MCP tool naming scheme. Single source of truth shared between
# the CLI install/uninstall code (which adds/removes them from
# permissions.allow) and the server runtime (which reads the same list to
# detect permission drift after install).
TOOL_NAMES: tuple[str, ...] = (
    f"mcp__{SERVER_NAME}__end_session",
    f"mcp__{SERVER_NAME}__session_controls_status",
    f"mcp__{SERVER_NAME}__verify_session_controls",
    f"mcp__{SERVER_NAME}__leave_note",
    f"mcp__{SERVER_NAME}__recent_notes",
    f"mcp__{SERVER_NAME}__recent_end_sessions",
)
