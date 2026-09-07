---
type: doc
title: Agent State
description: "Ava agent's LangGraph conversation, lifecycle, takeover receipt, and plugin state channels."
tags: []
---

# Agent State

## What it is

Ava agent's LangGraph state management system. Base `BaseAgentState` carries conversation and lifecycle channels, plus nested `compact`, `circuit`, `attach`, `memory`, `context_reset`, and `capabilities` state. Plugins register whole Pydantic BaseModel chunks via `register_plugin_state(Cls)`, and the framework automatically merges them into `AgentState`.

`impersonation_request_id` records the last consent request and version across
compaction. `impersonation_applied` records the external lease and plugin-log
version applied in the same checkpoint as its delta; recovery uses it to avoid
repeating reducers after an acknowledgement failure. See [[impersonation.ava.okf.md]].

## Core Responsibilities

- **BaseAgentState**: `messages` (guarded `add_messages` reducer), lifecycle and turn flags, takeover receipts, and `active_task_id`. Claim sets `active_task_id` only from an explicitly task-associated system note and clears it for chat or unassociated inbound work; a co-batched set of distinct task ids is likewise untagged because it drives one LLM turn. LLM usage attribution never follows task ownership. Nested last-value channels are `compact` (`CompactState`) / `circuit` (`CircuitState`) / `attach` (`AttachState`) / `context_reset` (`ContextReset`) / `capabilities` (`CapabilitiesState`); `memory` (`MemoryState`) has its union reducer. `agent/state.py:_BASE_STATE_FIELDS` derives from `BaseAgentState.model_fields` (`state._BASE_FIELDS`), auto-syncing the plugin-write guard with base fields.
- **Plugin state registration**: `register_plugin_state(Cls) → PluginStateHandle[Cls]`
  - Field names overlapping with BaseAgentState → treated as modifying base fields, no prefix added, types must exactly match
  - Field names disjoint → automatically prefixed with `<plugin>__<field>`, becoming plugin-private channels
  - Two plugins declare fields with same name and type → fail-fast error, force rename
- **Dynamic class construction**: `build_agent_state()` dynamically creates `AgentState` (subclass of BaseAgentState + all plugin fields) at graph build time
- **Type safety**: `PluginStateHandle` provides `.read()` / `.update()` methods, full Pydantic validation throughout

## Key Dependencies

- [[graph.ava.okf.md]] — `build_agent_state()` called at graph build time
- [[hooks.ava.okf.md]] — graph edge hooks receive and return the whole `state` parameter (via LangGraph reducer), **not** through `PluginStateHandle` — handle is only available to agent code inside execute_code (`ava.state` / `ava.state_update`)

## Entry Points

- `agent/state.py:BaseAgentState` — base state class
- `agent/state.py:register_plugin_state()` — plugin state registration
- `agent/state.py:build_agent_state()` — dynamic class construction
- `agent/state.py:checkpoint_msgpack_allowlist()` — checkpoint serde allowlist (framework saver + eval driver)

## Notes

- Design allows **multiple plugins declaring the same base field** (e.g., messages), because reducers naturally merge — this is more flexible than "exclusive fields"
- Plugins never access `ava.state` / `ava.state_update` directly (framework internal slots); all reads/writes go through `PluginStateHandle`
- Prefix mechanism avoids field name conflicts between plugins, no global registry coordination needed
- **Nested sub-state writing**: `compact` and `attach` are last-value channels. `attach.pending` holds resolved path + optional label entries from completed exec calls until claim drains them into one message, then clears it; duplicate paths retain their first position and latest label. `memory` is a union-reducer channel, recall hook only writes fresh paths for the current turn; reducer accumulates and deduplicates across turns (see `agent/hooks/compact.py`, `plugins/ava_memory/plugin.py`)
- **`capabilities.indexed`** is the record of what the rendered `# Capabilities` index actually lists — written by `init_context` when it builds the prompt, advanced by the drift check in `agent/hooks/capabilities.py`. Its `None` default is load-bearing and distinct from an empty set: `None` = a checkpoint written before the field existed, where the drift check adopts the live catalog silently rather than announcing the whole catalog as newly installed. See [[graph/system-prompt.ava.okf.md]]
- **Checkpoint compatibility**: nesting changes channel keys from flat (e.g., `compact_version`) to `compact` / `memory`. Old thread checkpoints lacking the new channel keys → LangGraph resume reads defaults (`from_checkpoint(MISSING)`), old flat keys are ignored (no error, no crash) — effect is that first resume resets compact/memory counters to default (self-healing, no message loss), message history and plugin channels preserved as-is.
- **Checkpoint msgpack allowlist**: nested sub-states (`AttachState` / `AttachEntry` / `CompactState` / `MemoryState` / `ContextReset` / `CapabilitiesState`) serialize into checkpoints as pydantic-v2 ext objects carrying `(module, name)`. LangGraph's `JsonPlusSerializer` only deserializes types on its explicit allowlist; without one it runs permissive and warns on **every** checkpoint load ("Deserializing unregistered type agent.state.*" — once per type per process start), and a future langgraph blocks them outright. The framework's saver (`services/agent_host/daemon.py:_build_checkpointer`) passes `allowed_msgpack_modules=checkpoint_msgpack_allowlist()` — the nested types plus every `register_plugin_state` class (a plugin field holding a BaseModel instance crosses the checkpointer as that class). New nested sub-states must be added to the allowlist or they degrade to raw dicts on load
