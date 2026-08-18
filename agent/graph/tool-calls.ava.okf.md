---
type: doc
title: Tool Calls & Code Execution
description: Ava agent's tool invocation and code execution layer—including normalization of LLM-output tool calls (`_tool_calls.py`) and Python sandbox execution (`_exec.py`). Follows
tags: []
---

# Tool Calls & Code Execution

## What it is

Ava agent's tool invocation and code execution layer—including normalization of LLM-output tool calls (`_tool_calls.py`) and Python sandbox execution (`_exec.py`). Follows the single-tool architecture: all tool calls ultimately normalize to `execute_code`.

## Core Responsibilities

### Tool Call Normalization (`_tool_calls.py`)
- Tool calls come from LangChain's native `AIMessage.tool_calls` (result of `bind_tools`, type `langchain_core.messages.ToolCall`), **not** from parsing XML/`<invoke>` tags in text
- **Multiple tool_use merging**: Sometimes the model emits multiple `tool_use` blocks in a single AIMessage—`merge_multiple_execute_code_tool_calls()` concatenates each code segment in the LLM's original order with `\n\n` into a single snippet, keeping only the first tool_call_id (and syncing the tool_use blocks in content)
- Prevents the next round from having the provider reject the whole history due to a missing tool_result
- **`code` parameter extraction** in two strictness levels sharing the same type contract: `code_from_args(args, source=)` strict version (raises if `args` is not dict or `code` is not str), used by the exec path (merge) and log lines; `first_tool_call_code(tool_calls)` loose version (returns `""` if not obtainable), used by before_llm/before_exec hooks for "no code, so skip" checks, avoiding repeated hand-written `tool_calls[0]["args"].get("code")`

### Code Execution (`_exec.py`)
- **Worker thread**: `_run_in_thread` starts a worker thread in the main process to run `exec(compile(code))`; the main task polls the thread's liveness/cancel/deadline every 50ms
- **Cancel/timeout**: Injects `KeyboardInterrupt` / `TimeoutError` into the thread via `ctypes.pythonapi.PyThreadState_SetAsyncExc` (CPython C API, taking effect at the next Python bytecode boundary)
- **Lifecycle exits**: Agent code raises `AgentTermination` / `AgentRestart` / `_SystemHalt` → exec_node recognizes and writes halted + marker
- **Streaming output**: stdout/stderr redirected to `StreamingTextIO`, main task pushes to Redis every 50ms (frontend streaming display), preserving timing order
- **Result type**: `_exec_with_cancel_event` returns a sum type (`_ExecDone | _ExecCancelled | _ExecTimedOut | _ExecLifecycle | _ExecCrashed`), dispatched by exec_node via `match`

## Key Dependencies

- [[llm.ava.okf.md]] — LLM-generated tool_calls as input
- [[state.ava.okf.md]] — Execution results written as ToolMessage into state
- [[sse.ava.okf.md]] — Redis streaming output push

## Entry Points

- `agent/graph/_tool_calls.py:merge_multiple_execute_code_tool_calls()` — Multiple tool_call normalization
- `agent/graph/_exec.py:exec_node()` — Execution node
- `agent/graph/_exec.py:_run_in_thread()` — Worker thread launch

## Notes

- When pure native code is stuck, the thread does not respond to ctypes cancellation—set `daemon=True`, and the OS cleans up on main process exit
- **Agent code must explicitly `import ava`**: `fresh_globals` no longer pre-sets `ava` (declared in `execute_code` docstring). The framework's main process already imported `ava` at startup; worker threads importing `ava.X` hit the frozen snapshot in `sys.modules`—changes to ava/*.py on disk do not affect running copies
- Design choice: subprocess → in-process thread to reduce process overhead
