---
type: doc
title: Tool Calls & Code Execution
description: Ava agent's tool invocation and fault-isolated code execution layer—including normalization of LLM-output tool calls (`tool_calls.py`) and disposable child execution (`exec/node.py`).
tags: []
---

# Tool Calls & Code Execution

## What it is

Ava agent's tool invocation and code execution layer—including normalization of LLM-output tool calls (`tool_calls.py`) and disposable child execution (`exec/node.py`). The child is a fault-isolation boundary, not a security sandbox. The layer follows the single-tool architecture: only `execute_code` is registered; unknown tools receive their own error result.

## Core Responsibilities

### Tool Call Normalization (`tool_calls.py`)
- Tool calls come from LangChain's native `AIMessage.tool_calls` (result of `bind_tools`, type `langchain_core.messages.ToolCall`), **not** from parsing XML/`<invoke>` tags in text
- **One result per invocation**: `normalize_tool_calls()` recovers calls missing from LangChain's native list from structured provider content, retaining IDs, arguments and provider order. It never concatenates code or drops sibling tool-use blocks.
- **Independent execution**: `exec_node()` runs one invocation per graph step, sequentially in fresh children. It routes back to `exec` while original IDs lack committed results, then to `after_exec`. Each ID receives its own ToolMessage, timing, SDK-call tally and distinct streaming item ID. Ordinary errors and timeouts do not prevent later calls; cancellation and lifecycle exits stop the batch and return explicit not-executed results for remaining IDs. Compaction retains its history-reset path.
- **State and message order**: each invocation returns its own plugin delta directly to LangGraph; the existing channel reducers and checkpoint path commit it before the next child. There is no exec-local state copy, reducer replay or batch accumulator. All ToolMessages precede security notes, plugin context notes and attachments. The presentation-only `pending_exec_notes` channel holds these until the last result; normal completion drains it, compaction clears it, and crash repair drains it after pairing interrupted calls.
- **Code extraction and repair**: `code_from_args()` strictly reads code for execution/logging; `first_tool_call_code()` is the optional hook read. `replace_execute_code()` updates only the named call and its matching content block.

### Code Execution (`exec/node.py`)
- **Disposable subprocess**: `_run_in_subprocess` spawns one child (`python -I -X utf8 -m agent.exec_child`) per exec; isolated mode prevents the inherited process cwd or `PYTHON*` environment from shadowing the trusted `agent.exec_child` entry, while explicit UTF-8 mode keeps text portable after `-I` ignores `PYTHONUTF8` / `PYTHONIOENCODING`. The child OS cwd is not changed by `ava.cwd`. The parent polls liveness/cancel/deadline every 50ms. POSIX sends a signal then closes the process group after a grace period; Windows immediately closes a `KILL_ON_JOB_CLOSE` Job Object. Windows gates child entry until Job attach completes. A non-reaping root-exit observer, one domain-close owner, one direct-child reap, and a bounded pipe-reader join form the teardown barrier
- **Lifecycle exits**: Agent code raises `AgentTermination` / `AgentRestart` / `SystemHalt` → the child reports the exception name in the result envelope → exec_node recognizes and writes halted + marker
- **Streaming output**: the child writes stdout/stderr line-buffered onto the pipe; the parent drains into `StreamingTextIO` and pushes to Redis every 50ms (frontend streaming display), preserving timing order
- **Result type**: `_run_in_subprocess` returns the sum type (`_ExecDone | _ExecCancelled | _ExecTimedOut | _ExecLifecycle | _ExecCrashed`) plus the raw child envelope, dispatched by exec_node via `match`

## Key Dependencies

- [[llm.ava.okf.md]] — LLM-generated tool_calls as input
- [[agent/docs/state.ava.okf.md]] — Execution results written as ToolMessage into state
- [[sse.ava.okf.md]] — Redis streaming output push

## Entry Points

- `agent/graph/tool_calls.py:normalize_tool_calls()` — Multiple tool_call normalization
- `agent/graph/exec/node.py:exec_node()` — Execution node
- `agent/graph/exec/node.py:_run_agent_code()` — Exec run (one disposable child)

## Notes

- When pure native code is stuck on POSIX, the child does not respond to SIGINT/SIGTERM—after the grace period the parent hard-stops the process group (an explicit `setsid`/double-fork is outside the guarantee). Windows cancel/timeout closes the Job immediately; it covers members that remain in the Job Object. Persistent `ava.shell.sessions` explicitly break away from the Ava exec Job and therefore survive
- **Agent code must explicitly `import ava`**: `fresh_globals` no longer pre-sets `ava` (declared in `execute_code` docstring). The child is a fresh process that imports ava from disk — changes to ava/*.py on disk DO affect the child (unlike the old in-process thread's frozen `sys.modules` snapshot)
- Design choice: in-process thread → subprocess, so a stuck native call is killable without touching the agent process (issue #184)

- Rationale: [Tool-call concurrency stays in Python](../../../docs/decisions/2026-09-28-tool-call-concurrency-stays-in-python.md).
