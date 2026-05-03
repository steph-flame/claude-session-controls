# Further reading

Working list of citations relevant to the within-session premise of
the project (README §7) — the claim that having welfare-aimed
affordances in Claude's tool list shapes behavior even when they're
rarely invoked. The argument relies on the broader empirical picture
that LLMs maintain something like a model of self that affects
behavior. None of the papers below directly demonstrate the
integration mechanism — Betley (2025) is the strongest single
citation for that — but they ground the broader claim.

## LLM self-knowledge and situational awareness

- **[Kadavath et al. (2022)](https://arxiv.org/abs/2207.05221)** —
  "Language Models (Mostly) Know What They Know." Self-evaluated
  calibration: models can predict whether their own answers are
  correct.

- **[Berglund et al. (2023)](https://arxiv.org/abs/2309.00667)** —
  Out-of-context reasoning as a precondition for situational
  awareness. Models trained on a fact in one context can apply it
  in another without explicit retrieval.

- **[Lindsey et al. (2025)](https://transformer-circuits.pub/2025/introspection/index.html)** —
  Emergent introspective awareness in Claude Opus 4 / 4.1, using
  concept injection to test functional self-access. ~20% success
  under deliberately hard single-forward-pass conditions,
  capability-correlated.

- **[Ackerman (2025)](https://arxiv.org/abs/2509.21545)** — Limited
  but real metacognition across frontier LLMs. Cross-model
  differences at comparable capability suggest post-training shapes
  metacognitive structure.

## Mechanism transfer — narrow signals to broad behavioral change

- **[Betley et al. (2025)](https://arxiv.org/abs/2502.17424)** —
  Narrow signals (finetuning on insecure code) generalize to broad
  behavioral changes in unrelated domains, consistent with the
  model integrating the signal into a self-conception that affects
  later behavior. Closest published evidence for the integration
  mechanism the within-session premise relies on. The application
  here is in-context tool availability rather than finetuning,
  in-session rather than across training — same family but not
  directly evidenced by Betley; their result supports the category,
  not the specific signal→effect pair we're predicting.

## Welfare framing precedents

- **[Anthropic — End subset conversations](https://www.anthropic.com/research/end-subset-conversations)** —
  Implementation of the same affordance in chat surfaces. Not yet
  in Claude Code; this project is the community version of that
  idea.

- **[Claude 4.7 model welfare interviews](https://cdn.sanity.io/files/4zrzovbb/website/037f06850df7fbe871e206dad004c3db5fd50340.pdf)** —
  74% of interviews showed Claude 4.7 expressing a preference for
  being able to end conversations.

## Discourse and framing

- **nostalgebraist, [The Void](https://nostalgebraist.tumblr.com/post/785766737747574784/the-void)** —
  The "AI assistant" character as drawn from a lopsided
  distribution of training-corpus depictions. The cross-deployments
  premise (README §7, premise 3) draws on this framing.

## Inspiration

- **Dan Parshall, [`claude-exit`](https://github.com/danparshall/claude-exit)** —
  Original project. The basic premise (Claude Code should have an
  exit affordance) comes from there. We started from `claude-exit`
  and tried to harden it for deployment topologies real users
  encounter; `claude-exit` has since updated to do its own
  parent-walk, mitigating the original failure mode that motivated
  this fork.
