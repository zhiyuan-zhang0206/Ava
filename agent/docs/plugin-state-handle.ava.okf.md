---
type: doc
title: Plugin State Handle and the Exec Slot
description: How a plugin reaches its graph state from each side of the exec process boundary, and why `ava.state` exists only inside an exec turn.
tags: []
---

# Plugin State Handle and the Exec Slot

A plugin's state is touched from two processes, so `PluginStateHandle` (`agent/state.py`) has two sides.

- **Host side (graph hooks).** A hook runs in the agent host and is handed the graph `state`. `handle.view(state)` builds the typed snapshot from it (`model_construct`: the graph already validated it, nothing is revalidated or copied, so a declared `messages` channel costs nothing), and `handle.delta({...})` maps plugin-local fields to the prefixed update dict the hook returns for LangGraph's reducer. Both are pure; neither names the SDK.
- **Exec side (SDK functions in the exec child).** `handle.read()` / `handle.update()` work on the exec turn's slot. The exec child binds `ava.state` (the snapshot, validated from the request envelope) and `ava.state_update` (the accumulated raw delta); the child returns the delta in its result envelope and the exec node commits it through the reducers.

The slot exists only in that child. Anywhere else (the host included) reading `ava.state` or `ava.state_update` raises `PluginStateOutsideTurnError`, an `AttributeError`, never a None; assigning `ava.state = None` is a `TypeError`. `ava.in_exec_turn()` is the explicit question the SDK's own call sites ask (the cwd-aware wraps, the security scan), `ava.unbind_exec_turn()` ends a turn (an external attachment's detach, a test), and `agent.state.compact_version()` is the exec-side read of the built-in compaction counter. Nothing resets the slot in the child: it is discarded with the process. An external attachment binds it for its lifetime; an exec turn cannot attach.

A hook module imports no `ava` (`scripts/lint/plugins/no_ava_in_hooks.py`, no allowlist): `agent/hooks/`, a plugin's `agent_runtime.py` face and any module defining a `Hook` subclass. SDK-touching code a face needs (a prompt section, a state default) lives in a sibling module such as ava_code's `_state.py` and `_prompt_sections.py`. Rationale and rejected alternatives: [decision](../../docs/decisions/agents/graph/2026-10-04-hooks-operate-on-graph-state.md).

Parent: [[agent/docs/state.ava.okf.md|state]].
