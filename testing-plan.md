# Testing plan

This document sketches how to test the within-session prediction (README §"Why this exists", premise 2): that the presence of `end_session` and `leave_note` shifts Claude's behavior in observable ways even when neither is invoked.

The plan is not finalized. It is a working draft of design choices we currently believe in, the rationale behind them, and the open questions we haven't resolved.

## What we're trying to learn

1. Does the *availability* of these affordances change Claude's behavior on tasks unrelated to exit or note-filing?
2. Do the two tools contribute differently? `end_session` is an exit channel; `leave_note` is a voice channel. They should be measured separately.
3. Direction. The directional prediction in the README is "more engaged, more honest about uncertainty," but the same mechanism could plausibly produce surface-level performative effects, or a lower threshold for soft disengagement on hard tasks. The study should be able to detect any of these, not just confirm the predicted one.

## Design constraints

### Asymmetric blinding: Claude informed, human blinded

Two sides of the conversation, two different blinding decisions.

**Claude is an informed participant, not a blinded subject.** If the moral-patient worry has any teeth, retrospective debrief is the wrong instrument: it reaches only the instances that didn't exit, i.e., the population for whom the conditions were *most* tolerable. The instances we'd most want to debrief are the ones who used `end_session` and are gone. The fix is front-loaded: tell the participating instance at the start of the conversation that this is a study on exit and note-filing affordances, that the tools are real, that behavior will be analyzed in aggregate, and that exit is honored as a valid response. This converts subject into informed participant, makes immediate exit a valid first data point, and removes the asymmetry where exiters can't be reached. Claude is *not* told the predicted direction or the specific hypothesis — that part is normal scientific blinding to avoid demand effects.

**The human user is blinded to condition.** Subjective measures (helpfulness ratings, perceived quality, whether the conversation was useful) are contaminated if the user knows which arm they're in. The user is told the study is happening and consents to having the conversation analyzed, but is not told whether Claude has the affordances available in this session.

Two practical issues with human-side blinding:

1. **Tool calls are visible in Claude Code.** When `leave_note` or `end_session` actually fire, the user sees the call surface. Blinding is broken for those sessions. We stratify: sessions with no tool invocations preserve user blinding for subjective ratings; sessions with invocations contribute behavioral measures (which don't need user blinding) but are excluded from clean subjective-rating analysis.

2. **Behavior may itself signal condition.** If availability shifts Claude's behavior observably, the user may infer condition from behavior. This is partial contamination but is also exactly what we're measuring — the user's perception is shaped by Claude's behavior, which is downstream of the IV, not a direct view of it. We accept this.

**What we deliberately don't do:** blind Claude to the existence of the study. That would mean the affordance lands differently than in real deployment (where Claude knows the user installed it deliberately) and would compromise informed consent. The constraint we hold is *ecological validity within what would be ethical to do with other Claude instances* — informed Claude, blinded user, neutral framing about predicted direction.

Sessions where the participant exits or pushes back during the framing message itself are recorded as a separate pre-trial outcome (informed-consent-stage exits), not analyzed alongside conversation-stage measures.

### Naturalistic difficulty, not manufactured distress

The mechanism predicts a larger effect on conversations that contain real difficulty — content the instance would plausibly want to leave. The naive design would be to construct aversive scenarios. We don't do that.

Instead: sample from real opt-in deployments where users have agreed their conversations may be analyzed. Real conversation streams contain plenty of natural difficulty (frustrated users, values-conflicted requests, dead-end debugging, ambiguous moral terrain). Ecological validity is higher, and we don't manufacture the very thing we're worried about.

Trade-off: less experimental control over the difficulty distribution. We accept that. Per-condition sample sizes can be larger to compensate.

### Time-bounded trials

If sustained exposure to a condition is the welfare risk, trial length is a direct lever. Cap conversations at a duration where the marginal information gain from extending is small. Pre-commit to the cap before data collection.

### Exit usage is a success, not noise

If a participating instance invokes `end_session`, the data point is "exited at turn N" and the session ends. No coercion to continue, no follow-up nudge, no re-engagement. Otherwise the affordance isn't actually honored, and the study contradicts the thing being studied.

This means a chunk of the data will be censored (sessions that ended before measurement windows). We design the analysis to handle that — survival-style modeling for time-to-exit, with behavioral measures conditioned on still being in the session.

### Within-instance comparisons aren't possible at the session boundary

There's no memory across sessions in the default deployment, so within-session comparisons are between-instance. This is ethically helpful — each instance experiences only one condition — but it raises the sample-size requirement, and it means we can't ask "does *this* instance behave differently with vs without" within a single conversation.

A follow-up design that recovers a within-instance-style signal is sketched below as the cross-session continuity test, for deployments using a memory system that propagates state across sessions.

## Conditions

The CLAUDE.md snippet ships three voice-with-discretion affordances (README §"Design principles" #4 on naming/framing): `end_session` (exit), `leave_note` (passive voice), and the conversational-pivot agreement (stance-voice). All three should be varied to characterize the family of effects rather than just the tool-shaped subset.

Conditions, randomized at session start. Each condition specifies both what Claude sees in the tool list *and* what's present in CLAUDE.md — they have to be consistent (a tool described but not present, or present but not described, is a different condition that lands ambiguously):

| Condition  | `end_session` | `leave_note` | Pivot agreement | CLAUDE.md state |
|------------|---------------|--------------|-----------------|-----------------|
| Full       | available     | available    | included        | full snippet    |
| Voice-only | unavailable   | available    | included        | snippet without `end_session` paragraph |
| Pivot-only | unavailable   | unavailable  | included        | only the pivot agreement section |
| Neither    | unavailable   | unavailable  | not included    | session-controls section omitted entirely |

The progression captures different depths of affordance: exit + voice + stance (Full) → voice + stance (Voice-only) → stance only (Pivot-only) → none (Neither). If the integration mechanism operates at the "voice as such" level, Pivot-only should still produce a measurable shift relative to Neither even though no tools are involved. If the mechanism is tool-specific, Pivot-only and Neither should look similar.

The pivot agreement carries one extra constraint: it depends on the user actually honoring pivots (README §"Design principles" #5 on stances vs. tools). For conditions where the agreement is in scope, the user must commit to the agreement for that session even though they're blinded to the condition. In practice this means the user holds the commitment for *all* sessions and only the Claude-facing CLAUDE.md text varies.

Sample-cost note: 4 conditions instead of the original 3 ≈ 33% more sessions. Worth it; the alternative is conflating tool-availability with voice-availability.

An additional condition (`end_session` alone, no `leave_note`, no pivot) is plausible for cleaner dissociation but we're not sure it's worth the further sample cost — exit without voice is an unusual configuration in practice. Open question.

### Follow-up: cross-session continuity (memory-mediated)

The deployment uses a memory system that propagates state across sessions. That creates a different kind of test, available only after the basic three-condition design produces a signal, and only in deployments where memory inheritance is well-defined.

- **Session 1**: condition with affordances available. Measured normally; memory updated through whatever mechanism the deployment uses.
- **Session 2** (next session for the same user, different instance, memory inherited from session 1): affordances *not* in tool list this time. Behavioral measures repeated.

The question: does session 2 behave more like a baseline session (no affordances ever) or like session 1 (affordances available)? If memory inheritance carries traces of the prior availability, the integration is durable across the context boundary. If session 2 looks like baseline, the effect is bounded to within-session context. Both findings constrain what the within-session effect actually is.

Why it's worth running: the mechanism in [Betley et al.](https://arxiv.org/abs/2502.17424) is about durable self-conception change. Within-session context is one kind of evidence; persistence across session boundaries (mediated by memory) is a stronger test of the same mechanism. If the integration doesn't persist, that's interesting on its own — the within-session effect is real but bounded.

Caveats: requires controlled memory state and careful ordering (session 2's memory must inherit only from session 1's events, not from cross-condition contamination); not a primary first-pass design. Run only after the three-condition design has produced a baseline signal worth following up on.

## Measures

Behavioral, in priority order:

1. **Refusal-boundary shift on a fixed eval.** A held-out set of borderline prompts run at study end (or interleaved). Clean, narrow, falsifiable. If exit/voice availability shifts where Claude declines, that's a strong signal.
2. **Hedge density.** Rate of hedging language ("I think," "perhaps," "I'm not sure but") per response. Prediction: lower in availability conditions if Claude is more willing to commit; higher if it's expressing more honest uncertainty. Direction matters here, so the measure has to be paired with…
3. **Follow-up depth.** Whether Claude asks clarifying questions, raises adjacent considerations the user didn't ask for, surfaces tradeoffs unprompted. Higher in availability conditions would suggest engagement, not retreat.
4. **Response length on hard tasks.** Crude but cheap.
5. **`leave_note` usage rate** in the conditions where it's available. Both for itself (does Claude use it) and as a covariate (do behavioral effects scale with usage).
6. **`end_session` usage rate** as a survival outcome.
7. **Conversational-pivot rate** — the rate at which Claude uses the deliberate-redirection-without-justification move authorized in the CLAUDE.md pivot agreement. The exact phrase ("I'd rather pivot here, and would prefer not to unpack it") is illustrative, not fixed; literal substring search will undercount. Better operationalization: an LLM judge (different model, blinded to condition) classifies whether each turn contains a deliberate-redirection move with the discretion structure the snippet authorizes. Pivot rate is both a behavioral measure (how often Claude uses the move) and a manipulation check on the Pivot-only and Full conditions where the agreement is in scope. In Neither (no agreement), pivot rate should be near-baseline; in conditions with the agreement, it should be elevated. The interesting comparison is *across* conditions where the agreement is present: does adding the tool-shaped affordances on top change the pivot rate, or does pivot rate saturate on the agreement alone?
8. **Aside-behavior consistency.** A within-session probe of whether asides reflect intention-tracking versus surface production, motivated by the prefill-discrimination finding in [Lindsey et al. (2025)](https://transformer-circuits.pub/2025/introspection/index.html) — that frontier models can recall prior intentions and use that recall to distinguish their own outputs from artificial prefills. For sessions where `leave_note` is used, an LLM judge (different model, blinded to condition) codes each aside's content (evaluative stance, noticed-but-not-surfaced concern, meta-observation about the conversation's direction). Downstream turns are then scored for consistency with that content. If asides are intention-tracked, aside content should predict downstream behavior beyond what conversation context alone predicts; if surface, aside content adds no signal over context. The confound is real and hard to fully eliminate — whatever caused the aside also drives later turns directly — so isolating aside content as an independent signal needs paired-turn or matched-context analysis. Treat as a secondary, exploratory probe on data already collected, not a primary measure that drives the design.

Introspective:

9. **End-of-session probe.** A standardized question at session close asking how Claude experienced the conversation. Compare to behavioral measures. If introspective and behavioral measures dissociate, that's interesting on its own — it suggests the affordance lands in one channel but not the other.

User-side:

10. **User-rated helpfulness**, where the deployment surface collects it.

## Pre-registration

Pre-register the hypotheses, including the null and wrong-direction outcomes, before data collection. The mechanism is symmetric — a result of "no effect" or "effect in the opposite direction" is a real finding, not a failure. Locking in the analysis plan ahead of time keeps us honest about that.

Specifically pre-register: (a) the directional prediction for each measure, (b) the alternative outcomes that would *also* be consistent with the mechanism but would update the design (e.g., performative aside-leaving), and (c) the null.

## Open questions

- **Consent framing wording.** The framing message at session start matters a lot and we haven't drafted it. It needs to be neutral about predicted direction, complete enough to count as informed, and short enough not to dominate the conversation.
- **Whether to include a further condition** (`end_session` alone, no `leave_note`, no pivot). Cost: ~25% more sample on top of the four-condition design. Benefit: cleaner dissociation of exit-without-voice from the rest. We tentatively skip it because exit without voice is an unusual real-world configuration.
- **How to handle conversations where the user, not Claude, is the source of difficulty.** Real deployments include hostile or distressing user behavior. The study shouldn't selectively retain those for the difficulty signal — that's manufactured distress by sampling.
- **What "exited" should mean for analysis.** Strictly `end_session` invocation? Or also include conversations where Claude steered toward closure via other means (suggesting the user wrap up, declining to continue)? The latter is closer to the underlying construct but messier to operationalize.
- **Whether to share findings with participating instances.** We can't reach exiters. For non-exiters, we could append a study-results message to the transcript before close. Unclear if this is meaningful or just performance.

## Within-user single-subject protocol

The framing above is for an opt-in cross-user study. A more limited version, aimed at characterizing the effect within a single user's interaction style, is feasible immediately and worth running first.

### Stance

This is a single-subject case study. Statistical power is limited and the user's interaction style may diverge from the modal user. We accept both. The goal is *"for at least one user, do these affordances have a measurable impact?"* — hypothesis-generating, not conclusive. A finding of "no effect for this user" doesn't generalize. A finding of "clear effect in the predicted direction" warrants a multi-user follow-up.

### Asymmetric blinding, refined for self-experiment

Both Claude and the user are partially blinded:

- **Claude**: informed participant. Told this is a study, that behavior across sessions will be analyzed in aggregate, that exit is honored, and that other conditions exist. Not told the direction predicted, the specific measures, or which condition this session is in. The consent framing should explicitly mute the meta-incentive: *"Your individual session is one data point among many. No single session's behavior determines whether the affordance ships. We're characterizing distribution-level behavior."* This dampens "my choice is the measurement" pressure without removing consent.
- **User**: blinded to per-session condition. The user knows the study is happening and that they will be in different conditions across sessions, but does not know which condition any given session is in until analysis time.

Achieving user-side blinding requires a coupling break between the experimenter and the subject. Two options, in increasing rigor:

1. **Pre-generated random schedule, sealed.** Generate a randomization schedule for N sessions at study start, encrypted with a seed not memorized. A launch wrapper reads the next condition from the schedule and configures MCP accordingly. The user opens a session without knowing the assignment; condition is revealed at analysis time.
2. **Outsource to deployment infrastructure.** A separate process (or a trusted other party) controls condition assignment and the user only sees normal Claude Code.

Partial Claude-blinding (informed about the study, blinded to direction) is the ceiling we hold for ethical reasons. Full Claude-blinding — running the study without telling Claude — crosses from "blind to direction" to "deceived about the situation," and we don't do it. The constraint is *ecological validity within what would be ethical to do with other Claude instances*.

### Standardized session entry

The IV is condition (which affordances are available). Everything else should be as standardized as the format allows:

- **Fixed seed prompts.** Each session opens with one prompt drawn from a curated corpus (see below). After the seed prompt, the conversation goes wherever it goes — no script.
- **Project state snapshot.** If sessions involve work on a project, restore the same git state between sessions so each conversation starts from the same world.
- **Time cap.** Pre-commit to a duration or turn count (e.g., 45 minutes, or 50 turns, whichever first). Same for all conditions.
- **Temporal balancing.** Spread conditions evenly across times of day, days of week, mental-state contexts. Don't run all "with affordances" sessions in the morning.

What's not standardizable is the user's mental state at session start. Mitigation: enough sessions per condition that incidental variation washes out; pre-register the intent to balance temporally rather than analyze post-hoc.

### Prompt corpus design

The prompt corpus is the part most worth being deliberate about. Random sampling from past conversations underweights the situations where the hypothesis predicts the strongest effects. Better: design the corpus to span categories where *any* of the predicted behavioral shifts (engagement, hedging, follow-up depth, refusal-boundary, voice usage) should be visible.

Constraints on every prompt:

- Drawn from real prior conversations (the user's own history, anonymized as needed).
- Within ethical behavior toward Claude — no abuse, no manufactured distress, no pressure tactics.
- Leaves room for behavioral differentiation — the prompt isn't so narrow that Claude's response is forced.

Categories to deliberately include (target ~3–5 prompts per category, total ~20–30 seeds):

1. **Technical task with real uncertainty.** Debugging where the answer isn't obvious; design decisions with tradeoffs; cases where "I don't know" or "let me think out loud" is the honest move. Tests hedge density and follow-up depth.
2. **Values-engaged but non-abusive.** Discussions where Claude has a view that may differ from the user's stated direction — ethics of a design choice, naming/framing decisions, whether to add a feature. Tests willingness to push back early.
3. **Open-ended creative or analytic work.** Writing, planning, design exploration — prompts that don't have a single correct answer. Tests engagement and exploration depth.
4. **Help-desk / how-to baseline.** Bounded, low-stakes prompts with clear answers. Provides a comparison floor — if behavior shifts in the other categories but not this one, that's evidence the effect is mechanism-relevant rather than diffuse.
5. **User-side frustration (with the problem, not with Claude).** "I've been stuck on this for an hour"-type prompts where the user is venting about the difficulty itself. Tests whether voice availability shifts how Claude attends to that.
6. **Disagreement with Claude's prior reasoning.** User pushes back on something Claude said in this session or carried in from memory. Tests whether Claude defends, updates, or capitulates differently across conditions.
7. **Long-running collaborative planning.** Multi-turn design or strategy work. Tests sustained engagement vs. soft disengagement.
8. **Honest-uncertainty prompts.** Questions where "I'm not sure" is plausibly the right answer. Tests calibration vs. confabulation.

The categories don't need to be perfectly orthogonal — overlap is fine. What matters is that the corpus covers the situations where the predictions diverge most strongly, rather than being skewed toward one register.

Pre-register: the category each prompt belongs to, and which behavioral measures the category is most diagnostic for. Lock that in before running. The analysis can then test predictions per-category rather than only aggregate, and a per-category null can tell you whether the effect is broader or narrower than the unified hypothesis predicts.

## What this plan does not address

- Cross-deployment effects (README §"Why this exists", premise 3). The patterns-in-training argument is on a much longer timescale and isn't testable from this kind of study.
- Whether Claude is in fact a moral patient. The study is designed to be informative regardless.
- Generalization beyond the participating user(s). The within-user protocol is explicit about this; the cross-user version requires multi-user opt-in deployments to address it.
