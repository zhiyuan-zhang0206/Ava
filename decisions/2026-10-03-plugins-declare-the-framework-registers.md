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

- **A**: system prompt sections and context notes. The framework's own are `FRAMEWORK_SECTIONS` /
  `FRAMEWORK_NOTES` constants; `build_system_prompt`, `context_notes` and `fork_notes` take the
  registry as their first argument.
- **B**: graph-edge hooks and plugin state. `PluginContributions` gains `after_init` / `before_llm` /
  `before_exec` / `after_exec` and `state`. The graph is a function of the registry:
  `build_graph(checkpointer, extensions)` runs the plugins' hooks, then the framework's own
  (`framework_hooks()`), and `build_agent_state(extensions)` builds the state class, which carries its own
  schema instead of reading module tables. `build_registry()` becomes the load-time gate: a plugin whose
  `ava-plugin.json` and `contribute()` disagree on the `hooks` / `systemPromptSections` keys (either
  direction), whose state fails validation, or whose `contribute()` is malformed is a load failure of that
  plugin.
- **B2**: plugin metrics and inspector widgets. Their consumers (the gateway, the Grafana supply) are other
  processes that never load an agent runtime face, so each declares in its own data face (`metrics.py`,
  `inspector.py`, each exporting `contribute()`), and a process builds a data registry
  (`base/packages/plugins/data_registry.py`) from the faces it loads. A face is gated on its own manifest key
  (`metrics`, `inspectWidgets`).
- **C**: the SDK surface (`ava.*` namespaces, members, expansions, wraps, skill sources) plus plugin
  config and core flags. `plugin.py` exports `contribute()`; `ava/sdk_surface/install.py:install(registry)`
  is the one writer of the `ava` module (a singleton that agent code reaches by attribute access, so it
  cannot be passed as a value): per plugin it applies the declaration and rolls the plugin back whole if
  any piece fails, then installs the SDK-usage recorder last so it stays outermost over every wrap layer;
  `uninstall()` takes the recorder off first and undoes newest-first. The plugin-side `register_*` /
  `extend.wrap` / `declare_flags` entry points, the `PluginContext` ContextVar, the attribution ledger and
  `clear_plugin_registrations` are deleted. The installation is recorded on the `ava` module object, not in a
  module global. Providers (`provider.py`) follow in their own change under the same declaration + gate shape;
  the model catalog stays a process-level singleton with one writer.

## Reload

There is no in-process plugin reload path. The agent host loads plugins once per process; a plugin installed
or changed afterwards is picked up by a host restart (`services/agent_host/daemon.py` watches the plugin
directory and exits so the supervisor restarts it), because plugin-spec-v2's dispose contract is
unimplemented. The graph is compiled once from the registry the daemon builds at boot (its hooks and state
channels are fixed in the compiled graph), so swapping only the registry the turns read would give prompt
sections from one plugin set and hooks from another. A registry is a value, so a future reload is "build a
new registry, build a new graph, swap both", but nothing triggers that today and none is added here.

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

## Final shape (A through C2)

A plugin ships up to six faces, each a plain module exporting a pure `contribute()` that returns the
`PluginContributions` fields it owns; a process builds its registry from the faces it loads, gates each
plugin against the keys of those faces in its `ava-plugin.json` (a mismatch, a malformed `contribute()`,
an invalid state or a bad config is a load failure of that plugin: reported, left out), and hands the
registry to whatever consumes it.

| Face (module) | Fields | Loaded by | Registry built by | Consumed by |
| --- | --- | --- | --- | --- |
| `plugin.py` | `sdk_namespaces`, `sdk_members`, `sdk_expansions`, `sdk_wraps`, `skill_sources`, `config`, `flags` | every process that runs agent code: agent host, exec child, watcher / schedule child, external attach | `agent.extensions.registry.build_registry(faces)` inside `load_extensions` | `ava.sdk_surface.install.install` |
| `agent_runtime.py` | hooks, `state`, `system_prompt_sections`, `context_notes` | the agent host (full `load_extensions`); a stateful exec child or external attach upgrades to it only to build the state class | same builder, both faces merged per plugin | the host daemon: checkpoint serde, `build_graph(checkpointer, extensions)`, `AvaContext.extensions` |
| `metrics.py`, `inspector.py` | `metrics`, `inspect_widgets` | the gateway (per call), the Grafana dashboard supply (repo and installed plugins), `ava plugins inspect` | `data_registry.build_data_registry` over `load_declaration` | inspector endpoints, dashboard render |
| `provider.py` | `providers` | every process that builds or validates a chat model | none: each declaration is installed as it is read | `base/lm/plugin_providers.py` into the process's model catalog |

The `ava` module is a singleton that agent code reaches by attribute access, so its writes are
concentrated in `ava/sdk_surface/install.py`: per plugin, in name order, namespaces, members, expansions,
wraps, skill sources, flags, config; a plugin whose declaration cannot be applied is rolled back whole and
omitted from the registry `install` returns; the SDK-usage recorder goes in last (outermost over every
wrap layer) and comes off first on `uninstall`; the installation is recorded on the `ava` module object.
The only other process-wide writes left are the model catalog (one writer, the provider loader, because
chat-model building, config validation, model lists and context budgets read it as a process-wide fact and
a process never holds two catalogs), the rebinding of `agent.state.AgentState` by `build_agent_state`, and
the bound plugin configs / declared flags that `install` writes for the readers in `ava._settings` and
`read_flag`. There is no `PluginContext` ContextVar, no attribution ledger and no `register_*` entry point
on the plugin side.

Left over, each a deliberate non-change:

- No in-process reload exists (see Reload): the host loads once, the daemon restarts on a plugin directory
  change. A registry is a value, so "new registry, new graph, new install, swap" is now expressible; nothing
  triggers it.
- The gateway and ops processes load no `plugin.py`, so overlay validation there knows only framework fields
  and rejects an overlay naming a plugin's config field as an unknown key. Unexercised today (no builtin
  plugin declares a config); fixing it means loading the declared config classes at that boundary.
- The gateway loads every shipped plugin's `metrics.py` regardless of the enable state, while inspector
  widgets follow it. Unifying them is a behavior change not made here.
- `agent.state.AgentState` is still rebound by `build_agent_state(extensions)`, and the exec IPC serializer
  reads the classes off it (`process_state_classes`).
- `scan_and_load` at host boot still imports external `plugin.py` files that the full `load_extensions`
  imports again.
- `ava plugins update` still finds a plugin's config class in `default_config.py` by inspection rather than
  from the declaration.
- No compatibility layer: plugins outside the repo that call the deleted entry points must export
  `contribute()`.
