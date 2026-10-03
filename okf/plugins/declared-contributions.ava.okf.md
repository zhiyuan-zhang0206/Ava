---
type: doc
title: Plugin Declared Contributions
description: A plugin declares system prompt sections and context notes as a frozen `PluginContributions` returned by `contribute()`; the loader collects them into an `ExtensionRegistry` the agent host hands to its turns.
tags:
- plugins
---

# Plugin Declared Contributions

A plugin does not register system prompt sections or context notes: its `agent_runtime.py` exports
`contribute() -> PluginContributions` (`base/packages/plugins/extensions.py`), a pure function returning a
frozen declaration.

- `system_prompt_sections` — `(slices: AgentSlices) -> str`; an empty string contributes nothing.
- `context_notes` — `ContextNote(build, on_fork, rank)`; `build` returns a `HumanMessage` or `None` when it has
  nothing to say. Lower `rank` sits closer to the SystemMessage; `on_fork` also grafts the note onto a fork.

`agent/extensions/registry.py:build_registry()` calls `contribute()` on every enabled plugin's loaded face, in plugin name
order, and returns an `ExtensionRegistry` — plugin name beside its contributions, so attribution is the entry,
not a ContextVar. A face whose `contribute()` raises or returns something else is reported and skipped like a
plugin that fails to import. Nothing is registered anywhere: a reload is a new registry.

The agent host builds the registry after the graph (`services/agent_host/daemon.py`) and sets it on each turn's
`AvaContext.extensions` (default `EMPTY`: the framework's own `FRAMEWORK_SECTIONS` / `FRAMEWORK_NOTES` only).
`build_system_prompt(extensions, slices)`, `context_notes(extensions, slices)` and `fork_notes(extensions, slices)`
read it. The attribution catalog (`ava plugins inspect`) merges `registry.records(plugin)` with the ledger of
the surfaces still registered at import, and compares both with the `ava-plugin.json` contribution keys.

Why: [decisions/2026-10-03-plugins-declare-the-framework-registers.md](../../decisions/2026-10-03-plugins-declare-the-framework-registers.md).
