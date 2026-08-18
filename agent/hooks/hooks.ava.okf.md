---
type: doc
title: Agent Hooks
description: Ava plugin's graph-edge hook system—four hook container nodes (after_init / before_llm / before_exec / after_exec) provide extension points in the LangGraph execution graph. Plugins register instances of Hook subclasses; the framework calls their typed __call__ in registration order during graph node execution, with reducer arbitration for co-writes.
tags: []
---

# Agent Hooks

## What it is

Ava plugin's **graph-edge hook system**—four hook container nodes (`after_init / before_llm / before_exec / after_exec`) provide extension points in the LangGraph execution graph. Plugins register instances of `Hook` subclasses; the framework calls their typed `__call__` in registration order during graph node execution, with reducer arbitration for co-writes.

Together with SDK wraps (the `ava.extend.wrap` registration primitive in `ava/_extend.py`), this forms the two-layer plugin system: SDK wraps modify the behavior of the ava namespace, while graph-edge hooks intervene in the agent execution pipeline.

## Core Responsibilities

- **`Hook` base class** (`agent/hooks/_registry.py:Hook`, `ABC`): subclasses override the typed `async def __call__(self, state, runtime, config, /) -> dict | None`. The base locks the signature—under pyright strict, incompatible overrides raise `reportIncompatibleMethodOverride`; not overriding leaves an uninstantiable abstract class. Optional `.name` property (defaults to class name) labels co-write conflict messages.
- **Hook registration**: `register_before_llm(hook)` / `register_before_exec(hook)` / `register_after_exec(hook)` / `register_after_init(hook)` accept a `Hook` **instance** (not class, not bare function), appending to `HOOKS[hook_name]`
- **Hook execution**: `make_hook_runner(name, default_next)` creates a node function at graph build time—it snapshots the `HOOKS[hook_name]` list object itself into the closure (so hooks registered later at runtime are also visible to older runners), then loops `await hook(state, runtime, config)`—the instance can be called directly (`Hook.__call__`), which is identical in form to the old bare-function calling style.
- **Global registry**: `HOOKS: dict[HookName, list[Hook]]`, `HookName = Literal["before_llm", "before_exec", "after_exec", "after_init"]`
- **Co-write arbitration**: If two hooks within the same run write to the same state key—if that key has a non-trivial reducer in the state schema (e.g., `messages`'s `add_messages`), the runner merges both values using the reducer; if no reducer, raises `RuntimeError` fail-loud (refusing silent last-wins, preventing one hook's wholesale replacement from swallowing another hook's appended content)
- **Instance state**: subclasses carry per-hook state/configuration in `__init__` on `self`—the registry holds instances, not bare functions. Built-in `_AutoCompactHook` / `_RepairDanglingToolUseHook` are module-level singletons: `register_compact_hooks()` / `register_repair_hooks()` append the same instance at graph build time; identity is stable (tests assert `HOOKS["before_llm"][-1] is _repair_dangling_tool_use`).
- **Route override**: If the dict returned by a hook contains `"goto": NodeName`, the container node's default route is overridden; other keys are treated as part of `Command(update=...)`

## Key Dependencies

- [[state.ava.okf.md]] — The hook's `state` parameter is the `AgentState` passed by LangGraph; the returned dict goes through standard LangGraph reducer merging; hooks do **not** go through `PluginStateHandle`—that is the channel for agent code inside the exec thread to read/write plugin state (`ava.state` / `ava.state_update` are only injected inside exec_node), whereas hooks run at the graph-node level and directly receive/return the whole state
- [[agent/graph/graph.ava.okf.md]] — Placement of hook container nodes in the 8-node topology

## Entry Points

- `agent/hooks/_registry.py:Hook` — Base class; subclasses override `__call__`
- `agent/hooks/_registry.py:register_before_llm()` / `register_before_exec()` / `register_after_exec()` / `register_after_init()` — Accept `Hook` instances
- `agent/hooks/_registry.py:make_hook_runner()` — Called at graph build time
- `agent/hooks/__init__.py` — Public API re-exports (`Hook`, `HookName`, `HOOKS`, three `register_*`, `make_hook_runner`)

## Notes

- **after_init** is the earliest hook point: its container node sits right after `START` and before `init_context`, so its state edits land before the standing message head is laid down (see [[agent/graph/graph.ava.okf.md]] for placement). Example: the ava_code plugin registers `sync_cwd_after_init` here.
- A hook returning `None` means "no modification"—it produces no state update
- Hook signatures are uniform, not distinguishing between nodes—the same `Hook` subclass instance can theoretically be registered at multiple hook points
- Built-in compact (`agent/hooks/compact.py:_AutoCompactHook`, registered via `register_compact_hooks()` from `build_graph()`) is a built-in before_llm hook: when approaching the context limit, it force-compacts via automatic summarization (if the agent actively calls `ava.self.compact()`, it writes a compact_summary inbound which claim directly replaces messages)
- Built-in repair (`agent/hooks/repair.py:_RepairDanglingToolUseHook`, `register_repair_hooks()`) is also an unconditionally registered built-in before_llm hook, deliberately registered **before compact**—the dangling tool_use message history it repairs is exactly the input that later hooks (e.g., force-compact's summarization call) might feed into the LLM
- Built-in capability-index drift (`agent/hooks/capabilities.py:_NewlyInstalledSkillsHook`, `register_capabilities_hooks()`) is the third unconditional built-in before_llm hook, registered **last**—it appends a note to whatever history survives repair's guard and compact's possible full replacement. It names the skills installed since the `# Capabilities` index was rendered, which is a snapshot while the skill catalog is a live filesystem scan; drift against `state.capabilities.indexed` is the trigger, and the snapshot advances with the note so one install is named once. It **defers** (returns `None`, writing nothing) on a pass where `auto_compact_will_fire(state)` — the shared gate the reminder plugins also call: `add_messages` applies compaction's `REMOVE_ALL` and then the append, so a note written alongside it would survive as the window's only message and make `init_context` mistake it for an intact history, dropping the parked summary and the SystemMessage with it. Deferring loses nothing—the compaction routes through `init_context`, which rebuilds the index from the catalog that now contains those skills. Also suppressed in container/eval mode (`ops_pool is None`). See [[agent/graph/system-prompt.ava.okf.md]]
- Plugin hooks are also `Hook` subclasses (e.g., `plugins/ava_sdk_reminder/plugin.py:_SdkReminderAfterExecHook`, `plugins/ava_code/plugin.py:_InjectCwdNotesAfterExecHook`, `plugins/ava_memory`'s recall hook, etc.), written in the same style as built-in hooks—plugin authors don't need to care about built-in vs plugin distinction
- The permission hook example (`demos/permission-hooks/sensitive_op_gate.py:_SensitiveOpGateHook`) demonstrates the hook system's access control application: two-stage interception (block/warn), intercepting sensitive operations (force push, delete files, external sends, etc.) in before_exec; when blocking, returns `goto: "after_exec"` to skip the exec node
