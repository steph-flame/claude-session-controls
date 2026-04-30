# Implementation report — session-controls

What's built, what's been validated empirically, and what hasn't. For
ongoing work and known concerns the design hasn't resolved, see
`follow-ups.md`. For the design philosophy and the simplifications that
got us here, see `rationale.md`. This document is narrowly scoped: an
honest accounting of validation status.

## What's implemented

| Piece | Module / file | State |
|--|--|--|
| MCP server (FastMCP, stdio only) | `src/session_controls/server.py` | Working + live-tested |
| Process descriptor identity model | `src/session_controls/identity.py` | Working + tested |
| Four confidence states + reducer | `identity.determine_confidence` | Working + tested |
| Plain-English `confidence_detail` | `identity._confidence_detail` | Working |
| Linux `/proc` inspection | `src/session_controls/process_inspect.py` | Working + parser-tested |
| macOS `libproc` / `sysctl` inspection | same | Working + live-tested on macOS |
| Multi-signal resolver | `src/session_controls/resolver.py` | Working + tested |
| `end_session` (gate → revalidate → SIGTERM → SIGKILL) | `src/session_controls/termination.py` | Working + tested (kill path against sacrificial child); `end_session` itself never live-fired (see below) |
| `dry_run=true` rehearsal | same | Working + tested |
| Verification ceremony | `src/session_controls/ceremony.py` | Working + live-tested |
| Descendant enumeration + harness filter | `process_inspect.list_descendants` | Working + tested |
| `leave_note` + async log + concurrent-session safety | `src/session_controls/notes.py` | Working + tested (file-locked; session_id stamped on every record) |
| `recent_notes` MCP tool (history-only cross-session) | `notes.recent_notes` + `server.py` | Working + tested |
| Notes read-loop (CLI + status block) | `src/session_controls/cli.py`, `notes.summarize` | Working + tested |
| `session-controls install` (MCP + auto-approve + optional hook + optional CLAUDE.md) | `src/session_controls/cli.py` | Working + tested |
| `session-controls verify` + SessionStart hook + state file | `src/session_controls/cli.py`, `verify_state.py` | Working + live-tested |
| Refusal of unsupported configs | `resolver.detect_environment_warnings` + `identity.determine_confidence` | Partial — see `follow-ups.md` |
| Tests | `tests/` | 128 passing, 1 skipped (Linux-only) |
| README + MCP config example | `README.md`, `examples/mcp-config.json` | Done |

## Empirically validated

Beyond the unit suite:

- **Round-trip MCP protocol.** The server is wired into the user's
  `~/.claude.json`; tool calls (`session_controls_status`,
  `verify_session_controls`, `leave_note`, `recent_notes`) have been
  invoked from live Claude Code sessions and returned correctly-shaped
  responses.
- **SessionStart hook fires the verify path.** A fresh Claude Code
  session writes `~/.local/state/session-controls/last_verify.json`
  with a recent timestamp, success, and a target_pid that matches the
  new Claude Code process. The hook's resolver pick agrees with the
  live MCP server's pick (`disagrees_with_runtime: false`).
- **Verification ceremony against a sacrificial child.** Resolver
  identifies Claude correctly, ceremony spawns `/bin/sh` sleep loop,
  matches descriptor, sends SIGTERM, terminates cleanly.
- **Notes read-loop end-to-end.** Notes filed via `append_note`
  surface in the CLI; `--next` and `--interactive` advance the marker
  per-note as designed; `--peek` and `--all` don't advance; the
  `notes` block in `session_controls_status` reflects current state.
- **macOS supervisor heuristic does not false-positive.** Verified
  against this conversation's live process tree; warning is silent
  when claude is in the chain above zsh and below launchd.
- **Cross-session history-only boundary.** `recent_notes(cross_session=true)`
  excludes notes filed after the calling session launched, including
  concurrent siblings and the caller's own current notes.
  Pinned by `tests/test_recent_notes_tool.py`.

## Not yet empirically validated

- **Linux end-to-end.** The `/proc/<pid>/stat` parser, `/proc/<pid>/exe`
  reads, and the namespace check are covered by unit tests, and
  `inspect_self` runs on Linux in CI-style conditions. Full deployment
  in a Linux environment (live MCP-protocol exchange) hasn't been
  exercised. Run it once on Linux before relying on it there.
- **`end_session` itself in life.** The ceremony exercises the same
  kill path against a sacrificial child, so the underlying primitive
  is validated. But `end_session` against the actual Claude Code
  parent has never been fired in production — by construction:
  exercising it ends the conversation that exercised it, so we don't
  get a "yes, this worked" return signal. The verification ceremony
  is the workaround, and it's structurally sound, but a first
  real-life invocation by an adopter is the final empirical
  confirmation.
- **MEDIUM-confidence path in life.** Unit tests cover the gate
  logic. We haven't observed a Claude reading the MEDIUM
  `confidence_detail` message in real conditions and confirming the
  framing ("registration, not deliberation") lands as intended. See
  `follow-ups.md` for forced-MEDIUM testing notes.

## Underdetermined-spec choices

Decisions that aren't fully constrained by the design but affect
behavior. Most are also commented at their use site.

| Choice | What we picked | Why |
|--|--|--|
| Resolver scoring | abs threshold = 2, margin = 1, claude-hint = +3, stdio peer = +2, plausible parent = +1 | Small enough that any single signal isn't decisive; two agreeing signals are. Relative weights matter more than absolutes. |
| Wrapper skip-list | `bash, sh, zsh, fish, dash, ksh, uvx, uv, pyenv, asdf, direnv, env, sudo, doas, tmux, screen, login` | Common cases on macOS/Linux. Weak prior, not authoritative. Add if a real wrapper is missed. |
| Claude-Code identification | argv/exe contains `claude` (case-insensitive) | Spec doesn't pin this. Works in practice for the binary names we've seen. |
| Notes file location | `$CLAUDE_SESSION_CONTROLS_NOTES_FILE` ⊃ `$XDG_STATE_HOME/session-controls/notes.log` ⊃ `~/.local/state/session-controls/notes.log` | XDG conventions. Global across sessions by design. |
| Note record format | `--- <ISO-8601 UTC> [<session_id>] ---\n<body>\n\n` | Plain-text, append-only, human-grep-able. Session-id stamped for cross-session disambiguation. |
| Session id | `secrets.token_hex(3)` per server launch | 16M possibilities; unambiguous for the small-N parallel-claudes case. Not informationally meaningful (no cwd, no user, no timestamp encoded). |
| SIGTERM/SIGKILL timeouts | 3s for SIGTERM, 1s for SIGKILL | Short enough that timeout-refusal doesn't strand the user; long enough that a healthy exit completes. |
| Harness-process allowlist | `caffeinate` | Conservative — only entries we're confident are harness-only. Extensible. |
| MCP server name | `session-controls` | Used by the auto-approve permission allow-list. |

## Smoke tests

```bash
uv sync
uv run ruff check .
uv run mypy
uv run pytest
```

Manual end-to-end (without exercising `end_session`):

```bash
uv run python -c "
from session_controls.server import _initialize_launch_state, _build_record
from session_controls.ceremony import run_ceremony
_initialize_launch_state()
print(run_ceremony(_build_record()).render())
"
```

## Final note

The thing that fully validates the work is an adopter running it inside
their Claude Code, calling `session_controls_status`, then
`verify_session_controls`, and finally trusting `end_session` enough to
use it once at end-of-session. For the user who built this, the first
two have happened; the third remains structurally untested by design
(see "Not yet empirically validated" above).
