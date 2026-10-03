---
type: doc
title: Plugin Declared Contributions
description: A plugin declares hooks, state, system prompt sections and context notes as a frozen `PluginContributions` returned by `contribute()`; the loader collects them into a gated `ExtensionRegistry` the host hands to the checkpointer, the graph and its turns.
tags:
- plugins
---

# Plugin Declared Contributions

A plugin does not register hooks, state, system prompt sections or context notes: its `agent_runtime.py` exports
`contribute() -> PluginContributions` (`base/packages/plugins/extensions.py`), a pure function returning a
frozen declaration.

- `system_prompt_sections` — `(slices: AgentSlices) -> str`; an empty string contributes nothing.
- `context_notes` — `ContextNote(build, on_fork, rank)`; `build` returns a `HumanMessage` or `None` when it has
  nothing to say. Lower `rank` sits closer to the SystemMessage; `on_fork` also grafts the note onto a fork.
- `after_init` / `before_llm` / `before_exec` / `after_exec` — `Hook` instances ([[okf/plugins/graph-edge-hooks.ava.okf.md]]).
- `state` — `BaseModel` classes whose fields become channels `<plugin>__<field>`; the plugin keeps its own
  `PluginStateHandle(cls, plugin)`.
- `metrics` / `inspect_widgets` — `MetricSpec` / `InspectWidgetSpec`, declared by the plugin's `metrics.py` /
  `inspector.py` instead (see below).
- The SDK surface, declared by `plugin.py`: `sdk_namespaces` (`SdkNamespace(name, module, expand)`), `sdk_members`
  (`SdkMember(namespace, name, fn)` on an existing namespace), `sdk_expansions` (dotted paths promoted into the
  prompt's expanded SDK reference), `sdk_wraps` (`SdkWrap(target, wrapper)`), `skill_sources`, `config` (one frozen
  BaseModel bound from `~/.ava/configs/<plugin>/config.json`, schema drift points at `ava plugins update`) and `flags`
  (`<domain>.<field>` core settings the plugin may read through `read_flag`).

`agent/extensions/registry.py:build_registry()` calls `contribute()` on every enabled plugin's loaded face, in plugin name
order, and returns an `ExtensionRegistry` — plugin name beside its contributions, so attribution is the entry,
not a ContextVar. It is also the load-time gate: a plugin is admitted only if `contribute()` returns a
`PluginContributions`, its state validates (`plugin_state_schema`), and — when it ships `ava-plugin.json` — the
manifest's `hooks` / `systemPromptSections` keys match what `contribute()` provides in both directions. Otherwise the
plugin is a load failure (reported, left out). Nothing is registered anywhere: a new registry is the whole of a reload.

The agent host builds the registry once, after the full plugin load, and hands that one value to the checkpoint
serde (state classes), `build_graph(checkpointer, extensions)` (hooks, state fields) and each turn's
`AvaContext.extensions` (default `EMPTY`: the framework's own sections, notes and hooks only). The graph is compiled
once, so a changed plugin set takes effect on the next host start (the plugin directory watchdog); there is no
in-process swap.
`build_system_prompt(extensions, slices)`, `context_notes(extensions, slices)` and `fork_notes(extensions, slices)`
read it. The attribution catalog (`ava plugins inspect`) reads each plugin's declaration records and compares them with the `ava-plugin.json` contribution keys.

## The SDK install

`ava` is a singleton module that agent code reaches by attribute access, so it cannot be passed around as a value; its
writes are concentrated in `ava/sdk_surface/install.py:install(registry)`, the only place that mutates it. Per plugin, in
registry (plugin name) order: namespaces, members, expansions, wraps (a target may be a namespace or member just added),
skill sources, flags, config. A plugin whose declaration cannot be applied (a conflicting or disabled namespace name, a wrap
target that does not resolve, a config that does not bind) is rolled back whole, reported as a load failure and absent from
the registry `install` returns. The SDK-usage recorder (`metering`) is installed last so it sits outermost over every wrap
layer, and `uninstall()` takes it off first and undoes every layer newest-first. A second `install` without `uninstall` is
an error; a reload is a new registry and a new install, and nothing triggers one at runtime.

## Faces and registries

A declaration lives in the face the consuming process loads: `agent_runtime.py` for the agent host (the registry above),
`metrics.py` and `inspector.py` for processes with no agent runtime. Each exports `contribute()` with only its own
field filled. The gateway (per-agent inspector panels) and the Grafana supply (dashboard render, repo and
installed plugins) build a **data registry** with `base/packages/plugins/data_registry.py`: `load_declaration` imports a
face fail-soft, `build_data_registry` validates and admits each plugin whole (query validation, metric names unique
across plugins, widget ids unique within one, `plugin` filled from the entry). A face is gated on its own manifest key
(`metrics`, `inspectWidgets`; `base/packages/plugins/gate.py`). A refused or unloadable face costs only that plugin and
leaves no partial state.

Why: [decisions/2026-10-03-plugins-declare-the-framework-registers.md](../../decisions/2026-10-03-plugins-declare-the-framework-registers.md).
