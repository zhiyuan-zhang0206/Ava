# Plugins declare; the framework registers

## Context

A plugin used to extend the agent by running code at import: `register_system_prompt_section`,
`register_context_note`, `register_before_llm`, `register_plugin_state`, `ava.register_namespace`
and the rest each mutated a process-global list or table the moment `plugin.py` or `agent_runtime.py`
was imported. Three things held that together:

- **Attribution by ContextVar.** The loader opened `PluginContext(name)` around the import so each
  `register_*` call could stamp its plugin onto the attribution ledger. A registration made outside
  that context was silently unattributed.
- **Reload by mutation.** `clear_plugin_registrations` truncated every registry back to its framework
  prefix (`_FRAMEWORK_SECTION_COUNT`, `_FRAMEWORK_NOTE_COUNT`) and the next load appended again. Any
  registry added without joining that function leaked across reloads, and a test that cleared only
  part of the set left a half-registered process (CI shard 14/16 on PR #3513).
- **Declared vs implemented checked after the fact.** `ava-plugin.json` lists a plugin's contribution
  keys, but the only way to learn what a plugin really contributed was to import it and read the ledger
  back.

The user's principle: changing state at import time is wrong; a plugin only declares, and the framework
registers.

## Decision

A plugin's `agent_runtime.py` exports `contribute()`, a pure function returning a frozen
`PluginContributions` (`base/packages/plugins/extensions.py`). The loader calls it for every enabled
plugin and holds the results in an `ExtensionRegistry` instance, which the composition roots hand to the
code that consumes them — for the agent runtime, `AvaContext.extensions`, set by the agent host.
Attribution is the registry entry (plugin name beside its contributions), not a ContextVar. A reload is
a new registry; nothing is truncated. `ava-plugin.json` contribution keys are compared against the
declaration, so declared-versus-implemented is checked from a value rather than from import side effects.

There is no compatibility period: the old entry points are deleted, every builtin plugin
migrates in the same change, and external plugins are not carried.

This is done one surface family at a time, each its own change:

- **A (this entry's first change)**: system prompt sections and context notes. The framework's own are
  `FRAMEWORK_SECTIONS` / `FRAMEWORK_NOTES` constants; `build_system_prompt`, `context_notes` and
  `fork_notes` take the registry as their first argument.
- **B**: hooks, state fields, inspector widgets and metrics; deletes `clear_plugin_registrations`.
- **C**: SDK namespaces, members, wraps and providers.

## Alternatives rejected

- **Keep the registries and fix their leaks.** Each leak was a symptom of the same shape — a
  process-global written at import, reset by a function that must know every registry. A new registry
  would reintroduce it.
- **One module-level `ExtensionRegistry` owned by the loader.** Moves the global without removing it:
  consumers import it, tests patch it, a reload still mutates it.
- **Declare in `ava-plugin.json` only.** The manifest holds names, not callables; the implementation
  still has to be bound to the name somewhere, and a second source of truth drifts.
- **A deprecation window with both mechanisms live.** Two registration paths would double the surface
  every consumer reads and keep the ContextVar alive for as long as the window lasts.
- **Make `AvaContext.extensions` required.** Rejected for now: the default `EMPTY` lets framework-only
  callers (eval containers, tests of framework behaviour) build a context without a loader. The cost is
  that a composition root that forgets to pass the registry silently drops plugin contributions; the
  agent host, the only production root, passes it and `test_a_plugin_declaration_reaches_the_head_through_the_context_registry`
  pins the consumer side.

## Consequences

- Plugin sections and notes carry their plugin by construction, so the activation record for a section
  no longer depends on a `PluginContext` being open.
- The surfaces not yet migrated (B and C) still register at import under `PluginContext` until their
  change lands; the catalog merges both sources meanwhile.
- A plugin face's `contribute()` must stay pure and cheap: the loader calls it on every registry build,
  and one that raises or returns a non-`PluginContributions` is reported and skipped like a plugin that
  fails to import.
