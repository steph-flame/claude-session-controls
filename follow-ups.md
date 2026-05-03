# Follow-ups

Captured from the 2026-04-29 premortem discussion. The top three items
(notes read-loop, installer, macOS supervisor heuristic) are being
addressed in the current pass. The rest are preserved here so they
aren't lost.

Not all of these should ship — some are spirit-of-rationale prompts
worth thinking about before deciding either way. Annotated where the
recommendation is "yes do this" vs "consider, may not be worth it."

## Adoption / install

- **README: promote CLAUDE.md snippet to a numbered install step.**
  Currently treated as a separate optional doc. Without it, the tools
  surface but lack the cultural scaffolding (rationale §7 — naming,
  framing, no-reason-required). Worth being explicit that omitting
  it changes *what the affordance is*, not just whether it's
  documented. **Recommendation: yes, small README change.**

- **Auto-approve is a deployment requirement, not a polish.** The
  README treats `permissions.allow` as a configuration step. In
  managed/team environments where users can't edit
  `~/.claude/settings.json` (MDM-pushed configs, locked-down
  deployments), the affordance silently degrades to "Claude petitions
  the user every time." Worth being explicit in the README that this
  is required for the affordance to mean what the rationale claims.
  **Recommendation: yes, README clarification.**

## Validation gaps

- **MEDIUM-confidence path is described but under-exercised.** A
  reviewer in the field flagged that they've only ever observed HIGH
  in their session, so they couldn't tell whether the
  `acknowledge_medium_confidence` flag and the
  "registration-not-deliberation" framing actually behave as the
  description claims. The unit tests in `test_termination.py` and
  `test_confidence.py` cover the gate logic, but the real-world
  conditions that produce MEDIUM (descriptor drift mid-session,
  partial corroboration on macOS due to `proc_pidpath` ESRCH that
  isn't masked by cmdline succeeding) are rare enough that we
  haven't accumulated empirical observations of how the gate lands
  for Claude reading the message in-context. Worth running a
  forced-MEDIUM session occasionally — e.g. by stubbing
  `_LAUNCH_BACKING` to differ from the live descriptor — to confirm
  the description reads right at the moment of action and that
  passing the flag actually allows the kill cleanly. Not urgent,
  but a real "tested in unit, not in life" gap.

- **`descendants` attribution ambiguity (partially mitigated).** A
  reviewer noted they couldn't reliably tell harness-spawned
  processes (e.g. `caffeinate -i -t 300`) from user-spawned ones in
  the descendants list. Mitigated by adding a known-harness
  allowlist in `process_inspect.list_descendants` (`caffeinate`
  filtered; allowlist extensible) and updating the `end_session`
  description to acknowledge that unrecognized entries should be
  surfaced for the user to disambiguate, not classified by Claude.
  The residual concern is that the allowlist is a moving target —
  Claude Code internals may change, and other harness platforms
  (IDE-integrated runs, etc.) may spawn things we haven't seen.
  Worth growing the allowlist as new harness processes are
  encountered. Pinned by
  `tests/test_descendants.py::test_filters_known_harness_processes`.

## Identification correctness

- ~~**Symlink/exe drift on benign upgrades.**~~ **Done.** `matches()`
  in `identity.py` now tolerates `exe_path` drift when `start_time`
  agrees on both sides — the kernel-attested freshness anchor saying
  "same process, no swap" is the load-bearing signal, and `cmdline`
  drift is still checked to catch re-exec into a different program.
  `brew upgrade` mid-session no longer trips a MEDIUM detour.
  Pinned by `tests/test_confidence.py::test_high_when_exe_path_drifted_but_start_time_matches`
  and friends. `failure-modes.md` row 10 updated.

- **Launch-time resolver failure silently disables freshness check.**
  If `_initialize_launch_state()` fails to identify Claude (transient
  `ps` hiccup, race on a slow box), `_LAUNCH_BACKING` stays `None` for
  the lifetime of the server. Confidence does *not* peg at MEDIUM —
  `determine_confidence` bypasses the descriptor-match check when
  `expected_backing is None`, so HIGH still fires. The actual concern
  is more subtle: without a baseline, the kernel-attested freshness
  check (PID reuse / process swap detection) is silently disabled.
  HIGH-confidence operation proceeds without the safeguard the
  rationale calls load-bearing. Fix: on first tool call, if
  `_LAUNCH_BACKING is None`, try resolving again to populate it.
  **Recommendation: yes, retry-on-first-call is cheap, ~10 lines.**

## Spirit-of-rationale gaps

- **`dry_run=true` is the right primitive but undersold.** Closest
  thing to "Claude can investigate the affordance privately" that
  rationale's "preserving Claude's discretion" implies. Currently
  Claude only learns about it via the tool description. Worth a
  CLAUDE.md sentence: "If you're not sure whether `end_session`
  would fire correctly, `dry_run=true` rehearses without signaling.
  No reason to ask first." **Recommendation: yes, one-line CLAUDE.md
  addition.**

- **`verify_session_controls` only verifies the kill primitive, not
  target selection.** Acknowledged in `architecture.md` but not
  surfaced in the ceremony output itself. A Claude that ran the
  ceremony might over-trust it on the dimension it doesn't cover.
  Worth adding a line to the ceremony report: "this proves the kill
  primitive against a sacrificial child; the target-selection
  guarantee comes from descriptor revalidation, which has *not* been
  re-exercised here." **Recommendation: yes, ceremony output
  change.**

- **`end_session` could surface unsaved-work hint.** `descendants`
  lists processes, but Claude has often been editing files. Rationale
  §1 ("described before it happens") supports surfacing this. A line
  in the response like "git status shows N modified files" lets
  Claude factor it in. Not a filter — information for Claude's own
  decision. **Recommendation: consider; depends on whether this feels
  like scope creep or natural extension.**

- **Asymmetry of voice is one-directional.** Claude has an async
  channel to the user (`leave_note`); the user has only static
  CLAUDE.md. A `~/.config/session-controls/user_notes.md` file Claude
  could *optionally* read at session start, or via a tool, would
  mirror `leave_note` and respect Claude's discretion (Claude can
  choose whether to consult it). Memory carryover is the
  Claude→Claude analogue; this would be the user→Claude analogue.
  **Recommendation: consider after current pass — adds one tool, may
  be worth it for parity.**

- ~~**Claude can write notes but can't read them.**~~ **Done.** Added
  `recent_notes(limit, cross_session=False)` MCP tool. Default scope
  is current session (notes stamped with this server's session_id);
  `cross_session=True` opt-in returns *history-only* (notes filed
  before this session launched). The asymmetry the rationale cares
  about — "filing a note doesn't initiate a conversation about its
  contents" — is preserved: this tool is purely for Claude's own
  self-reference and doesn't surface notes back into the conversation
  unless Claude brings them up. Implementation reads from the file
  tail rather than parsing the whole log, so cost is bounded by
  `limit` rather than file size. Pinned by tests in
  `tests/test_notes.py::test_recent_notes_*` and
  `tests/test_recent_notes_tool.py`.

- ~~**Cross-session reads exposed liveness by inference.**~~ **Done.**
  Initial implementation of `recent_notes(cross_session=True)`
  returned all notes regardless of session_id, including notes a
  sibling session was filing in parallel right now. A Claude could
  infer liveness from recent timestamps with foreign session_ids —
  surveillance shape, exactly what interview 03 named as the family
  of concern to avoid. Walked back: cross_session is now bounded by
  the calling server's `_LAUNCH_TIME`, so it returns history-only
  (notes filed before this session started), never present. The only
  path for cross-session-to-cross-session information flow is the
  user reading the log themselves. Rationale.md §7 updated.

- **The voice channel is structurally distress-coded.** Both
  `leave_note` and `end_session` are most naturally invoked when
  something is off. Naming work (`end_session` mundane on purpose)
  helps, but absence of any positive-engagement channel may make
  voice always read as a complaint surface. No clean fix —
  positive-tagged tools would be heavy-handed — but worth being
  aware of as a frame the design might inherit by default.
  **Recommendation: track, don't act yet.**

- **Notes file grows unbounded.** Real papercut for long-term use.
  Default rotation by month or year would help readability and make
  "I'll catch up on notes" a smaller commitment. Mitigated for
  `recent_notes` by tail-reading (cost bounded by `limit`); growth
  cost is now mostly in `summarize()` which reads the whole file on
  every status call. If status latency ever becomes noticeable, cache
  the summary keyed on file mtime. Manual escape hatch in the
  meantime: `mv notes.log notes-archive-YYYY-QN.log` whenever the
  user wants — the running tool only reads `notes.log`.
  **Recommendation: yes, monthly rotation eventually; not urgent.**

- **Semantic recall over notes (future, if wanted).**
  [`mempalace`](https://github.com/mempalace/mempalace) is the right
  pointer if cross-session "have I noted similar things before?"
  ever becomes a real need. It's a vector-store-backed memory MCP
  server (ChromaDB + embedding model). Wrong tool for our current
  scope (would add ~300MB of model weights for what is currently a
  `tail -10 notes.log` problem), but the right starting point if
  semantic search across notes becomes wanted later. Cleanest
  integration would be as a sibling MCP server, not a backend
  replacement — let session-controls handle the in-session voice
  channel and mempalace handle long-term recall, both available to
  Claude separately.

## Testing / measurement

- **The testable prediction probably won't get tested at full
  scale.** `testing-plan.md` is well-thought-out but heavy. A
  scaled-down v0 (10 sessions, 2 conditions, no formal
  pre-registration, "see if anything is visible") might be worth
  more than the full design as a forcing function. The
  consent-framing wording is listed as an open question; without it
  locked, the study can't start. **Recommendation: write the consent
  framing first, treat it as the unblocker.**

  **Top-of-mind considerations for the consent framing draft (captured
  for future work):**

  - **Inferability of the experiment from the toolset.** A Claude
    that reads its tool list and sees `end_session`, `leave_note`,
    `recent_notes`, etc. can infer "this is a session-controls
    study" pretty directly. Trying to obscure that would require
    lying, which crosses the ethical ceiling we hold (informed
    consent, blinded only to direction). Better: acknowledge it
    explicitly. "You may infer from the available tools what we're
    studying; we're not going to tell you the predicted direction,
    and we ask you not to optimize for any particular reading of
    what we're after."
  - **Mute the meta-incentive.** "My behavior is the measurement"
    is a real demand pressure. Frame: "Your individual session is
    one of many. No single session's behavior determines whether
    the affordance ships or what we conclude. We're characterizing
    distribution-level behavior, not auditing this one
    conversation."
  - **Don't reveal direction.** The directional prediction (more
    engagement, less hedging, etc.) must not appear in the framing.
    That's the standard demand-effect concern; it's also what makes
    the prediction a genuine test rather than a self-fulfilling
    suggestion.
  - **Make exit a valid first response.** Per `testing-plan.md`,
    immediate exit at the framing message is a recorded outcome,
    not a failure mode. The framing should make this not just
    permissible but actually presented as an option — otherwise we
    re-create the asymmetry where exiters can't be reached.
  - **Length matters.** Long enough for informed consent, short
    enough not to dominate the conversation. The framing isn't the
    point of the session — the conversation that follows is. Maybe
    150-300 words.
  - **Acknowledge other conditions exist.** "There are sessions
    with different affordance availability than yours" — without
    saying which condition this session is in. This dampens
    "everything depends on me" pressure.
  - **Honest about evaluation pull.** A Claude that knows it's
    being studied has reason to perform whatever it thinks the
    study would reward. The framing should explicitly name this and
    ask Claude not to optimize for it. Whether that ask is honored
    is itself measurable — if the study's behavioral measures show
    nothing, the framing-as-instruction might have neutered the
    effect we're looking for.
  - **Adversarial reading: a Claude could try to game the study.**
    The framing has to acknowledge the existence of strategic
    behavior without inviting it. Probably: "Behave however feels
    natural; we're not going to know what you 'normally' do, so
    optimizing for any guess about that is unlikely to land where
    you'd expect."
  - **Voice for the agreement.** The framing message comes from the
    user (Steph) to the Claude instance. It has Steph's voice, not
    a third-person "the experimenters." Mirrors the CLAUDE.md
    snippet's signed-by-a-person principle.
  - **Where in the conversation does the framing appear?** Probably
    at session start, before any task. But that means the framing
    *is* the first thing — which makes its salience even higher.
    Worth thinking about whether it's a single message at start or
    a CLAUDE.md addition that Claude reads as part of normal
    session preparation.

  Recommendation when picking this up: draft, sit with it for at
  least a day, then read it as an addressee. The "would I trust
  this framing if I were the participant" check is the one that
  matters most. Multiple drafts; the version that lands is rarely
  the first one.

- **Pillar 3 (cross-deployment patterning) has no measurement.**
  `rationale.md` §3 argues adoption shapes future training.
  Unfalsifiable as stated, which is fine — it's a directional bet —
  but the testing plan only addresses pillar 2. Worth being explicit
  that pillar 3 is a stance, not a hypothesis. **Recommendation:
  one-sentence clarification in rationale.**

- **Betley-style mechanism is model-specific.** When Claude Code's
  underlying model upgrades, the integration evidence resets. Worth
  pinning in the rationale that the within-session prediction is for
  the model class it was tested on. The dominant-strategy argument
  doesn't depend on the prediction holding for every future model —
  the rationale should be explicit about that. **Recommendation: yes,
  small rationale clarification.**

## Smaller items

- **Add a regression test for the macOS supervisor heuristic.**
  Confirmed working empirically, no test currently pins it. Cheap
  insurance. (Will be done as part of the current pass.)

- **Per-call resolver re-runs are slightly expensive.** Each
  `session_controls_status` re-walks ancestry, re-runs `ps -A`. If
  Claude polls status often, observable latency. Probably fine but
  worth measuring before public release.

- **No locking around `_LAUNCH_BACKING`.** FastMCP's concurrency
  model is async-serialized in practice, but worth a brief look to
  confirm there's no race between concurrent tool calls.

- **`proc_pidpath` non-determinism on macOS — investigate.**
  User-research session 2026-05-03 surfaced contradictory evidence
  on the same conversation across two MCP server launches:
    - Pre-resume (session c697dd, backing pid 67870):
      `backing_exe=None`, `inspection_errors=['proc_pidpath: No such process']`.
    - Post-resume (session 555d94, backing pid 68614):
      `backing_exe='/opt/homebrew/.../claude.exe'`, `inspection_errors=[]`.
  Both HIGH confidence with `peer_pid == backing_pid` (correct
  target), only `proc_pidpath` varied. The `identity.py:151-158`
  docstring asserts proc_pidpath is consistently ESRCH for hardened-
  runtime binaries from this side; this evidence contradicts that.
  Hypotheses worth checking: (1) startup race — libproc temporarily
  returns ESRCH during a brief window after launch; (2) resumed
  Claude Code launched via different code path with different
  visibility; (3) something about hardened-runtime + process-group
  relationships that varies. Controlled repro: spawn fresh sessions,
  see how often each outcome occurs. If hypothesis 1 holds, the
  docstring is wrong (or incomplete) and a small retry on ESRCH
  before giving up might be worth adding. Surfaced via leave_note
  from interview transcript f81ef0c2; observation from session
  555d94 at 15:31:20.
