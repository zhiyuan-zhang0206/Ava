---
type: doc
title: Agent Hooks
description: Ava plugin's graph-edge hook system—four hook container nodes (after_init / before_llm / before_exec / after_exec) provide extension points in the LangGraph execution graph. Plugins register instances of Hook subclasses; the framework calls their typed __call__ in registration order during graph node execution, with reducer arbitration for co-writes.
tags: []
---

# Agent Hooks

## What it is

Ava plugin's **graph-edge hook system**—four hook container nodes (`after_init / before_llm / before_exec / after_exec`) provide extension points in the LangGraph execution graph. Plugins register instances of `Hook` subclasses; the framework calls their typed `__call__` in registration order during graph node execution, with reducer arbitration for co-writes.

Together with SDK wraps (a plugin's declared `SdkWrap`, applied by `ava/sdk_surface/install.py` through `ava/sdk_surface/wraps.py`), this forms the two-layer plugin system: SDK wraps modify the behavior of the ava namespace, while graph-edge hooks intervene in the agent execution pipeline.

## Core Responsibilities

- **`Hook` base class** (`agent/hooks/_registry.py:Hook`, `ABC`): subclasses override the typed `async def __call__(self, state, runtime, config, /) -> dict | None`. The base locks the signature—under pyright strict, incompatible overrides raise `reportIncompatibleMethodOverride`; not overriding leaves an uninstantiable abstract class. Optional `.name` property (defaults to class name) labels co-write conflict messages.
- **Hook declaration**: a plugin's `contribute()` returns `PluginContributions(before_llm=(...), before_exec=(...), after_exec=(...), after_init=(...))` of `Hook` **instances** (not class, not bare function); nothing is registered at import. The framework's own are `agent/hooks/framework.py:framework_hooks()`
- **Hook execution**: `make_hook_runner(name, default_next, hooks)` creates a node function at graph build time—`hooks` is the `(plugin | None, hook)` sequence the build hands it (plugins' first, then the framework's), fixed for the life of that graph; the runner loops `await hook(state, runtime, config)`—the instance can be called directly (`Hook.__call__`), which is identical in form to the old bare-function calling style.
- **Hook points**: `HookName = HookPoint = Literal["before_llm", "before_exec", "after_exec", "after_init"]` (`base/packages/plugins/extensions.py`)
- **Co-write arbitration**: If two hooks within the same run write to the same state key—if that key has a non-trivial reducer in the state schema (e.g. the `messages` channel's guarded reducer), the runner merges both values using the reducer; if no reducer, raises `RuntimeError` fail-loud (refusing silent last-wins, preventing one hook's wholesale replacement from swallowing another hook's appended content)
- **Instance state**: subclasses carry per-hook state/configuration in `__init__` on `self`—declarations hold instances, not bare functions. Built-in `_CompactReminderHook` / `_RepairDanglingToolPairingHook` are module-level singletons that `framework_hooks()` returns.
- **Route override**: If the dict returned by a hook contains `"goto": NodeName`, the container node's default route is overridden; other keys are treated as part of `Command(update=...)`
- **Activation telemetry**: a hook declared by a *plugin* that returns a non-empty dict also emits one `plugin_activation` event naming the keys it wrote (`base/packages/plugins/activation.py`; attribution is the plugin name paired with the hook in the runner's `hooks`). Framework hooks and `None` returns record nothing, so pure observation stays free. This is the runtime half of the registration ledger and philosophy §6's obsolescence gauge — see [[okf/plugins/plugins.ava.okf.md]]

## Key Dependencies

- [[agent/docs/state.ava.okf.md]] — The hook's `state` parameter is the `AgentState` passed by LangGraph; the returned dict goes through standard LangGraph reducer merging; hooks run at the graph-node level and directly receive/return the whole state. A plugin hook reads its own fields with the handle's pure `view(state)` and returns writes through `delta({...})`; `read` / `update` are the exec child's channel (`ava.state` / `ava.state_update` exist only there). A hook module imports no `ava` (`scripts/lint/plugins/no_ava_in_hooks.py`)
- [[agent/graph/docs/graph.ava.okf.md]] — Placement of hook container nodes in the 8-node topology

## Entry Points

- `agent/hooks/_registry.py:Hook` — Base class; subclasses override `__call__`
- `agent/hooks/framework.py:framework_hooks()` — The framework's own hooks per edge
- `agent/hooks/_registry.py:make_hook_runner()` — Called at graph build time
- `agent/hooks/__init__.py` — Public API re-exports (`Hook`, `HookName`, `make_hook_runner`)
- `agent/hooks/understanding_chunks.py` — when understanding chunks are enqueued: after an llm turn (`due_chunk_update`) and at compaction (`enqueue_closing_chunk`, called by `stamp_compact_boundary`); see [[base/agents/history/hierarchy/docs/chunks.ava.okf.md]]
- `agent/hooks/history_dump.py:dump_history()` — pre-compact JSONL dump of the full conversation, written by both compaction paths (`agent/graph/claim/_decide.py` and `agent/hooks/compact.py`) before the history is wiped

## Notes

- **after_init** is the earliest hook point: its container node sits right after `START` and before `init_context`, so its state edits land before the standing message head is laid down (see [[agent/graph/docs/graph.ava.okf.md]] for placement). Example: the ava_code plugin registers `validate_cwd_after_init` here to repair a persisted logical cwd that cannot be statted or is not a directory, without mutating the Python process cwd.
- A hook returning `None` means "no modification"—it produces no state update
- Hook signatures are uniform, not distinguishing between nodes—the same `Hook` subclass instance can theoretically be registered at multiple hook points
- Built-in before_llm hooks (compact / repair / capability-index drift) — the three unconditional built-ins and their registration order: [[agent/hooks/docs/builtin-before-llm.ava.okf.md]].
- Plugin hooks are also `Hook` subclasses (e.g., `ava_builtins/plugins/ava_sdk_reminder/agent_runtime.py:_SdkReminderAfterExecHook`, `ava_builtins/plugins/ava_code/agent_runtime.py:_InjectCwdNotesAfterExecHook`, `plugins/ava_memory`'s recall hook, etc.), written in the same style as built-in hooks—plugin authors don't need to care about built-in vs plugin distinction
- The permission hook example (`demos/permission-hooks/sensitive_op_gate.py:_SensitiveOpGateHook`) demonstrates the hook system's access control application: two-stage interception (block/warn), intercepting sensitive operations (force push, delete files, external sends, etc.) in before_exec; when blocking, returns `goto: "after_exec"` to skip the exec node
