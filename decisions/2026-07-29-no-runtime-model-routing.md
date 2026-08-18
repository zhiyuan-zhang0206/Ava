# No runtime model routing/fallback as a mistake-shim — the agent chooses its model at spawn

## Context

Ava supports 8 model providers behind one factory and one per-model
fact/tuning registry (`shared/lm/registry.py:MODELS` — deepseek, claude,
gemini, gpt, mimo, kimi, glm, grok), each with its own pricing, context
window, effort vocabulary, and per-model tuning defaults
(`decisions/2026-07-25-per-model-config-registry.md`). Having that roster
raises the natural next question: should the framework itself decide, turn by
turn, which model handles a given request — routing cheap turns to a cheap
model, or silently falling back to a second provider on an error or overload?

Today's positioning is single-user: one operator is present, can see an error,
and can swap config by hand. That positioning is exactly what this decision is
scoped to — see "Scope" below for why the verdict is not permanent.

## Decision

No, not for today's single-user deployment. Model choice is made once, at
spawn (or restart), by whoever is deciding the agent's job — never mid-run,
and never by an opaque framework heuristic that silently swaps the model to
paper over an error or chase a cost/load signal. The mechanisms for making
that choice are already first-class and are the intended surface:

- `ava.agents.spawn(config_overlay={"llm_model": "..."})` — a per-agent
  overlay (`llm_model` is `per_agent`-overridable, `shared/config/lm.py`).
- Saved `preset`s (`ava/agents/presets.py`) — named config templates a spawn
  can start from.
- The `ava_guide/models` skill (`ava_builtins/skills/ava_guide/models/SKILL.md`)
  — the judgment layer the registry itself cannot carry: which tier
  (`deepseek-v4-pro` for orchestration/judgment, `deepseek-v4-flash` for
  high-volume mechanical work) a given sub-task deserves.

`llm_model` is fixed for the agent's lifetime once resolved
(`restart_required="agent"`) — nothing swaps the model underneath a running
agent.

## Scope — a positioning-gated non-goal, not a permanent one

This decision rejects routing/fallback specifically as an **error-recovery
shim** or an **opaque/random cost-or-load router** for a single operator who
can just swap config by hand. It does **not** reject provider fallback as an
**availability mechanism** for a deployment with no operator present to swap
on an outage — that is a different problem with a different verdict, already
recorded in
[`future/roadmap/open-source-prerequisites.md`](../future/roadmap/open-source-prerequisites.md)
("Provider fallback chain") as a prerequisite that unlocks at open-source /
multi-tenant scale, not something this entry forecloses. This entry and
`conventions/non-goals.md`'s corresponding bullet apply to today's
single-user positioning; when that positioning changes, the roadmap entry
governs, not this one.

Multi-model *support* was never in question either way: the registry backs 8
providers side by side regardless of this decision. "Single model" language
elsewhere in the docs (e.g. the roadmap entry's "Today: single model,
fail-fast") describes today's default *operating* choice — one provider live
per deployment — not a registry limit.

## Alternatives rejected

- **A framework-level fallback chain (provider A fails → silently retry on
  provider B) as today's default.** Rejected for the same reason plugin-layer
  model-retry-budget designs are rejected in `conventions/non-goals.md`'s
  first item: fail-fast with a strong model beats framework shimming when an
  operator is present. A 429/5xx already surfaces through the agent's own
  retry/backoff on the *same* model/provider (`shared/lm/registry.py`
  per-model `llm_retry_max_attempts` and `llm_stream_ttft_timeout_seconds`,
  each tuned to that provider's documented failure modes — e.g. DeepSeek's
  10-minute keep-alive queue, Kimi K3's overloaded-engine silence). A silent
  cross-provider swap on top of that would hide exactly the signal an
  operator needs to see. (This is the case the Scope section above carves
  back out once no operator is present — see the roadmap entry.)
- **Cost-based automatic downgrading (route to a cheaper model under budget
  pressure).** Rejected: this is what the `ava_guide/models` skill's
  pro/flash tiering already achieves, deliberately, at spawn — a flash worker
  fleet under a pro orchestrator (the skill's documented shape) rather than
  one model whose identity silently drifts based on a runtime budget counter.
- **Mid-run model swap to chase a cheaper price on long turns.** Rejected on a
  mechanical ground, not just a philosophical one: every provider's cache
  (DeepSeek's server-side auto cache today; Anthropic `cache_control`, still a
  non-goal per `conventions/non-goals.md` until the gateway model
  switches to Anthropic) is keyed to a stable system-prompt + tool-schema
  prefix on one model. Swapping models mid-run invalidates that prefix on
  every switch, so a router built to save money would spend the savings right
  back on cache misses. This mechanical cost applies regardless of the
  single-user/multi-tenant scope split above — it is why even the future
  provider fallback chain is an availability mechanism (triggered by an
  outage) rather than a routine mid-run optimization.

## Consequences

- Multi-provider support stays a first-class, static-per-agent capability —
  a cluster with multiple provider keys configured runs agents on any of them
  side by side, and the whole roster's per-model tuning
  (`decisions/2026-07-25-per-model-config-registry.md`) keeps working
  unchanged.
- What is foreclosed **today** is a runtime dispatcher choosing FOR the agent
  inside a turn boundary as a mistake-shim or an opaque router — there is no
  `ava.lm.route(...)` and no gateway-side model selector to build toward under
  today's positioning.
- If a genuine need for faster model selection appears under today's
  positioning, the extension point is a sharper `ava_guide/models` judgment or
  a spawn-time helper — not a hidden factory/gateway router. Trigger for
  revisiting *that*: a real workflow needs the tier choice made faster than an
  agent can reason about it once at spawn; even then the choice should stay
  explicit and inspectable, made at a spawn boundary, not injected silently
  mid-turn.
- Separately, the open-source/multi-tenant provider-fallback-chain work in
  `future/roadmap/open-source-prerequisites.md` is unaffected by this
  entry and is not gated on revisiting it.
