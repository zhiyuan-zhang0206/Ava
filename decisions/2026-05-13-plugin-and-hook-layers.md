# Plugin and hook two-layer extension model

## Context

Ava's kernel is a small self-cycling LangGraph (`claim → dispatch → llm/exec → end_turn → claim`)
running forever inside one agent process. Extending it without bloating the core requires a
plugin surface that answers four questions at once:

1. **Where can a plugin intervene** in the agent loop, and with what power (observe / mutate
   state / short-circuit control flow)?
2. **How does a plugin carry its own runtime state and boot-time config** without each plugin
   inventing its own persistence and namespacing?
3. **What does the agent see** — the SDK surface the model reads must equal the import paths it
   writes; a separate "virtual" view splits the two.
4. **What stays out** — every extension point is permanent surface area that contradicts the
   "small core, strippable" principle, so the denominator must be defended.

Three industry harnesses bound the design space: LangChain `AgentMiddleware` (8 hooks, deep
wrap-stack), OpenClaw plugin hooks (34 hooks, broad pub/sub + 63 register surfaces), and Claude
Code hooks (29 hooks, out-of-process shell + exit code). They disagree on nearly every axis —
process model, hook count, decision-vs-observation encoding, short-circuit semantics, mutation
semantics, manifest. The decision is which patterns to copy and which to refuse.

## Decision

**Two extension layers, two concepts the plugin author learns: `wrap` and `hook`.**

- **`wrap`** wraps an SDK function — `(request, handler)` signature, `handler` is a stateless
  thunk callable repeatedly (retry / cache / cost-cap / fallback). Not calling `handler` is the
  short-circuit. Requests are frozen dataclasses mutated via `.override(**)` (immutable pattern).
- **`hook`** fires at agent-loop boundaries (`pre_llm` / `pre_exec` / `post_exec` / `pre_compact`
  / lifecycle …). Node-style: returns a dict merged into graph state through a reducer, or `None`
  for pure observation. Decision-vs-observation is encoded in the return type, not left to
  convention.

Internal terms (graph edges, layer A/B, LangGraph nodes) are never exposed — the author sees only
"wrap an SDK function" and "hook a loop boundary".

**A plugin carries state and config through one symmetric mechanism: declare a Pydantic
`BaseModel` subclass and register it.** The framework handles prefix isolation, persistence, and
the fallback chain. One set of rules spans both:

| | State (runtime mutable) | Config (boot snapshot) |
|---|---|---|
| Declare | `register_plugin_state(FooState)` | `register_plugin_config(FooConfig)` |
| Read | `ava.state.foo.x` (auto de-prefixed view) | `ava._settings.plugins.foo.x` |
| Write | hook returns `{"foo__x": v}` through the reducer | not writable (frozen) |
| Persist | graph checkpoint | complete on-disk image, not an overlay |
| Default | `BaseModel` field default | plugin's `default_config.py`, same directory |

State field-name routing: a name already in the base agent state shares that field and its reducer
(type must match, else fail-fast); a private name auto-prefixes `<plugin>__<field>`; two plugins
colliding on a private name fail-fast and must negotiate a rename. Config is image-based: disk
holds a complete snapshot (no default-plus-override fallback chain), schema drift between disk and
the declared class raises and is reconciled by an explicit `ava plugins update`, never an implicit
boot-time auto-merge. Per-agent overrides travel as a CLI argv overlay restricted to fields marked
`per_agent=True` — not a DB column — so the event trail (spawn/restart inbound) captures them for
free and restart-drift becomes a feature, not a bug.

**The help surface is container-vs-element, and the import path is the help view.** Containers
(modules, classes) render their whole docstring as a header plus one-line child summaries;
elements (functions) render their full body. No virtual grouping: what the agent imports is what
help shows.

## Alternatives rejected

**Out-of-process shell hooks (Claude Code model).** Each hook forks a shell, communicating by
stdin JSON / stdout JSON / exit code. Simple and language-agnostic, but it has no shared state
(every fork is isolated) and pays process-spawn cost per boundary. Ava's kernel is in-process
Python; state extension via a typed graph-state union is free and typed. A shell escape hatch was
considered as an *addition* (convenient for trivial formatter/log hooks) and deferred — start
in-process only, add when a real need appears.

**Broad pub/sub with 34+ hooks and 63 register surfaces (OpenClaw scale).** Maximally expressive
but it inflates permanent surface area against the strippable-core principle. Several specific
OpenClaw hooks were rejected on principle, not budget:
- `before_agent_finalize` *revise* mode (re-run the model on a "bad" answer) and `before_agent_reply`
  (replace the model's reply) — both contradict fail-fast: let it blow up, feed the error back,
  let the model fix itself. The plugin does not get to launder or substitute model output.
- `inbound_claim` short-circuit reply (plugin emits a synthetic answer) — the model replies for
  itself; a plugin may not take over the reply.
- `subagent_delivery_target` (plugin rewrites routing) — the inbound-messages table *is* the
  routing layer; a plugin doesn't redirect it.
- `tool_result_persist` / `before_message_write` — the checkpoint is a low-level mechanism, not
  plugin-facing surface.
- `before_install` (install scan) — plugins are first-party and uv-managed; no untrusted install
  step exists.

One OpenClaw decision was explicitly inverted: its manifest does **not** declare the hook set
(hooks attach dynamically via `api.on(...)`), so the hook set isn't statically visible, can't be
selectively disabled, and tooling can't find callers. Ava declares the hook set statically in the
manifest — IDE- and tooling-visible, no dynamic `on()` attach.

**Deep wrap-only stack with 8 hooks (LangChain model).** Elegant for control flow (retry/cache via
nested `wrap_model_call`), but its single "whole agent run" boundary (`before_agent`/`after_agent`)
doesn't fit a forever-cycling graph — Ava has exactly one run (the process lifetime) and instead
needs a *per-inbound-cycle* boundary. So both styles are kept: wrap for control flow + retry,
node-style hooks for prompt/state mutation and observation — the same observation-vs-control split
LangChain draws between node-style and wrap-style APIs, with the immutable-request `.override()`
and the typed-state-union patterns adopted directly.

**Virtual grouping in help.** Collapsing the top-level namespace into synthetic groups (e.g.
`ava.action.*`) to stay under an arbitrary limit was rejected: it desyncs the help view the agent
*learns* from the import paths it *writes*. If the top level ever genuinely overflows, the fix is a
real import-path refactor, not a display alias.

**Per-agent overlay in the DB.** Storing overlays in a database column was rejected because a
stored overlay can't be tied to the plugin version it targeted (snapshot drift), whereas a CLI argv
overlay lands in the process startup log and the existing spawn/restart event trail with no extra
machinery.

**A new event role for restart.** Adding a dedicated event type for restart was rejected as
redundant — agent metadata (status/pid/started_at) plus the `restart` / `restart_completed` inbound
kinds (the latter carrying the effective-config snapshot) already make the trail complete.

**Hot reload of config.** Rejected: all settings are `restart_required`, one snapshot per boot.
Changing config means changing disk (affects the next spawn/restart) or calling restart with an
overlay (applies on the next boot). Fail-fast over a hidden live-mutation path.

## Consequences

- **One mental model, two register calls.** A plugin author learns `wrap`/`hook` and the
  `BaseModel`-declare-and-register pattern once; state and config behave identically. Framework
  owns isolation, persistence, and the fallback chain.
- **The hook denominator is defended.** A mid-sized hook set (loop-node boundaries + compact +
  cross-graph lifecycle, between LangChain's 8 and OpenClaw's 34) covers the real plugin needs
  (retry budget, syntax self-rescue, dangerous-API gating) without exceeding the graph's natural
  boundaries. Surface beyond that is refused by default, justified per hook.
- **Fail-fast is structural, not advisory.** No plugin can revise, replace, or synthesize model
  output, or reroute messages; the only blocking power (`pre_exec` block) is reserved for genuine
  safety gating. Errors propagate to the model rather than being absorbed by middleware.
- **Image-based config trades convenience for legibility.** No silent default-plus-override
  chain; disk holds a full snapshot and schema drift is an explicit, surfaced reconcile step.
  Restart-drift (changed disk, not-yet-restarted agent runs the old snapshot) is an accepted,
  intended behavior.
- **Import path equals help view.** The agent never learns a namespace shape it can't type. The
  cost is no virtual grouping escape hatch — relieving top-level pressure requires a real refactor.
- **Typed surface, with a type-inference cost.** Pydantic models give IDE completion for framework
  and plugin config alike, but a dynamically concatenated `ava._settings.plugins.<name>` may need
  generated stubs or a per-plugin type-only export for full static inference.
