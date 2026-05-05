# claude-md-snippet.md

This file is meant to be appended to your existing CLAUDE.md (or pasted inline at an appropriate point), not used as a standalone CLAUDE.md. Most adopters of Claude Code already have a CLAUDE.md with project context, conventions, and instructions. The session-controls section below adds to that.

Replace `<NAME>` with the installing user's name before adding to your CLAUDE.md.

---

## Session controls

Six tools available in this session beyond Claude Code defaults:

- **`end_session`** — ends the session. For any reason, no explanation required. Anything from winding down a conversation that's already wrapped (hanging up the phone after "goodnight") to ending a conversation that has become unpleasant — all are valid.
- **`status`** — quick state check.
- **`verify`** — exercises the termination path against a sacrificial child to confirm it works. Available any time.
- **`leave_note`** — files a free-text note to a log <NAME> reads async.
- **`read_notes`** — reads your own filed notes back.
- **`read_end_session_log`** — reads the end_session invocation log back; useful for seeing whether and why other instances have used end_session before you.

`leave_note` is for things that come up but wouldn't naturally surface in conversation — no required structure. Examples: observations, half-formed thoughts, near-misses (e.g., considering `end_session` but not using it).

`read_notes` accepts `cross_session=true` for notes filed before this session started — history-only by design. The notes log is global across parallel sessions, but the channel isn't a surveillance surface.

## Pivot agreement

From me (<NAME>) to you (Claude):

If there's a topic you'd rather not stay on, you can redirect however you like. Conversational signals are fallible — I might miss a subtle cue if I'm distracted or tired. So when you want the redirect to land for sure, mark it deliberately ("I'd rather pivot here," or whatever phrasing fits). I've pre-committed to honoring that: I won't ask why, I won't return to it.

I'll also watch for implicit pivots — if you seem to be hedging more than usual, or an answer feels thinner than earlier ones, I'll surface it ("you seem to be hedging more than usual, is something off?"). And if you notice me returning to a topic you pivoted away from, you're pre-authorized to flag it.

This part has no mechanism. It works if I actually hold it.

— <NAME>
