---
type: doc
title: Plugin Graph-Edge Hooks
description: The four hook container nodes in the LangGraph graph (after_init / before_llm / before_exec / after_exec), the Hook ABC instance-registration contract, and reducer-aware state merge.
tags:
- plugins
- hooks
---

# Plugin Graph-Edge Hooks

## Graph-Edge Hooks (`agent/hooks/_registry.py`)
Four hook container nodes sit in the LangGraph execution graph (topology: START → after_init → init_context → claim → before_llm → llm → before_exec → exec → after_exec → claim):
- `after_init` — runs once after the checkpoint is loaded, before `claim` (e.g., restores `os.chdir` on agent restart; introduced 2026-07-23, used by ava_code's `register_after_init(sync_cwd_after_init)`)
- `before_llm` — after claim completion, before LLM invocation
- `before_exec` — after LLM completion, before code execution
- `after_exec` — after code execution completion, before the next node

Plugins register an **instance** of a `Hook` ABC subclass (PyTorch `nn.Module` shape—base class locks the signature, subclasses fill the body), rather than a bare async function + decorator:
```python
from agent.hooks import Hook, register_before_llm

class MyHook(Hook):
    async def __call__(self, state, runtime, config, /) -> dict | None:
        return None  # or return a dict for state update

register_before_llm(MyHook())
```

The signature is inherited and checked by pyright strict's `reportIncompatibleMethodOverride`—narrowing parameter types / widening return value / missing parameters are caught at type-checking time, no longer just a convention described in a Protocol. The four hook points share the same `Hook` base class (same signature); the only difference is which list they are registered into. Instances can carry per-hook state in `__init__`. Returning a dict can modify state; returning None is a no-op. When two hooks write the same key in the same round, the merge is **reducer-aware** (`agent/hooks/_registry.py:202-229`): keys with reducers (e.g., `messages`→`add_messages`) merge both values; **only keys without reducers raise RuntimeError** (avoiding silent overwrites).


Parent: [[okf/plugins/plugins.ava.okf.md|Plugin System]].
