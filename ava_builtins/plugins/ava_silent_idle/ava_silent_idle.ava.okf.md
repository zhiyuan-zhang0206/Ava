---
type: doc
title: ava_silent_idle — Silent Idle Nudge Plugin
description: '`ava_silent_idle` nudges the agent when it produces a "silent idle" (has reasoning tokens but no text, no tool_call). Such
  turns look like the agent is stuck thinking — reasoning but no action.'
tags:
- extensions
- plugin
- agent-extension
---

# ava_silent_idle — Silent Idle Nudge Plugin

## What is it

`ava_silent_idle` nudges the agent when it produces a "silent idle" (has reasoning tokens but no text, no tool_call). Such turns look like the agent is stuck thinking — reasoning but no action.

## Registered Hooks

### before_llm hook

```python
class _SilentIdleContinueHook(Hook):
    async def __call__(
        self, state: AgentState, _runtime: Runtime[AvaContext], _config: RunnableConfig, /
    ) -> dict | None: ...

silent_idle_continue_before_llm = _SilentIdleContinueHook()
register_before_llm(silent_idle_continue_before_llm)
```

**Trigger conditions**:
- Check the tail of the conversation: the last message is an AIMessage, and it has no text content, no tool_calls, but has reasoning (thinking tokens)
- The `_tail_is_silent_idle()` function makes this determination

**Behavior**:
- Injects a `HumanMessage` (`_NUDGE`): "The previous turn produced reasoning but no output. You must now produce either text or a tool call. If your task is complete, state so in text — do not end a turn with reasoning alone."
- Consecutive silent idle has a counting limit (per-process consecutive-count guard in `agent/graph/_llm.py`); after exceeding the limit the kernel halts — perhaps the model truly cannot continue

**Mutual exclusion**:
- If auto-compact would also trigger on the same turn, defer (return None)
- Avoid two before_llm hooks writing `messages` simultaneously

## Key Dependencies

- [[agent/hooks/hooks.ava.okf.md]] — before_llm hook
- [[messages.ava.okf.md]] — message format (AIMessage/reasoning tokens)
- [[llm.ava.okf.md]] — LLM interface (source of reasoning tokens)

## Configuration

- Consecutive silent counting guard in the kernel (`agent/graph/_llm.py`)
- No additional plugin-level configuration

## Notes

- The kernel already handles the "no dropped token" retry for silent idle (halted=False → claim's multi-step continue); this plugin adds a **reminder** — a text nudge for the agent
- Accompanying kernel behavior: reasoning is kept in context, directly returning to the LLM loop, no need to waste tokens re-streaming
- This is a "one-shot" nudge — if the agent continues silent after the nudge, the kernel's counting guard takes over
