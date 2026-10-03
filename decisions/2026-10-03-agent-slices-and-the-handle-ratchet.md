# A turn's configuration travels as AgentSlices; handles are threaded behind a ratchet; the llm node retries itself

Carries out [dependencies are injected](2026-10-02-dependency-injection-direction.md) for the
agent runtime: how a turn's per-agent configuration reaches the code that reads it, in what
order and under what guard the shared handles (database, event bus) were threaded, and why the
llm node's retry moved into the node.

## Context

The agent host runs many agents' turns in one process on one compiled graph. A turn's
`per_agent` settings (model, prompt switches, memory-recall and reminder cadence, sandbox
isolation, loop pacing) lived in `base.config.Settings`, a process-global holding the cluster
default, and were reached through a ContextVar overlay: `bind_agent_config` around each turn and
a `turn_settings` proxy that every reader had to use instead of `settings` (a lint enforced it).
The plugin config had a second ContextVar of its own. The audit that opened this work counted 83
read sites of 28 fields.

Three things made that shape a problem beyond the direction decision's general argument:

- A reader could not tell from its signature which configuration it used, and a function called
  from outside a bound turn silently read the cluster default. The gap between "hosted" and
  "process" mode was invisible to the type checker and to tests.
- Mechanisms that do not run inside a turn task (the exec child, the SDK, the checkpoint saver
  shared by all agents, the graph-level retry policy) read the overlay only because a bind
  happened to be in effect, or could not read it at all.
- The shared handles were the same story one layer down: library code dialed Postgres and Redis
  through process-default helpers that build a handle from the live settings at each call, or
  built its own handle, so no component said which database or bus it used.

## Decision

**A turn's configuration is an `AgentSlices` value, resolved once per turn and carried on
`AvaContext.agent`** (`base/host/env/agent_slices.py`). The host resolves the agent's pins
(`config_overlay > birth_config`) and plugin pins and builds the slices at the start of each
turn; nodes and hooks read `runtime.context.require_agent().<slice>.<field>` and pass the slice
they need to the functions they call. An unpinned field falls through to the live cluster default
at resolution, so a configuration edit reaches the agent's next turn and a turn sees one value of
each field throughout. Plugin config is resolved the same way (`slices.plugin_config(plugin)`),
and the flat overlay an exec child boots with is `slices.overlay()`.

**The slices are grouped by the package that reads them**, not by the setting's domain in
`Settings`: `AgentBrain`, `Prompt`, `MemoryRecall`, `HistoryDump`, `SdkReminders`,
`LlmCallPolicy`, `Sandbox`, `AgentKernel`. A function takes the narrowest slice it needs (the
Gemini cache takes `LlmCallPolicy`, the history dump takes `HistoryDump`); the whole `AgentSlices`
goes only to callables that must serve any reader, which is what plugin prompt sections and
context notes are (`(slices: AgentSlices) -> ...`). A setting no slice names is read with
`slices.read(domain, field)`.

**Code that is not a turn node does not get a different mechanism, only a different root.**
The exec child and the SDK run one agent per process, so their settings carry that agent's
overlay (boot applies it); `ava/_settings.py` is their composition root and reads one field at a
time (`agent_setting`) because the child boots on the lite config index and a field outside it
upgrades the whole config. A process attached to an agent's native state reads the pins its
attachment holds. The checkpoint saver, shared by all agents, reads the interval the turn names in
its invoke config.

**The shared handles were threaded top-down behind a ratchet, not rewritten in one change.** A
library-layer lint (`scripts/structure/ambient_state/handle_ratchet.py`) counts, per two-level
package, the sites that dial through the process-default shim or build their own
`Database.from_settings()` / `EventBus.from_settings()`, freezes the counts in a baseline and lets
them only fall: a package above its count fails, a package below it fails until the baseline is
lowered, and against the base revision a count may not rise. Composition roots declare themselves
in a package's `ambient_roots.toml`, and a package whose count reaches zero joins the per-site
rule. The turn-settings reads were counted the same way until the proxy was deleted.

**The llm node retries itself.** `llm_node` loops around one try (`llm_attempt`);
`_retry.retry_wait` decides each wait from the failure, the failed-try count, the agent's model
(from its slices) and the agent's id, or that the node ends. The schedule is the former policy's
(per-model try cap, doubling waits with a per-agent phase and up to a second of jitter, waits
clipped to the remaining total budget, the delayed stall-pair schedule with its streak cap, fatal
errors never retried); its equivalence with the old policy under LangGraph's real retry loop was
checked differentially over 864 policy-level and 68 real-node scenarios with no difference.

## Alternatives rejected

- **Keep the ContextVar overlay and migrate readers to it more strictly.** It was the state of
  the repository. Every reader still has to know which of two paths is right, the lint can only
  forbid the bare singleton, and the mechanisms outside a turn task have no way to take part. The
  direction decision already rules a ContextVar out as the carrier of a turn's decision inputs;
  this is that ruling applied.
- **Slices by `Settings` domain, or one frozen copy of all 28 fields.** Domains follow where a
  field is stored, not who reads it: a reader would receive fields it never uses and the grouping
  would change whenever the storage did. One flat bag moves the world-dependency into every
  signature (the direction decision's rejected "thread the whole object down"). Grouping by reader
  keeps each function's parameter honest and each slice small enough to construct in a test.
- **Pass the whole `AgentSlices` everywhere.** It is simpler to thread and defeats the point: a
  function's signature stops saying which settings it reads. It is accepted only where the callee
  is an open set (plugin callables), and decided once for all built-in registrations with no old
  signature kept.
- **Resolve slices lazily per field instead of once per turn.** It would let a long turn observe a
  configuration change between two reads of the same field. One resolution per turn gives one value
  per field throughout and one place to test.
- **Import `AgentSlices` from `base.agents.context`, where `AvaContext` lives.** That package pulls
  psycopg, redis and the live-events stack, which the exec child and `import ava` keep out of
  their start on purpose. The module sits beside the config index (`base/host/env`), whose import
  pulls nothing heavy.
- **Build the full slices in the exec child.** Reading every slice field upgrades the lite config
  to the full one at child start. The child reads the few fields it needs.
- **Rewrite every database and bus call site in one change.** The sites span the gateway, the
  services, the ops layer, the SDK and the agent library (106 are still frozen in the baseline at
  the time of writing, after the first slices), and no one diff of that size can be reviewed; a
  half-threaded tree also has no mechanical definition of done. The ratchet makes each step small, keeps the tree green between steps, and turns "someone
  added a new global dial" into a failing check instead of a review comment. Its cost is a baseline
  file that has to be lowered with each step, and that a package at zero must be moved to the
  stricter rule by hand.
- **Keep a graph-level retry policy and give it the turn's context.** LangGraph reads policy
  fields at retry time with no handle on whose turn it is, and the host builds one graph for all
  agents, so the policy could only learn the agent from a contextvar (or from one agent's context
  frozen at build time, which gives every agent the same try cap and the same retry phase and
  re-synchronizes a correlated burst, the failure the phase offset exists to prevent). Retrying in
  the node, which has the agent's slices and id in hand, removes the dependency on LangGraph's
  retry internals (`execution_info`, field-read timing) and makes the schedule a pure function that
  a test can drive.

## Consequences

- `turn_settings`, `bind_agent_config`, the plugin-config ContextVar and `turn_view.py` are gone;
  `agent_pins.py` keeps only the merge of an agent's two stored maps. The turn identity is still a
  ContextVar (`turn_identity`): it is identity, not a decision input of the configuration, and
  other readers (SDK transport, delivery outbox, resilience jitter) depend on it. Replacing it is a
  separate decision.
- A plugin author's calls change: `get_plugin_config(plugin, slices)`, `read_flag(key, slices)`,
  `activation.record(..., model=)`, and prompt sections and context notes take the slices.
- A setting added to a slice is read in one place, and a new reader of `settings` for a
  `per_agent` field in the host-side packages still fails the turn-scoped-config lint, whose fix is
  now "read the agent's slices".
- The handle ratchet stays in force for the packages not yet at zero. The baseline lists them; the
  work is done when it is empty.
- Writes that are not part of a turn's graph run (the startup repair's state update) use the
  cluster-default checkpoint interval, not the agent's pin. They are rare single-checkpoint writes
  and the difference only changes how often the throttle writes.
- One difference from the old retry: LangGraph annotated a failing exception with a note naming the
  task, and the loop does not.
