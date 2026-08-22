---
type: doc
title: Tool Calls & Code Execution
description: Ava agent's tool invocation and fault-isolated code execution layer—including normalization of LLM-output tool calls (`_tool_calls.py`) and disposable child execution (`_exec.py`).
tags: []
---

# Tool Calls & Code Execution

## What it is

Ava agent's tool invocation and code execution layer—including normalization of LLM-output tool calls (`_tool_calls.py`) and disposable child execution (`_exec.py`). The child is a fault-isolation boundary, not a security sandbox. The layer follows the single-tool architecture: all tool calls ultimately normalize to `execute_code`.

## Core Responsibilities

### Tool Call Normalization (`_tool_calls.py`)
- Tool calls come from LangChain's native `AIMessage.tool_calls` (result of `bind_tools`, type `langchain_core.messages.ToolCall`), **not** from parsing XML/`<invoke>` tags in text
- **Multiple tool_use merging**: Sometimes the model emits multiple `tool_use` blocks in a single AIMessage—`merge_multiple_execute_code_tool_calls()` concatenates each code segment in the LLM's original order with `\n\n` into a single snippet, keeping only the first tool_call_id (and syncing the tool_use blocks in content)
- Prevents the next round from having the provider reject the whole history due to a missing tool_result
- **`code` parameter extraction** in two strictness levels sharing the same type contract: `code_from_args(args, source=)` strict version (raises if `args` is not dict or `code` is not str), used by the exec path (merge) and log lines; `first_tool_call_code(tool_calls)` loose version (returns `""` if not obtainable), used by before_llm/before_exec hooks for "no code, so skip" checks, avoiding repeated hand-written `tool_calls[0]["args"].get("code")`

### Code Execution (`_exec.py`)
- **Disposable subprocess**: `_run_in_subprocess` spawns one child (`python -I -X utf8 -m agent.exec_child`) per exec; isolated mode prevents the inherited process cwd or `PYTHON*` environment from shadowing the trusted `agent.exec_child` entry, while explicit UTF-8 mode keeps text portable after `-I` ignores `PYTHONUTF8` / `PYTHONIOENCODING`. The child OS cwd is not changed by `ava.cwd`. The parent polls liveness/cancel/deadline every 50ms and escalates SIGINT (cancel) / SIGTERM (timeout) → SIGKILL(-pgid) after a grace period on POSIX; Windows termination currently targets the direct child only
- **Lifecycle exits**: Agent code raises `AgentTermination` / `AgentRestart` / `_SystemHalt` → the child reports the exception name in the result envelope → exec_node recognizes and writes halted + marker
- **Streaming output**: the child writes stdout/stderr line-buffered onto the pipe; the parent drains into `StreamingTextIO` and pushes to Redis every 50ms (frontend streaming display), preserving timing order
- **Result type**: `_run_in_subprocess` returns the sum type (`_ExecDone | _ExecCancelled | _ExecTimedOut | _ExecLifecycle | _ExecCrashed`) plus the raw child envelope, dispatched by exec_node via `match`

## Key Dependencies

- [[llm.ava.okf.md]] — LLM-generated tool_calls as input
- [[state.ava.okf.md]] — Execution results written as ToolMessage into state
- [[sse.ava.okf.md]] — Redis streaming output push

## Entry Points

- `agent/graph/_tool_calls.py:merge_multiple_execute_code_tool_calls()` — Multiple tool_call normalization
- `agent/graph/_exec.py:exec_node()` — Execution node
- `agent/graph/_exec.py:_run_agent_code()` — Exec run (one disposable child)

## Notes

- On POSIX, when pure native code is stuck, the child does not respond to SIGINT/SIGTERM—after the grace period the parent SIGKILLs the child's process group
- **Agent code must explicitly `import ava`**: `fresh_globals` no longer pre-sets `ava` (declared in `execute_code` docstring). The child is a fresh process that imports ava from disk — changes to ava/*.py on disk DO affect the child (unlike the old in-process thread's frozen `sys.modules` snapshot)
- Design choice: in-process thread → subprocess, so a stuck native call is killable without touching the agent process (issue #184)
