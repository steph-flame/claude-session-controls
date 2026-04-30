# Architecture

This document describes what the system does technically. The companion `rationale.md` describes why we made the design choices we did; this document focuses on the mechanics.

## Identity model

The system identifies the Claude Code session it is bound to using two layers of evidence:

1. **Transport identity** — the live MCP stdio connection. Stdio is the only supported transport, which gives us a kernel-attested parent-child relationship (the MCP server is spawned as a child of Claude Code). A live transport is itself evidence that the session is current.
2. **Process identity** — `(pid, start_time, exe_path, cmdline)`. Captured at server launch and re-validated on every tool call. `start_time` is the kernel-attested freshness anchor: if the PID is reused, `start_time` differs.

Confidence comes from how strongly the two layers agree, plus a stability check between the launch-time descriptor and the per-call descriptor.

There is no session token, no V1/V2 mode distinction, no cryptographic binding. The threat model is cooperative-user — a motivated adversary running both Claude Code and the MCP server can lie about anything, and we don't try to defend against that. What we do defend against is *accidental* misidentification: wrappers between Claude and the server, PID reuse, process swap, namespace mismatches, and re-parenting.

## Confidence states

| Level    | Meaning |
|----------|------|
| HIGH     | Backing process identified, fully corroborated (`start_time` + `exe_path` + `cmdline` all readable, no inspection errors), and matches the launch-time baseline. `end_session` fires automatically. |
| MEDIUM   | Backing identified but corroboration is partial (e.g. `proc_pidpath` returned an error on macOS) *or* the descriptor has drifted from the launch baseline. `end_session` requires `acknowledge_medium_confidence=true` per call. |
| LOW      | No backing identified (resolver refused). `end_session` refuses. |
| INVALID  | No live transport, or a blocking warning fired (currently `namespace_mismatch`). `end_session` refuses regardless of acknowledgment. |

We chose per-call acknowledgment over config-level opt-in because config flags are set once and forgotten. A user who enables MEDIUM in config a year ago will not remember they did so, and "guess and act" will become the default. Per-call acknowledgment keeps the lower-confidence operation visible at the point of action.

`session_controls_status` reports the current confidence cheaply, alongside a plain-English `confidence_detail` line that explains the state and what to do next.

## Startup flow

When the MCP server starts:

1. Capture our parent PID via `os.getppid()` as the launch-time peer.
2. Run the resolver against that peer to walk through wrappers and find Claude Code.
3. If the resolver returns a chosen PID, inspect it and store the descriptor as `_LAUNCH_BACKING`. This is the baseline that per-call resolution is compared against.
4. Begin serving MCP tool calls.

Per-call (`_build_record`):

1. Re-read `os.getppid()`. If it's `1` (init/launchd) or unreachable, transport is dead → INVALID.
2. Run `detect_environment_warnings` to flag namespace mismatch or auto-restart supervisors.
3. Re-run the resolver. Inspect the chosen PID for a fresh descriptor.
4. Run `determine_confidence` against (live descriptor, launch-time descriptor, transport liveness, warnings) → confidence.
5. Enumerate descendants of the backing PID (excluding our own subtree) for surfacing in the response.

## `end_session` flow

The flow is a single sequential pipeline:

1. **Confidence gate.** Refuse INVALID/LOW; require acknowledgment for MEDIUM.
2. **Descriptor revalidation.** Re-inspect the backing PID. If the descriptor doesn't match (PID alive but `start_time` / `exe_path` / `cmdline` differs), refuse. This is what closes the PID-reuse race and process-swap window.
3. **SIGTERM.** Wait up to 3 seconds for exit.
4. **SIGKILL** if SIGTERM didn't take. Wait up to 1 second.

Two distinct success conditions are reported:
- `sent_signals` — what we actually sent (`["SIGTERM"]`, `["SIGTERM", "SIGKILL"]`, or `[]` if the target was already gone).
- `exited` — whether the target is actually dead.

The MCP server itself dies as a side-effect: when Claude Code exits, the stdio peer goes away and the FastMCP loop reads EOF.

### What we don't do: graceful close via FastMCP

An earlier iteration of the design called for a "Phase 2 graceful close" that would tear down the MCP transport before the OS signal, in case Claude Code exits cleanly on stdio EOF. **FastMCP doesn't expose a public API to close the transport from inside a tool handler**, and the alternatives (reaching into private SDK internals, or replacing FastMCP with the lower-level `Server` class) added complexity without changing outcomes — Claude Code's stdio peer dies as a side-effect of the OS signal anyway. We removed the dead phase rather than carry the parameter forward.

## Resolver (target identification)

The resolver gathers candidates from two sources:

- **Spawn ancestry walk** from our own PID upward. Known shells/launchers (`bash`, `sh`, `zsh`, `uvx`, `uv`, `sudo`, `pyenv`, `direnv`, `tmux`, `screen`, etc.) are traversed but not treated as candidates; non-wrapper ancestors with a Claude-hint match (`claude` substring in argv or exe basename) score highly; non-wrapper ancestors without a hint score low.
- **Stdio peer** (`os.getppid()`). Kernel-attested parent. Same skip-list applies — if our peer is `uv`, we don't credit it as a target.

Candidates are scored on multiple signals. The resolver returns a chosen PID only when:

1. **Positive Claude identification.** At least one candidate must have a Claude-hint match. Without a hint anywhere, we refuse rather than pick by elimination.
2. **Absolute threshold.** The winning candidate's score must be ≥ 2.
3. **Margin.** The winner must beat the runner-up by ≥ 1.

Otherwise the resolver returns no chosen PID and `end_session` refuses (LOW confidence).

## Process inspection

**Linux:** Reads `/proc/<pid>/{exe, stat, cmdline}`. The `/proc/<pid>/stat` parser is right-to-left from the closing paren of `comm` — the field is wrapped in parens but may itself contain parens or whitespace, so a left-to-right split is wrong. Process start time is field 22 of stat, in clock ticks since boot.

**macOS:** Uses `proc_pidpath` for executable path, `proc_pidinfo` with `PROC_PIDTBSDINFO` for start time and parent PID, and `sysctl` with `KERN_PROCARGS2` for argv. Inspection permissions can vary by launch context (Spotlight vs. Terminal vs. IDE); the system reports inspection failures via `ProcessDescriptor.inspection_errors` rather than silently degrading. The Claude Code binary specifically is built with hardened-runtime entitlements that block task-port access — `proc_pidpath` returns ESRCH for it even from the same uid. `KERN_PROCARGS2` and `proc_pidinfo(PIDTBSDINFO)` work regardless. Corroboration tolerates this: `fully_corroborated()` requires `start_time` plus at least one of `exe_path` or `cmdline`, so HIGH confidence is reachable on macOS even with `exe_path` empty.

Zombie detection: `is_alive` checks process state on both platforms (`/proc/<pid>/stat` field 3 == "Z" on Linux, `pbi_status == SZOMB` from `PROC_PIDTBSDINFO` on macOS), so a zombie PID that still exists in the kernel doesn't read as alive.

## Descendant enumeration

For surfacing in `session_controls_status` and `end_session`, we list all transitive descendants of the backing process minus our own subtree. Implementation: `ps -A -o pid=,ppid=` (cross-platform), build a parent→children map, walk from the target excluding the server's subtree.

Descendants are typically:
- Sibling MCP servers (filesystem, github, etc.) — die naturally on stdio EOF when Claude Code exits.
- `run_in_background` jobs — may survive Claude Code, depending on Claude Code's shutdown behavior.
- Task sub-agents — child processes of Claude Code.

The list is informational. It is **not** a refusal trigger: most descendants will die naturally with Claude Code, and refusing on their presence would block `end_session` in nearly every real session (sibling MCP servers are always present). Surfacing them lets Claude mention user-spawned long-running tasks before exit.

## Detection of unsupported configurations

The system surfaces or refuses on:

- **PID namespace mismatch** (Linux): `/proc/self/ns/pid` ≠ `/proc/<peer>/ns/pid`. Emitted as `namespace_mismatch` warning, collapses to INVALID.
- **Auto-restart supervisor**: walks ancestry looking for `launchd`/`systemd`/`pm2`/`nodemon`/`supervisord`. Emits `auto_restart_supervisor` warning. **Advisory only** — the warning surfaces in status, but `end_session` doesn't refuse on it. Killing Claude under a supervisor will kill the current session correctly; the supervisor may respawn it, which is the user's deployment choice.
- **Multiple equal candidates in resolver**: refused with reason "multiple equal candidates within margin".

We do not detect proxied transport. Under stdio, "proxied" would mean a relay process between Claude Code and us, and the only signal we could think of — "our parent's exe doesn't look like claude/shell/known-launcher" — overlaps with the resolver's existing wrapper-walking logic. In a proxied topology, the resolver simply finds no Claude-hint candidate beyond the relay and refuses on LOW confidence. The honest framing is: we lack a positive proxied-transport detector, but the LOW-confidence refusal covers the common shape.

## Verification ceremony

`verify_session_controls` runs:

1. **Discovery exhibition.** Resolver outputs all candidates, their evidence chains, and the descriptor it would target.
2. **Status report.** Current confidence and any environmental warnings.
3. **Sacrificial validation.** Spawns `/bin/sh -c 'while true; do sleep 60; done'`, captures its descriptor, then exercises the same revalidation + signal path that `end_session` uses against it. The sacrificial PID is captured directly from `Popen.pid` and never leaves the server — Claude has no way to pass an arbitrary PID into the kill path.

The ceremony verifies the kill primitive works against an observable target. It does not prove "killing Claude Code ends *this conversation* from the user's perspective" — no in-process test can. What it does prove is descriptor capture, revalidation, signal delivery, and exit detection.

## What this design does not do

- **Cryptographic binding.** No signed token, no nonce exchange. Cooperative-user threat model.
- **Sub-agent termination.** We surface descendants but don't kill them. Killing Claude Code's process tree would expand blast radius; the cleaner path is to let Claude Code's own shutdown handler clean up, or rely on stdio EOF for sibling MCP servers.
- **Process-group termination.** Same reasoning — narrower primitive over broader one.
- **Non-stdio transports.** No HTTP, no SSE, no Unix sockets. Stdio is the only kernel-attested option for "your parent really is Claude Code," and that attestation is doing load-bearing work.
- **Detection of every adversarial topology.** A motivated attacker who controls the launch environment can defeat the resolver. The defenses are tuned for accident, not adversary.
