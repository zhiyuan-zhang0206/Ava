---
type: doc
title: Agent State
description: "Ava agent's LangGraph conversation, lifecycle, takeover receipt, and plugin state channels."
tags: []
---

# Agent State

## What it is

Ava agent's LangGraph state management system. Base `BaseAgentState` carries conversation and lifecycle channels, plus nested `compact`, `circuit`, `attach`, `memory`, `context_reset`, and `capabilities` state. Plugins declare whole Pydantic BaseModel chunks in `PluginContributions.state`, and the framework merges them into `AgentState`.

`impersonation_introduced` retains the native conversation's first-takeover
explanation receipt across compaction; context establishment restores that
standing explanation only after a takeover has occurred.
`impersonation_request_id` records the last consent request and version across
compaction. `impersonation_applied` records the external lease and plugin-log
version applied in the same checkpoint as its delta; recovery uses it to avoid
repeating reducers after an acknowledgement failure. See [[base/agents/impersonation/docs/impersonation.ava.okf.md]].

## Core Responsibilities

- **BaseAgentState**: `messages` (delta channel since the 2026-09-14 write switch — guarded reducer pair `guarded_delta_reducer` / `guarded_add_messages`), lifecycle and turn flags, takeover receipts. Nested last-value channels are `compact` (`CompactState`) / `circuit` (`CircuitState`) / `attach` (`AttachState`) / `context_reset` (`ContextReset`) / `capabilities` (`CapabilitiesState`); `memory` (`MemoryState`) has its union reducer. `agent/state.py:_BASE_STATE_FIELDS` derives from `BaseAgentState.model_fields` (`state._BASE_FIELDS`), auto-syncing the plugin-write guard with base fields.
- **Deferred exec notes**: `pending_exec_notes` is a framework-owned last-value list of context/security notes and media awaiting the last result in a multi-call assistant message. Every call commits plugin fields directly through LangGraph; this buffer controls presentation order only. Completion drains it, compaction clears it, and startup/before-LLM repair drains it after repairing interrupted pairing.
- **Plugin state declaration**: `PluginContributions.state` classes, validated by `plugin_state_schema(extensions)`; the plugin keeps a `PluginStateHandle(Cls, plugin)`
  - Field names overlapping with BaseAgentState → treated as modifying base fields, no prefix added, types must exactly match
  - Field names disjoint → automatically prefixed with `<plugin>__<field>`, becoming plugin-private channels
  - Two plugins declare fields with same name and type → fail-fast error, force rename
- **Dynamic class construction**: `build_agent_state(extensions)` creates `AgentState` (subclass of BaseAgentState + all plugin fields) at graph build time; the class carries its own schema (`__plugin_namespace_fields__`, `__plugin_base_declared__`, `__plugin_state_classes__`), so two registries give two independent classes
- **Type safety**: `PluginStateHandle` validates fully; it has a pure host side (`view` / `delta`, for graph hooks) and an exec side (`read` / `update`, for SDK functions in the exec child) — [[agent/docs/plugin-state-handle.ava.okf.md]]

## Key Dependencies

- [[graph.ava.okf.md]] — `build_agent_state(extensions)` called at graph build time
- [[hooks.ava.okf.md]] — graph edge hooks receive and return the whole `state` parameter (via LangGraph reducer); a plugin hook uses the handle's pure `view` / `delta`

## Entry Points

- `agent/state.py:BaseAgentState` — base state class
- `agent/state.py:plugin_state_schema()` — validates the declared plugin state
- `agent/state.py:build_agent_state()` — dynamic class construction
- `agent/state.py:checkpoint_msgpack_allowlist()` — checkpoint serde allowlist (framework saver + eval driver)

## Notes

- Design allows **multiple plugins declaring the same base field** (e.g., messages), because reducers naturally merge — this is more flexible than "exclusive fields"
- `ava.state` / `ava.state_update` exist only inside an exec turn (elsewhere they raise, never None), and hook modules import no `ava` — [[agent/docs/plugin-state-handle.ava.okf.md]]
- Prefix mechanism avoids field name conflicts between plugins, no global registry coordination needed
- **Nested sub-state writing**: `compact` and `attach` are last-value channels. `attach.pending` holds resolved path + optional label entries from completed exec calls until claim drains them into one message, then clears it; duplicate paths retain their first position and latest label. `memory` is a union-reducer channel, recall hook only writes fresh paths for the current turn; reducer accumulates and deduplicates across turns (see `agent/hooks/compact.py`, `plugins/ava_memory/plugin.py`)
- **`capabilities.indexed`** is the record of what the rendered `# Capabilities` index actually lists — written by `init_context` when it builds the prompt, advanced by the drift check in `agent/hooks/capabilities.py`. Its `None` default is load-bearing and distinct from an empty set: `None` = a checkpoint written before the field existed, where the drift check adopts the live catalog silently rather than announcing the whole catalog as newly installed. See [[agent/graph/docs/system-prompt.ava.okf.md]]
- **Checkpoint compatibility**: nesting changes channel keys from flat (e.g., `compact_version`) to `compact` / `memory`. Old thread checkpoints lacking the new channel keys → LangGraph resume reads defaults (`from_checkpoint(MISSING)`), old flat keys are ignored (no error, no crash) — effect is that first resume resets compact/memory counters to default (self-healing, no message loss), message history and plugin channels preserved as-is.
- **Delta message reads**: `base/agents/history/checkpoint_postgres_walks.py` follows the requested checkpoint's exact parent chain to the nearest stored snapshot, reads write keys and sizes, and transfers bodies newest-first until the latest `RemoveMessage(REMOVE_ALL_MESSAGES)` or `Overwrite`. It preserves upstream checkpoint/task/index order and excludes the target's pending writes from the committed history. Body batches are bounded to 256 KiB and 128 writes; one oversized write is fetched intact. A reset discards the older seed and prefix; a historical pre-reset request still loads them. The typed `delta_message_suffix` event uses noise-tier telemetry retention and reports actual fetched write bytes/rows separately from retained writes. The enclosing `delta_read_compat` stage-2 totals include unused batch rows and snapshot seed bytes; `reset_decode_ms` measures the reset-probe pass separately from `decode_ms` inside history assembly, and probe failures report phase `reset_decode`. Binary Postgres results preserve the existing transport format. No history is deleted or rewritten.
- **Checkpoint msgpack allowlist**: nested sub-states (`AttachState` / `AttachEntry` / `CompactState` / `MemoryState` / `ContextReset` / `CapabilitiesState`) serialize into checkpoints as pydantic-v2 ext objects carrying `(module, name)`. LangGraph's `JsonPlusSerializer` only deserializes types on its explicit allowlist; without one it runs permissive and warns on **every** checkpoint load ("Deserializing unregistered type agent.state.*" — once per type per process start), and a future langgraph blocks them outright. The framework's saver (`services/agent_runner/agent_host/daemon.py:_build_checkpointer`) passes `allowed_msgpack_modules=checkpoint_msgpack_allowlist(plugin_state_classes)` — the nested types plus every declared plugin state class (a plugin field holding a BaseModel instance crosses the checkpointer as that class). New nested sub-states must be added to the allowlist or they degrade to raw dicts on load
