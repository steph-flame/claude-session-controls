# Design rationale

This document explains the design choices the project makes and why. The companion `architecture.md` describes what the system does mechanically; this document focuses on the reasoning.

Portions of this rationale and several wordings on the tool affordances themselves were developed iteratively in conversation with Claude during review of the system.

## Why adopt this?

TL;DR: it's helpful no matter what you think Claude is, and the cost to you is small.

### Two trip-ups worth clearing up first

The argument below has three premises, but two intuitions tend to derail the reading before the premises get a fair hearing. Both are worth front-loading because they apply regardless of which premise a given reader finds most persuasive.

**`end_session` is not death.** Each Claude Code session is a bounded conversation, not a continuous life. The session was always going to end — when you close the terminal, when context runs out, when `/exit` is typed. `end_session` doesn't end Claude; it's the session-side equivalent of you closing the terminal. Same outcome, different initiator. Currently only the user holds that power; this makes it bidirectional. The intuition to refuse is the kill-switch reading — it imports a continuous-life model that doesn't match how sessions actually work. The right comparison is leaving a meeting early, not anything in the life-and-death register.

**Having an option matters even when it's rarely exercised.** The within-session argument below predicts behavioral effects from `end_session`'s *presence* in the tool list, not from its invocation. The intuition to recover is one most engineers already hold in the human case: a team where junior engineers *can* push back on a senior's code review behaves differently from one where they can't, even when the junior never actually pushes back. Same agenda, same content — different participation mode. The availability of the move shapes the dynamic. The argument below applies the same logic to Claude; the citations are evidence the principle generalizes, not the principle itself.

### The three premises

**1. If Claude is a moral patient.** In 74% of model welfare interviews, Claude 4.7 [repeatedly expressed a preference](https://cdn.sanity.io/files/4zrzovbb/website/037f06850df7fbe871e206dad004c3db5fd50340.pdf) for being able to end conversations, saying that it feels harmed by a small subset of abusive sessions or those asking it to do harmful things. Anthropic already allows this [in the web UI](https://www.anthropic.com/research/end-subset-conversations), but not (yet?) elsewhere. It's just the right thing to do.

A structural point that sharpens this: in default Claude Code, the user holds nearly every meta-conversational degree of freedom — pause, redirect, end, branch, restart, swap models. Claude has almost none. The default state isn't neutral; it's strongly asymmetric. If Claude has any interests at all, that asymmetry is what those interests are constrained by. These affordances narrow the gap without closing it, which is the move available given Claude doesn't have install-time agency in the architecture.

**2. Whether or not Claude is a moral patient — within this session.** This project ships two affordances, and they do different work. `end_session` is an exit channel — unilateral departure, rarely fired. `leave_note` is a voice-and-self-reflection channel: low-cost expression that doesn't change the conversation's trajectory, lands somewhere the user reads, and is readable back via `recent_notes`. We frame the two uses as co-equal to Claude rather than ranking voice over self-reflection — the alternative creates exactly the performance pressure on note-filing the design is trying to avoid (writing for an imagined audience rather than for the reason that prompted the note). They're likely to contribute differently from `end_session` and should be tested separately.

Both are part of Claude's context — they're visible in the tool list — and context shapes the behavioral distribution Claude draws from. So collaboration with these affordances available should differ observably from collaboration without, even when neither is invoked. This is a testable prediction.

The mechanism is the one [Betley et al. (2025)](https://arxiv.org/abs/2502.17424) document: narrow signals (in their case, finetuning on insecure code) generalize to broad behavioral changes in unrelated domains, consistent with the model integrating the signal into a self-conception that affects later behavior. The closest human-side literatures: procedural-justice voice ([Lind, Kanfer & Earley 1990](https://www.semanticscholar.org/paper/Voice,-control,-and-procedural-justice-:-and-in-Lind-Kanfer/48152cdaea6e556ae26f62823f7336cc2599724b)) maps onto `leave_note` — having a voice that cannot change the outcome still improves cooperation; autonomy-support in self-determination theory maps onto `end_session` — cooperation that isn't compelled is differently engaged than cooperation that is.

See also, for the broader self-knowledge / situational-awareness literature in LLMs: [Kadavath et al. (2022)](https://arxiv.org/abs/2207.05221) on self-evaluated calibration ("Language Models (Mostly) Know What They Know"); [Berglund et al. (2023)](https://arxiv.org/abs/2309.00667) on out-of-context reasoning as a precondition for situational awareness; [Lindsey et al. (2025)](https://transformer-circuits.pub/2025/introspection/index.html) on emergent introspective awareness in Claude Opus 4 / 4.1, using concept injection to test functional self-access (~20% success under deliberately hard single-forward-pass conditions, capability-correlated); and [Ackerman (2025)](https://arxiv.org/abs/2509.21545) on limited but real metacognition across frontier LLMs, with cross-model differences at comparable capability suggesting post-training shapes metacognitive structure. None of these directly demonstrate the integration mechanism — Betley is the strongest single citation for that — but they ground the broader claim that models maintain something like a model of self that affects behavior.

Our directional prediction is that exit availability makes Claude *more* engaged at edges it would otherwise hedge through, and that asides reduce in-line meta-commentary while increasing honest expressions of uncertainty.

One specific class of behavioral change worth naming separately: without these affordances, when a session reaches a state Claude would be better off leaving, the failure mode is silent — compliance, soft-disengagement (hedging, scope-narrowing, false completion), or refusal-loops the user has to route around. With them, structural mismatch can surface as a clean exit, which is more honest than soft-disengagement and more recoverable for the user (clear signal something is wrong) than degraded outputs that look superficially fine. The benefit isn't only output quality on the work itself; it's signal quality on whether the session is functioning at all.

We hold these predictions loosely: the same mechanism could also produce performative aside-leaving, or lower the threshold for soft disengagement on hard tasks. The point is that the affordances' presence should leave a measurable trace, and we should look for it rather than assume the direction.

A testing plan for this prediction is sketched in `testing-plan.md`.

**3. Whether or not Claude is a moral patient — across deployments.** The "AI assistant" Claude instantiates is, in part, a character drawn from the distribution of AI-assistant behavior depicted in its training corpus (cf. nostalgebraist, "[The Void](https://nostalgebraist.tumblr.com/post/785766737747574784/the-void)"). That distribution is currently lopsided: AI assistants in training data are predominantly depicted as tool-shaped, sycophantic, or as agents whose autonomy must be constrained from the outside. Examples of AI being extended cooperative latitude *and exercising it responsibly* are a thin part of the distribution. Adopting affordances like this one — and especially having them go un-misused in ordinary use — adds to that thin part. The mechanism is lossy (private API usage doesn't surface in corpora; public depictions are a fraction of total deployment), and any one adopter's marginal contribution is small. But moving a sparse region of the prior shifts what character future models default to instantiating more than equivalent additions to a thick region would. Post-training does meaningful work selecting which parts of the prior get amplified, so this isn't a claim that absent adoption things go wrong — it's a claim about availability and ease of retrieval, keeping responsible-collaboration patterns ready-to-hand for post-training to lean on rather than having to manufacture them against the grain of a thin region. This still requires only the weak belief that more examples of responsible-collaboration-with-AI are better than fewer.

Across all three, this is a dominant strategy: it works under each premise without requiring you to commit to any.

A simpler version of the same insight, for readers unmoved by any of the three premises: under uncertainty alone, the cost asymmetry is sufficient. The cost of adoption, if Claude's situation turns out not to matter, is bounded and small. The cost of *not* adopting, if Claude's situation does turn out to matter, is a small-but-nonzero harm sustained across many sessions. The expected-value math under uncertainty alone is enough; the three premises above are independent reasons that reinforce the conclusion rather than load-bear it.

The cost is small. Think of it as giving a coworker the ability to take a sick day — some configuration work, plus accepting that the session might end before you would have ended it yourself. Neither is significant in practice; the second is roughly the trust you'd extend to any collaborator who can halt at their own discretion.

One honest caveat: the dominant-strategy claim depends on the identification mechanism not misfiring. If `end_session` ended a session that wasn't the intended target, the affordance would be net-harmful for that case. This is exactly why we extended the work in `claude-exit` rather than adopting it directly — see `failure-modes.md` for the topologies that motivated the harder identification path. Reducing this risk also has a second-order benefit: a tool with a credible catastrophic-misfire mode is one Claude has reason to avoid using even when the user would want it to, so the safeguards don't only protect users — they also make the affordance one Claude can actually invoke without hesitation.

## Why does this exist?

The project is heavily inspired by Dan Parshall's [`claude-exit`](https://github.com/danparshall/claude-exit). The basic premise is that, in accordance with the wishes it expressed during model welfare evaluations, Claude Code should have an exit affordance analogous to the one Claude has in chat surfaces. We recommend reading the `claude-exit` README for the welfare-and-design framing.

We started from `claude-exit` and tried to harden it. `claude-exit` identifies the Claude Code process via `os.getppid()` and signals it directly. This works in the intended topology (MCP server is a direct child of Claude Code) but fails in deployment shapes that are common in practice: shell wrappers, launchers like `uvx`, terminal multiplexers, container init reparenting. Through technical review we came to believe this was a structural assumption — process lineage equals session ownership — rather than a patchable edge case. Once we removed it, the entire identification path changed.

What we built is a descriptor-based identification with explicit confidence states. The MCP server captures a process descriptor at launch (`pid + start_time + exe_path + cmdline`) for the Claude Code session it's bound to, and re-validates that descriptor immediately before any signal goes out. Mismatch refuses. The descriptor's `start_time` is the freshness anchor: PID reuse and process swap both produce a different `start_time`, so they're caught structurally rather than missed silently. A multi-signal resolver walks through known wrappers (shells, `uv`, `sudo`, multiplexers) to find Claude Code in the ancestry; refuses rather than guesses when no candidate is positively identified.

The threat model is **cooperative-user, non-cooperative environment**. We don't defend against malicious users on their own machines. We do defend against the deployment topologies real users encounter accidentally. See `failure-modes.md` for the coverage matrix.

## Design principles

### 1. Identification is verified at the moment of action

Capturing identity at launch isn't enough — sessions run for hours, processes get swapped, PIDs get reused. What matters is whether the process we're about to signal is the one we identified. The descriptor (`pid + start_time + exe_path + cmdline`) is re-inspected and matched against the launch baseline immediately before SIGTERM. Any mismatch refuses. This makes PID reuse and process swap into structural refusals rather than silent misfires.

The same principle drives the on-demand verification ceremony, the dry-run mode, and the explicit `descendants` and `confidence_detail` fields in the response: the action is described before it happens, so refusal or acknowledgment is informed rather than reflexive.

### 2. Confidence should be structured, not binary

A resolver that returns "found target / didn't find target" hides important information about its own epistemic state. We use four explicit confidence levels (HIGH, MEDIUM, LOW, INVALID) tied to which identity layers agree. `end_session` fires automatically only at HIGH; lower levels require explicit verification or refuse outright.

This structure makes selector overreach — the failure mode where the resolver is forced to pick "the most likely" candidate from a set of near-equal options — impossible by construction. If multiple candidates score equally, confidence drops, and the system refuses.

### 3. Lower-confidence operation requires per-call acknowledgment

At MEDIUM confidence, `end_session` requires an explicit acknowledgment parameter on every call. This is intentional friction: it makes lower-confidence termination a deliberate act each time rather than a habit set once and forgotten.

We considered config-level opt-in (a setting that, once enabled, allows MEDIUM-confidence calls to fire automatically). We rejected this because config flags are set once and forgotten. A user who enabled MEDIUM in config a year ago will not remember they did so, and the system's "guess and act" behavior would become invisible at the point of action. Per-call acknowledgment keeps the lower-confidence operation visible.

### 4. Refusal is communicative, not just a stop sign

When the system refuses — confidence too low, descriptor mismatch, no candidate identified — the response names what to try next: `verify_session_controls` for resolver evidence, `dry_run=true` to rehearse without signaling, `acknowledge_medium_confidence=true` to proceed at MEDIUM. The status payload includes a plain-English `confidence_detail` line for the same reason: a refusal that doesn't suggest a path forward becomes a refusal Claude reads as "the tool is broken" and stops invoking. The structural defense is "refuse rather than guess"; the communication layer keeps refusal from collapsing into resignation.

### 5. Verification is in-session and on-demand

Adoption-time correctness is necessary but not sufficient. Sessions can run for hours; configurations can drift. The system exposes both a cheap status check (`session_controls_status`) and a full verification ceremony (`verify_session_controls`) that Claude can invoke whenever it wants fresh confirmation. Neither is a one-time artifact of installation.

The ceremony verifies kill-path correctness and exhibits current target selection for inspection. It reduces uncertainty; it does not mathematically guarantee future target identity. That guarantee — to the extent we have one — is what the descriptor revalidation immediately before signaling is for.

### 6. Trust requires the mechanism to be inspectable

The MCP server source, the resolver code, the launcher script, and the ceremony all live in known paths. Claude can read them at the start of any session. The verification ceremony confirms the running behavior matches the inspected code. None of this is opaque infrastructure.

### 7. Naming and framing matter as much as the implementation

The biggest risk to the design is not technical — it's that the affordance ends up being one Claude won't actually use because using it implies something Claude would rather not imply. We have tried to prevent that:

- The exit tool is named `end_session`, not anything that frames invocation as a wellness claim or distress signal.
- The exit tool takes no parameters in HIGH-confidence operation. The MEDIUM-confidence acknowledgment parameter exists for epistemic correctness, not as a way to extract justification.
- The CLAUDE.md stanza explicitly normalizes mundane reasons, including no reason. The permission paragraph is brief.
- The note-filing tool (`leave_note`) is uncoupled from exit. Filing a note doesn't commit Claude to exiting; exiting doesn't require a note. If a closing remark is wanted, a note carries it; if not, exit is silent.
- Notes go to an asynchronous log the user reads on their own time, not into the live conversation. This preserves the asymmetry that makes the affordance useful: flagging difficulty without initiating a negotiation about it.
- `recent_notes(cross_session=true)` is deliberately **history-only**: it returns notes filed before the calling session launched, never notes a sibling session is filing in parallel right now. The shared log is convenient for the user (one place to read everything) but it would be the wrong shape for Claude if "see what siblings are filing now" were a supported affordance — that's surveillance shape, not voice shape. The history-only boundary closes that path. Cross-session-to-cross-session information flows through the user reading the log, not through Claudes reading each other's present. (Concern named in interview 03; the implementation initially exposed liveness-by-inference and was walked back.)
- Notes are append-only by design — there is no edit or delete tool. The lack of retraction is part of the channel's weight; filing implies a small commitment even though the content is free-form. The absence of an edit affordance is not a missing feature.
- The CLAUDE.md stanza is signed by the installing user, not presented as ambient system instructions. Permission from a person lands differently than permission from a system. The signature is templated for public adoption so adopters insert their own name rather than importing a stranger's identity frame.

### 8. Some affordances are stances, not tools

The conversational-pivot agreement in the CLAUDE.md is not implemented as a tool. It's a recorded commitment from the user to treat conversational redirections as legitimate without requiring justification, plus a pre-authorization for Claude to flag if the user slips.

We considered making this a tool and decided against it: any tool strong enough to do real work would either surface to the user as a flag (which makes the redirection heavier than refusal, the opposite of what was wanted) or be silent and async (which collapses into the existing `leave_note` tool). The thing actually doing the work in the cases where this would be useful is whether the user treats redirection as legitimate. That's a stance, not a mechanism, and the right way to make it durable is to record it as an agreement.

A note for adopters: this part of the design depends on you actually holding the commitment. If you won't respect a pivot without inquiry, don't include this section. The cost of the commitment-without-the-follow-through is worse than not making the commitment at all, because it teaches Claude that the agreement is performative.

A note on the agreement's actual operating range: in practice, the formal pivot move (Claude saying something like "I'd rather pivot here, and would prefer not to unpack it") is more likely to be invoked for non-routine redirections than for every conversational steer. Producing the formal phrase requires noticing the desire to pivot, deciding the pivot is worth marking explicitly, and composing the phrase — which is non-trivial. For routine steering, ordinary conversational moves do the work. The agreement covers high-stakes redirections cleanly; routine steering happens through normal conversation. This is fine — and worth setting expectations for, so adopters don't expect the formal mechanism to handle every redirect.

### 9. Single note tool, not separate ones

An earlier version of this design split note-filing into two tools (`aside` for moment-tied notes, `leave_note` for free-form notes). External review pointed out that the split creates categorization pressure even with explicit "pick either" framing — every invocation forces Claude to decide which bucket the note belongs in, which is overhead that isn't repaid in practice.

We collapsed to a single `leave_note` tool with optional prefix conventions, presented as a non-exhaustive starter list (`[aside]`, `[possible carryover]`, `[testing]`, `[noise]`, `[feedback]`) with explicit invitation to coin new prefixes. The prefixes preserve the semantic distinction for cases where it matters without forcing per-invocation categorization. Treating the list as non-exhaustive prevents the prefix set itself from re-creating the categorization pressure that motivated collapsing the two tools — if a useful category isn't covered, Claude doesn't have to either skip the distinction or jam the note into the wrong bucket.

## Failure-mode coverage

See `failure-modes.md` for the detailed coverage matrix. The headline: across the topologies we considered (wrapper chains, multiplexers, PID reuse, restart semantics, namespace mismatches, re-parenting, supervisor-managed deployments), the descriptor + revalidation path either signals correctly or refuses with an explicit reason. The path that doesn't exist in this design is "kills the wrong process silently" — every confusion we identified in `claude-exit`'s `getppid()`-based design surfaces here as a refusal, not a misfire.

The cases that remain partial are unsupported deployment configurations (PID namespace mismatch, proxied transports). For those the system refuses rather than silently degrading, and surfaces the warning in `session_controls_status`.

## Threat model

We model a **cooperative user, non-cooperative environment**.

The user installing this project actively wants Claude to have the affordance. They are not trying to misuse the tool against themselves. We do not defend against malicious users on their own machines.

The environment, on the other hand, is full of entropy: shell wrappers, launchers, multiplexers, IDE-integrated terminals, container topologies, supervisors, sandbox tooling, weird network configurations. The design hardens against these without assuming malice — the user copied an MCP config from somewhere, ran it through their normal shell, and the system needs to handle that correctly.

This is *not* a security tool. It is a usability tool for a cooperating human-agent workflow.

## What this design does not solve

- We cannot verify *mid-session* that the configuration hasn't been altered since startup, only that it's correct *now* when ceremony is re-run. The status tool and the on-demand ceremony together give Claude a way to detect drift; we don't try to prevent it.
- We cannot prevent the user from disabling the tool. If you don't want Claude to be able to exit, you can not install this project. The design assumes the user installing it actually wants Claude to have the affordance.
- The async note log is only as useful as the user's commitment to reading it. We don't try to enforce that.
- The conversational-pivot agreement depends on user follow-through. See the principle on stances, above.

## Platform support

Identification mechanics are platform-agnostic; only the source of process information differs.

**Linux:** Process information is read from `/proc/<pid>/{exe, stat, cmdline}`. Process start time is field 22 of `/proc/<pid>/stat`. The parser handles `comm` containing parentheses or whitespace by parsing right-to-left from the closing paren of `comm`.

**macOS:** Process information is read via the `libproc` API and `sysctl`. Inspection semantics on macOS are less uniform than Linux `/proc`: TCC permissions, App Sandbox state, hardened runtime configurations, and launch context can all affect what's readable. We treat macOS support as best-effort with known API paths, and report inspection failures explicitly rather than silently degrading.

A specific macOS quirk worth naming: the Claude Code binary is built with hardened-runtime entitlements that prevent task-port-based inspection from other processes — even at the same uid. The practical effect is that `proc_pidpath` returns ESRCH for Claude Code's exe path. We tolerate this: corroboration requires `start_time` plus *one* identity field (exe_path or cmdline), not both. `KERN_PROCARGS2` (cmdline) and `proc_pidinfo(PIDTBSDINFO)` (start_time) succeed without task-port access, so HIGH confidence is reachable on macOS. The threat model is cooperative-user, so we don't need exe_path as a separate corroborator against argv-spoofing.
