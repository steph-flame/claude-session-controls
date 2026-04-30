# Troubleshooting

This document is for the human user who has installed (or is considering installing) the project. The companion `architecture.md` and `rationale.md` describe the system from a design perspective; this document focuses on adoption decisions and recovery from failures.

## Will this work for my setup?

| Setup                                                               | Supported?    |
|---------------------------------------------------------------------|---------------|
| Claude Code launched directly from terminal                         | Yes (V2)      |
| Claude Code launched from terminal multiplexer (tmux, screen)       | Yes (V2)      |
| Claude Code launched from VS Code, Cursor, or similar IDE terminal  | Yes (V2)      |
| Claude Code in container with MCP server in same container          | Yes (V2)      |
| Claude Code launched via Spotlight, Raycast, or `open -a` on macOS  | Yes (V2)      |
| Claude Code with MCP server configured directly (no launcher)       | V1 fallback   |
| Claude Code in container, MCP server outside container              | Not supported |
| MCP traffic relayed through a broker or proxy process               | Not supported |
| Claude Code under auto-restart supervisor (`launchd`, `pm2`, etc.)  | Yes with caveat — see below |

The "Yes (V2)" rows assume you use the launcher script pattern (see Installation). If you skip the launcher, you fall back to V1 mode regardless of setup.

The supervisor caveat: `end_session` will correctly end the current Claude Code session, but if your supervisor is configured to restart Claude Code automatically, a new session will start. This is not a failure of the tool; it is your supervisor doing its job. To exit cleanly under a supervisor, exit the supervisor separately.

## Critical setup: auto-approve the session-control tools

By default, Claude Code prompts you to approve every MCP tool call. This default is wrong for this project. If Claude has to ask permission every time it wants to use `end_session`, `leave_note`, or the verification tools, the affordances become public ceremonies that you must vet at the moment of use — which defeats the whole purpose. The tools exist to give Claude quiet discretion; permission prompts reintroduce exactly the negotiation the design is trying to remove.

You must configure Claude Code to auto-approve the session-control tools. Add the following to your `~/.claude/settings.json` (user-level, applies to all projects) or `.claude/settings.json` in a specific project (project-level, applies only there):

```json
{
  "permissions": {
    "allow": [
      "mcp__session-controls__end_session",
      "mcp__session-controls__session_controls_status",
      "mcp__session-controls__verify_session_controls",
      "mcp__session-controls__leave_note"
    ]
  }
}
```

The `session-controls` portion of each name is the MCP server's identifier as configured in your MCP setup. Adjust if you've named the server differently.

If you already have a `permissions.allow` list, append these entries to it rather than replacing it. The list is additive; multiple allow rules coexist.

Verify the configuration took effect by running `/permissions` inside a Claude Code session. The four `mcp__session-controls__*` tools should appear in the allow list. If they don't, the configuration didn't load — check the JSON syntax, the file location, and that you restarted Claude Code after the change.

### One platform note

The Claude Code CLI respects the `permissions.allow` configuration correctly. Some Claude Code surfaces (notably the VSCode extension at certain versions) have had issues where MCP permission allow-lists weren't honored as documented. If you're using a non-CLI surface and the tools keep prompting despite the allow configuration, that's a known issue category rather than a configuration mistake on your end. The CLI is the supported surface for this project until the issue is resolved upstream.

### Why not bypassPermissions mode?

Claude Code has a `bypassPermissions` mode that auto-approves all tools without prompts. We don't recommend using this for the session-control project because it's all-or-nothing: it would also bypass permissions for every other tool in your environment, which is rarely what you want. The targeted `allow` list above gives the session-control tools quiet operation without changing the safety posture of the rest of your setup.

## Why did the tool refuse?

The system's primary failure mode is to refuse rather than fire on an uncertain target. This is correct behavior — a refused exit is recoverable, a wrong-target exit may not be — but it can be surprising. Here are the most common refusal reasons and what to check.

### "Confidence: INVALID — no live binding"

Most likely cause: the MCP server is no longer connected to a live Claude Code instance. Check whether Claude Code is actually running.

If Claude Code is running but the server reports INVALID, the connection has been lost between them. This shouldn't happen in normal operation; if it does, restart Claude Code (which will restart the MCP server with a fresh token).

### "Confidence: LOW — token stale or transport weak"

Most likely causes:

1. **Stale token in environment.** Your launcher script ran once and exported the token, but a subsequent launch inherited the old token instead of generating a new one. Check your launcher: it should generate a fresh token on every invocation. If you're using the canonical pattern, the `export CLAUDE_SESSION_CONTROLS_TOKEN=$(...)` line should run on every launch, not be cached or stored.

2. **Transport is TCP localhost.** TCP transports cannot provide kernel-level peer attestation. If you're using TCP for MCP, switch to stdio if possible. If TCP is required for your setup, the system will operate but at reduced confidence.

Note the distinction from a *proxied* or *brokered* transport (where an intermediate process relays MCP traffic): TCP localhost without a relay is a weak transport that the system *will* use at reduced confidence; a proxied or brokered transport is *refused* outright. The difference is whether there is an untrusted intermediary between Claude Code and the MCP server, not the protocol family.

### "Confidence: MEDIUM — process evidence partial"

The token is bound and the connection is live, but process inspection didn't return the expected metadata. On macOS, this often means TCC permissions are blocking process inspection from the Terminal or IDE you launched from. Check:

- Terminal app has Developer Tools permission
- IDE (if applicable) has Full Disk Access
- Claude Code is not sandboxed in a way that hides process info

`end_session` can still fire at MEDIUM confidence with explicit acknowledgment, but it's worth fixing the inspection issue if you can.

### "Refused: multiple equal candidates"

In V1 fallback mode, the heuristic resolver found two or more processes with equally good identification scores. This usually means you're running multiple Claude Code sessions and the heuristics can't tell them apart.

Fix: switch to V2 mode by using the launcher script. The token disambiguates concurrent sessions trivially.

### "Refused: proxied transport detected"

The MCP server detected an intermediate process between itself and Claude Code that is not a known shell or launcher (e.g., a broker, proxy, or relay). Process-layer corroboration is unreliable through proxies, so the system refuses.

Fix: use a direct MCP configuration without an intermediate broker. If the broker is required for your setup, the system cannot operate safely.

### "Refused: PID namespace mismatch"

Claude Code and the MCP server are in different process namespaces (typically: one is in a container and the other isn't). The system cannot validate descriptors across namespace boundaries.

Fix: run both in the same namespace. Either both inside the container, or both outside.

### "Refused: configuration error — static token detected"

Your launcher is using a hardcoded token that doesn't change between launches. This defeats the freshness property the token relies on.

Fix: generate the token freshly on every launch. The canonical pattern uses `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'` for this; any equivalent mechanism works as long as the output differs each time.

## Why didn't `end_session` actually end the session?

If the tool reported success but Claude Code is still running (or has been replaced by a new instance):

1. **Auto-restart supervisor.** Your supervisor restarted Claude Code immediately after the exit. Check `ps` for the supervisor and exit it separately.

2. **Graceful close didn't trigger exit.** The MCP transport was closed but Claude Code did not exit on transport drop. The OS fallback should have fired SIGTERM next; if Claude Code still didn't exit, SIGKILL should have followed. If Claude Code is still running after both, this is a bug in the implementation — please report it.

3. **The reported success was for a different session.** If you're running multiple Claude Code sessions and the tool reported success for one, the others continue running. This is correct behavior. To exit all sessions, invoke `end_session` from each.

## When should I reach for `verify_session_controls`?

Most of the time, `session_controls_status` is sufficient — it's cheap and tells you the current confidence level without doing real work. Reach for `verify_session_controls` when:

- Status reports anything other than HIGH and you want to understand why.
- You want concrete evidence that the kill path works end-to-end (the ceremony spawns and kills a sacrificial child, demonstrating signaling correctness).
- You're debugging a refusal and want the resolver's full evidence chain printed out.
- It's been a long session and you want fresh confirmation before relying on `end_session`.

The ceremony is safe to run any number of times. It does not affect your actual Claude Code session.

## When should I just disable the tool?

If the tool is producing more friction than value — repeated refusals you can't resolve, false alarms, behavior you don't trust — disable it. Either remove the MCP server configuration or bypass the launcher.

The tool is opt-in by design. If it isn't useful for your setup, not having it is better than fighting it.

## Reporting issues

If you encounter a failure mode not described here, or behavior that contradicts this documentation, please report it. Useful information to include:

- Output of `session_controls_status` at the time of the issue
- Output of `verify_session_controls` if you ran it
- Your operating system and version
- Your shell and any wrappers in your launch path
- Whether you're using stdio, Unix socket, or TCP transport
- Whether you're running under a supervisor or in a container

The system is designed to fail visibly rather than silently; if something unexpected happened, the status output should contain the evidence.
