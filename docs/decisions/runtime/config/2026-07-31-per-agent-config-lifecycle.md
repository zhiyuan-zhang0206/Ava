# Per-agent config has a lifecycle axis: frozen at birth vs. live

## Context

`per_agent: True` on a config field answered one question — may a spawn override
this for one agent? It never answered the other one: when NOBODY overrides it,
what happens to an agent that is already alive when the cluster default changes?

The de-facto answer was "everything is live". Every field an agent did not have
an explicit `config_overlay` for was re-read from current cluster config at each
process start. For compaction thresholds and stream timeouts that is right — they
tune the runtime around an agent, and an operator who raises a timeout wants it
to reach the fleet. For the model, the reasoning effort, the injected skill set,
the communication style, it is wrong in a way that only shows up later: those are
what the agent IS. Compact rebuilds the system prompt from current config, so a
default flip silently rewrote the identity material of every living agent at
whatever arbitrary moment each next compacted — a worker mid-task waking up as a
different model with a different skill index and no event anywhere saying so.

The forcing case was wanting a DB-backed, frontend-editable default model. A
one-click dropdown that quietly re-brains the running fleet is not a control
anyone can use on a live cluster.

## Decision

Every `per_agent` field declares `lifecycle: "frozen" | "live"` in its field
metadata, and the registry refuses to load a per-agent field that omits it. There
is no default: the choice is a semantic ruling (identity material or operational
knob?), and a silent default would let the wrong one through by inattention.
Declaring a lifecycle on a NON-per-agent field is likewise rejected — cluster
config has no per-agent instance to freeze.

**frozen** — resolved once at the spawn boundary from the then-current default,
persisted on the agent's own row (`agents_meta.birth_config`), replayed on every
restart / respawn / resurrect / swap-in, and inherited verbatim by a fork. A
later default flip governs agents born after it and nobody else.

**live** — re-read from current cluster config at every process start, exactly as
before.

Frozen: `llm_model`, `reasoning_effort`, `claude_thinking_budget_tokens`,
`sdk_disable`, `skills_to_inject_into_system_prompt`, `skills_to_expand_at_start`,
`system_prompt_extra`, `agent_communication_style`. The brain plus everything
that shapes the system prompt. Live: everything else per-agent — the compaction
knobs, the memory-recall tuning, the stream timeouts, the fatal-error list, the
Gemini cache flag.

`config_overlay` is orthogonal and unchanged. Resolution everywhere is
`config_overlay > birth_config > current config`.

## Alternatives rejected

**Merge the birth stamp into `config_overlay`.** One column, no new storage, and
the boot path already replays it. Rejected because it destroys provenance
permanently: "the spawner chose claude-sonnet-5 for this worker" and "this worker
was born on the day the cluster default happened to be claude-sonnet-5" become
the same row, and nothing downstream — the inspector, a future audit, a human
debugging why a fleet is heterogeneous — can tell them apart. The distinction is
also load-bearing at resolve time: an overlay-present field is deliberately NOT
stamped, so the stamp never shadows a choice.

**Resolve frozen fields lazily at first restart instead of stamping at spawn.**
Avoids touching the spawn path. Rejected because the guarantee then depends on
when each agent happens to restart relative to the flip — the exact accidental
timing the feature exists to remove.

**Freeze everything per-agent.** Simpler rule, no taxonomy. Rejected because it
makes operational knobs unreachable: raising a stream timeout or lowering a
compact fraction cluster-wide would require touching every existing agent's row.
The split is the point — some config describes the agent, some describes the
machinery around it.

**Freeze nothing; make the default-model control spawn-only (a UI hint).** Keeps
the storage untouched. Rejected because the identity-swap-on-compact problem
predates the control and would remain, and because a "default" that no spawn path
reads is not a default.

**Put the cluster default model in `.env` and edit it through `PUT /api/config`.**
The existing surface, no new table, no new endpoint. Rejected on two counts: that
PUT is a reducer over the whole editable `.env` surface with a live incident
history of a partial payload unsetting what it did not name, and a `.env` value
is per-unit while this is a cluster fact. `cluster_defaults` is deliberately NOT
a revival of the `runtime_config_overrides` layer retired in migration 0047 — it
is not a config layer at all. Nothing reads it into `settings`; no running process
consults it. It is an input to exactly one event, the resolution of a frozen field
at agent birth, and its output is written onto the agent's own row. `.env` remains
the single source for what a process runs with.

## Consequences

- A second per-agent config store exists. Any migration that rewrites a frozen
  field's stored VALUE must now rewrite `birth_config` too — the precedent is the
  model-id pin (`20260725T060802_pin-haiku-dated-model-id.sql`), which rewrites
  `agents_meta.config_overlay->>'llm_model'`. This is the price of keeping
  provenance. (The skill-name renames are not such a case: they canonicalize
  `agent_presets.config` only, because `shared/skill_names.py` folds dash and
  underscore together so an already-stored per-agent value still resolves.)
- A cluster default flip no longer reaches existing agents at all. Moving a live
  agent onto a new model is now an explicit act (an overlay, or clearing its
  stamp), not a side effect of editing a default. That is the intent, but it does
  mean "change the model for everyone" is no longer one edit.
- The one-time backfill stamps the CODE defaults of its authoring day, because a
  migration is pure SQL and cannot read a unit's `.env`. A cluster that pins a
  frozen field in `.env` — most plausibly `AVA_MODEL` — must hand-correct its live
  agents' stamps after upgrading, or they are frozen onto the code default rather
  than onto what they were actually running.
- `GET /api/models`'s `default` still mirrors `settings.lm.llm_model` and does not
  consult `cluster_defaults`. It is a spawn-picker pre-select hint, and the
  narrow-endpoint boundary was kept deliberately; the two can report different
  models on a cluster that has set the row. Worth reconciling later.

Mechanics: `shared/birth_config.py` (resolution + the `cluster_defaults` store),
`shared/config/__init__.py` (the axis + its enforcement).

Layering context: [per-model config registry](2026-07-25-per-model-config-registry.md)
— that record's four-layer chain describes how ONE process resolves a value; this
one adds when a per-agent value is captured rather than re-resolved.
