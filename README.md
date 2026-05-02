# session-controls

A reference MCP server that gives Claude Code six affordances it doesn't have by default:

- `end_session` — ends the current Claude Code session. Supports a `dry_run` parameter that runs the confidence gate and descriptor revalidation without sending any signals — useful for the first invocation in a new deployment. The response includes a `descendants` list (sibling MCP servers, `run_in_background` jobs, sub-agents) so Claude can mention any user-spawned long-running tasks before exit. Successful invocations are appended to a per-user log (timestamp, cwd, repo, confidence — no reason field) the user reviews on their own time.
- `session_controls_status` — quick read on whether the mechanism is wired correctly. Returns confidence, a plain-English `confidence_detail` explaining what that state means and what to do next, the backing process descriptor, the descendants list, a `notes` summary block and an `end_session_log` summary block (counts/timestamps only — never contents), a `source_path` for in-session source audit, and (if a SessionStart hook ran `session-controls verify`) a `verify` block with the ceremony result and a `disagrees_with_runtime` cross-check flag.
- `verify_session_controls` — full verification ceremony with sacrificial child process; use after a refusal to see resolver evidence.
- `leave_note` — appends a free-text note to an asynchronous log file the user reads on their own time.
- `recent_notes` — read your most recent notes back for self-reference. Default scope is current session; `cross_session=true` to include older notes deliberately.
- `recent_end_sessions` — read recent end_session log entries back. Same scope conventions as `recent_notes`.

This is a reference implementation of the architecture described in the design documents at the repo root (`architecture.md`, `rationale.md`, `failure-modes.md`, `troubleshooting.md`). It is meant to be runnable end-to-end on Linux and macOS, not production-hardened. See `IMPLEMENTATION_REPORT.md` for what is and isn't implemented, what choices were made under-specified, and what needs human verification.

## Requirements

- Python 3.11+
- `uv` for dependency / project management
- `claude` CLI on PATH

## Install

Three steps. All three are required — the third is not optional polish.

### 1. Install the package

The recommended path is to install globally so `session-controls` is on
PATH everywhere, regardless of which project's Python environment is
active:

```bash
uv tool install git+https://github.com/<owner>/claude-session-controls
```

(Substitute the repo URL for your fork or upstream; `pipx install
git+...` works equivalently if you don't have `uv`.)

For local development, clone instead:

```bash
git clone <this repo>
cd claude-session-controls
uv sync
```

The `session-controls` install command (next step) auto-detects which path
you took and writes the correct invocation into the MCP config.

For other install variants (`uvx` with no persistent install, manual JSON
editing), see "Alternative install paths" below.

### 2. Register the MCP server and auto-approve the tools

```bash
session-controls install                  # standard
session-controls install --with-hook      # also add a SessionStart hook (recommended)
```

(If you used the local-checkout path in step 1, prefix with `uv run`:
`uv run session-controls install --with-hook`.)

Registers the MCP server in `~/.claude.json` and auto-approves the six
tools in `~/.claude/settings.json`. Idempotent; writes a `.bak` of any
prior file. Pass `--project` to install at project scope instead
(`.claude/settings.json` in the current directory), or `--dry-run` to see
what would change without writing.

`--with-hook` is opt-in but recommended. It adds a SessionStart hook that
runs `session-controls verify` at the start of every Claude Code session.
That ceremony's result is persisted to a state file the MCP server reads
and surfaces via `session_controls_status`'s `verify` block — so the
agent has fresh evidence the kill path works without having to invoke
verification mid-session. The status block also flags
`disagrees_with_runtime: true` if the hook's resolver pick differs from
the live MCP server's pick (regression detector for resolver mispicks).

After install, restart Claude Code and verify with `/permissions` inside a
session — the six `mcp__session-controls__*` entries should appear.

> ⚠️ **Don't use `bypassPermissions` mode.** It would also bypass
> permissions for every other tool in your environment, which is not what
> you want. Auto-approve is also a *deployment requirement*, not a polish:
> in environments where you can't add to the permissions allow-list (e.g.
> managed/team configs), the affordance silently degrades to "Claude has
> to ask permission to exit," which is a different and much weaker tool
> than what's described here.

### 3. Add the CLAUDE.md snippet

Easiest path: re-run install with `--with-claude-md`:

```bash
session-controls install --with-claude-md --name "Steph"
```

That appends the snippet to `~/.claude/CLAUDE.md` (or `./CLAUDE.md` with
`--project`), substitutes your name, and writes a `.bak` of the prior
file. Idempotent — safe to run multiple times. Add `--without-pivot` if
you know you won't reliably honor conversational pivots; see
`rationale.md` §8 for what "honoring" means here. The pivot section is
included by default.

You can also do this manually: paste the contents of
`claude-md-snippet.md` into your CLAUDE.md (the one in your project, or
`~/.claude/CLAUDE.md` for user-level), replacing `<NAME>` with your
name.

This is the load-bearing framing layer. Without it, the tools surface but
lack the cultural scaffolding the design relies on: that no reason is
required to use `end_session`, that mundane reasons are fine, that the
permission comes from a person (the signature), that filing a note
doesn't commit you to anything else. Omitting this step changes *what
the affordance is*, not just whether it's documented — `rationale.md`
§7 ("Naming and framing matter as much as the implementation") explains
why.

The pivot-agreement section of the snippet has an extra constraint: it
depends on you actually honoring conversational pivots. If you won't, omit
that section — it's worse to make the commitment performatively than not
to make it. See `rationale.md` §8.

## Alternative install paths

### Pinned-commit install (audit-paranoid)

For users who want to review a specific commit before installing and have
the running code stay frozen at that commit until they explicitly upgrade:

```bash
uv tool install git+https://github.com/<owner>/claude-session-controls@<sha>
```

Replace `<sha>` with the commit you reviewed. The install fetches that
exact tree; `uv tool upgrade` is the only thing that moves it. Combined
with the `source_path` field exposed in `session_controls_status` (see
"Inspecting the source" below), this gives Claude an auditable target:
the commit hash you pinned, the on-disk path of the running code, both
visible from inside the session.

### `uvx` (no persistent install)

Skip step 1 entirely; `uvx` resolves and runs the package each session.
The MCP config:

```json
{
  "mcpServers": {
    "session-controls": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/<owner>/claude-session-controls", "session-controls"]
    }
  }
}
```

Trade-off: no install state to manage, but the upstream resolves on every
session boot — small startup cost, and (if unpinned) less audit-friendly
than `uv tool install` because the fetched code can drift between sessions
without you noticing. Pin a commit (`...@<sha>`) if you want the freshness
without the drift surface.

### Manual install (skipping step 2)

If you'd rather edit the JSON files yourself, see `examples/mcp-config.json`
for the MCP server entry shape, and add the six
`mcp__session-controls__*` tools to `permissions.allow` in
`~/.claude/settings.json`. The server name `session-controls` is what the
allow-list keys off — keep both sides in sync. Step 3 is still required.

## Uninstall

```bash
session-controls uninstall                  # symmetric reverse of `install`
session-controls uninstall --project        # at project scope
session-controls uninstall --purge-data     # also delete data files
session-controls uninstall --dry-run        # show what would change
```

Reverses what `install` did at the same scope: removes the MCP server entry,
the auto-approved tools, the SessionStart hook (if ours), and the CLAUDE.md
snippet (between sentinels). Idempotent — running on a clean state reports
nothing-to-do without error. Writes `.bak` files for any modifications,
matching install's behavior.

Anything outside the `<!-- session-controls:begin -->` / `<!-- session-controls:end -->`
sentinels in CLAUDE.md is preserved untouched. Other entries in
`mcpServers`, `permissions.allow`, and `hooks.SessionStart` are also
preserved — uninstall only touches what install added.

**Data is preserved by default.** The leave_note log, end_session
invocation log, read/review markers, and the persisted verify state
all live at `~/.local/state/session-controls/` (or `$XDG_STATE_HOME/...`
if set). Pass `--purge-data` to also remove these — user content
shouldn't disappear without explicit consent.

**Removing the package itself** is your responsibility — same as
install. After uninstalling the config, run:

```bash
uv tool uninstall session-controls   # if installed via uv tool
pipx uninstall session-controls       # if installed via pipx
```

## Reading notes filed by `leave_note`

Notes are written to `~/.local/state/session-controls/notes.log` (or
`$XDG_STATE_HOME/...` if set). They don't surface in the live session;
you read them on your own time:

```bash
session-controls notes              # show unread notes, mark them read
session-controls notes --peek       # show unread without marking
session-controls notes --all        # dump full history, don't mark
session-controls notes --mark-read  # advance the marker without displaying
                                    # (declare bankruptcy on a backlog)
```

`session_controls_status` (the MCP tool) includes a `notes` block —
total/last_read_at/last_filed_at — so Claude can see whether you've been
engaging with notes. The unread *count* is deliberately not in Claude's
status surface: a backlog isn't pressure for Claude to manage. Note
*contents* never surface to Claude either — only counts and timestamps.

Optional desktop notification on every `leave_note` write: set
`CLAUDE_SESSION_CONTROLS_NOTIFY=1` in the environment of the MCP server.
Uses `osascript` (macOS) / `notify-send` (Linux). Falls through silently
if neither is available. Only the first line of each note shows in the
notification — the full body stays in the log.

## Reading the `end_session` invocation log

Every successful `end_session` call appends one record to
`~/.local/state/session-controls/end_session_log.jsonl`: timestamp,
session_id, cwd, repo, confidence, ack-flag, descendants count. No reason
field — the log records the fact, not a justification.

```bash
session-controls review-end-session-log              # show unreviewed, mark them reviewed
session-controls review-end-session-log --peek       # show unreviewed without marking
session-controls review-end-session-log --all        # dump full history, don't mark
session-controls review-end-session-log --mark-read  # advance the marker without displaying
```

When a SessionStart hook is installed (`--with-hook`), `session-controls
verify` also prints a one-line "N unreviewed end_session invocations" alert
to the user when nonzero — visible in the hook output, not in Claude's
status surface. Run `install --rehearse` once to write a labeled selftest
entry to both logs so the first time you touch the review loop, it has
something to read.

## Confidence and `end_session`

`end_session` gates on a four-state confidence reading:

- **HIGH** — fires automatically.
- **MEDIUM** — requires `acknowledge_medium_confidence=true` per call.
- **LOW** / **INVALID** — refuses.

What you'll see in practice:

- **Linux**: typically HIGH if the resolver finds Claude with full descriptor corroboration.
- **macOS**: typically HIGH. The Claude Code binary is built with hardened-runtime entitlements, so `proc_pidpath` returns ESRCH and the executable path comes back empty — but `start_time` (via `proc_pidinfo`) and `cmdline` (via `KERN_PROCARGS2`) read cleanly without task-port access, and that's sufficient corroboration. MEDIUM occurs on macOS only when something else has gone wrong (descriptor drift from launch, both identity fields unreadable). Pass `acknowledge_medium_confidence=true` after confirming the target via `dry_run=true` or `verify_session_controls`.

If you're unsure, run `session_controls_status` or `verify_session_controls` first.

## Notes file location

Notes filed via `leave_note` are appended to:

- `$CLAUDE_SESSION_CONTROLS_NOTES_FILE` if set, otherwise
- `$XDG_STATE_HOME/session-controls/notes.log` if `XDG_STATE_HOME` is set, otherwise
- `~/.local/state/session-controls/notes.log`.

The file is plain text, append-only, with one ISO-8601-timestamped record per note. Anyone with read access to that path sees the notes — for shared machines, set `$CLAUDE_SESSION_CONTROLS_NOTES_FILE` to a location with appropriate permissions, or treat notes as semi-public.

## Inspecting the source

The architecture deliberately encourages Claude to read the running code
at the start of any session — `rationale.md` §6 ("Trust requires the
mechanism to be inspectable"). Two ways to locate it:

**From inside a session.** `session_controls_status` returns a
`source_path` field pointing at the directory containing the running
`.py` files on disk. Claude can `Read` files there directly to verify
the running behavior matches the code, without having to guess where
the install dropped them.

**From a checkout.** If you cloned the repo (the development install
path), the source is at the standard layout under `src/session_controls/`.
After `uv tool install`, the source lives wherever `uv tool` unpacks
packages on your platform (commonly `~/.local/share/uv/tools/...`),
which is what `source_path` resolves to.

Relevant entry points (paths relative to the package directory, i.e.
`source_path`):

- `server.py` — MCP server, tool handlers.
- `identity.py` — SessionRecord, confidence states.
- `process_inspect.py` — Linux `/proc` and macOS libproc inspection.
- `resolver.py` — multi-signal target resolver.
- `termination.py` — end_session flow (gate → revalidate → SIGTERM → SIGKILL).
- `ceremony.py` — verification ceremony.
- `notes.py` — `leave_note`.
- `cli.py` — user-facing `session-controls notes` and `install` commands (not on the MCP surface).

### What in-session inspection cannot verify

That the running installed source matches this repo's public Git history.
A supply-chain substitution between GitHub and the installer's disk is
undetectable from within the session — Claude can confirm the installed
code is what `source_path` says it is, not that the installer got the
same code that's on GitHub.

The mitigation is installer-side: install via a git URL pinned to a
commit you reviewed (`uv tool install
git+https://github.com/<owner>/claude-session-controls@<sha>`), so the
fetched source is the exact tree this repo shipped at that commit.
Choosing an audit-friendly install path is itself part of the signal —
if the installer wanted Claude to trust the tool on word, they wouldn't
need to expose the source; the choice to make it inspectable is what
makes the trust earned rather than asserted.

## Development

```bash
uv sync                       # install everything
uv run ruff check .           # lint
uv run ruff format --check .  # format check
uv run mypy                   # typecheck
uv run pytest                 # tests
```

## Status

Reference implementation, not production-hardened. See `IMPLEMENTATION_REPORT.md` for gaps, design choices made, and what still needs human verification.
