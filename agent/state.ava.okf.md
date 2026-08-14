---
type: doc
title: Agent State
description: "Ava agent's LangGraph state management system. Base `BaseAgentState` has seven channels: top-level `messages` / `halted` / `update_initiated`, plus four nested sub-states `compact` (CompactState) / `memory` (MemoryState) / `context_reset` (ContextReset) / `capabilities` (CapabilitiesState)"
tags: []
---

# Agent State

## What it is

Ava agent's LangGraph state management system. Base `BaseAgentState` has seven channels: three top-level flat fields — `messages` (cross-turn LLM history, `add_messages` reducer wrapped by the append-only guard `guarded_add_messages` — only a full wipe, a tail append, or modifying the last message is allowed, task #1256), `halted` (whether current turn is finished), `update_initiated` (whether a self-update has been initiated); plus four nested sub-states — `compact` (`CompactState`: `version` / `reminder_shown` / `reminder_seen_version`, last-value channel), `memory` (`MemoryState`: `injected_paths`, union-reducer channel `_memory_state_merge`), `context_reset` (`ContextReset`: parked tail + resume target, last-value channel), and `capabilities` (`CapabilitiesState`: `indexed`, last-value channel). Plugins register whole Pydantic BaseModel chunks via `register_plugin_state(Cls)`, and the framework automatically merges them into `AgentState`.

## Core Responsibilities

- **BaseAgentState**: seven built-in channels `messages` (guarded `add_messages` reducer — append-only invariant, task #1256) / `halted` / `update_initiated` / `compact` (nested `CompactState`) / `memory` (nested `MemoryState`) / `context_reset` (nested `ContextReset`) / `capabilities` (nested `CapabilitiesState`). `_exec.py:_BASE_STATE_FIELDS` derived from `BaseAgentState.model_fields` (`state._BASE_FIELDS`), auto-sync with base — ensures plugins cannot wrongly write to any undeclared base channel via `ava.state_update`
- **Plugin state registration**: `register_plugin_state(Cls) → PluginStateHandle[Cls]`
  - Field names overlapping with BaseAgentState → treated as modifying base fields, no prefix added, types must exactly match
  - Field names disjoint → automatically prefixed with `<plugin>__<field>`, becoming plugin-private channels
  - Two plugins declare fields with same name and type → fail-fast error, force rename
- **Dynamic class construction**: `build_agent_state()` dynamically creates `AgentState` (subclass of BaseAgentState + all plugin fields) at graph build time
- **Type safety**: `PluginStateHandle` provides `.read()` / `.update()` methods, full Pydantic validation throughout

## Key Dependencies

- [[graph.ava.okf.md]] — `build_agent_state()` called at graph build time
- [[hooks.ava.okf.md]] — graph edge hooks receive and return the whole `state` parameter (via LangGraph reducer), **not** through `PluginStateHandle` — handle is only available to agent code in the exec_node's worker thread (`ava.state` / `ava.state_update`)

## Entry Points

- `agent/state.py:BaseAgentState` — base state class
- `agent/state.py:register_plugin_state()` — plugin state registration
- `agent/state.py:build_agent_state()` — dynamic class construction
- `agent/state.py:checkpoint_msgpack_allowlist()` — checkpoint serde allowlist (framework saver + eval driver)

## Notes

- Design allows **multiple plugins declaring the same base field** (e.g., messages), because reducers naturally merge — this is more flexible than "exclusive fields"
- Plugins never access `ava.state` / `ava.state_update` directly (framework internal slots); all reads/writes go through `PluginStateHandle`
- Prefix mechanism avoids field name conflicts between plugins, no global registry coordination needed
- **Nested sub-state writing**: `compact` is a last-value channel, writer takes current value `model_copy(update=...)` modifying only a subset of its fields (version bump or reminder flags); full overwrite does not reset sibling fields. `memory` is a union-reducer channel, recall hook only writes fresh paths for the current turn; reducer accumulates and deduplicates across turns (see `agent/hooks/compact.py`, `plugins/ava_memory/plugin.py`)
- **`capabilities.indexed`** is the record of what the rendered `# Capabilities` index actually lists — written by `init_context` when it builds the prompt, advanced by the drift check in `agent/hooks/capabilities.py`. Its `None` default is load-bearing and distinct from an empty set: `None` = a checkpoint written before the field existed, where the drift check adopts the live catalog silently rather than announcing the whole catalog as newly installed. See [[graph/system-prompt.ava.okf.md]]
- **Checkpoint compatibility**: nesting changes channel keys from flat (e.g., `compact_version`) to `compact` / `memory`. Old thread checkpoints lacking the new channel keys → LangGraph resume reads defaults (`from_checkpoint(MISSING)`), old flat keys are ignored (no error, no crash) — effect is that first resume resets compact/memory counters to default (self-healing, no message loss), message history and plugin channels preserved as-is.
- **Checkpoint msgpack allowlist**: nested sub-states (`CompactState` / `MemoryState` / `ContextReset` / `CapabilitiesState`) serialize into checkpoints as pydantic-v2 ext objects carrying `(module, name)`. LangGraph's `JsonPlusSerializer` only deserializes types on its explicit allowlist; without one it runs permissive and warns on **every** checkpoint load ("Deserializing unregistered type agent.state.*" — once per type per process start), and a future langgraph blocks them outright. The framework's saver (`agent/loop.py:_build_checkpointer`, MyAva's `evals/driver.py`) passes `allowed_msgpack_modules=checkpoint_msgpack_allowlist()` — the four sub-states plus every `register_plugin_state` class (a plugin field holding a BaseModel instance crosses the checkpointer as that class). New nested sub-states must be added to the allowlist or they degrade to raw dicts on load
