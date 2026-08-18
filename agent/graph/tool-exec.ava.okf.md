---
type: doc
title: Tool Execution — Code Execution Sandbox
description: 'The agent''s sole tool—`execute_code(code: str)`—executes Python code in a sandbox. This is the agent''s only way to interact with the outside world.'
tags: []
---

# Tool Execution — Code Execution Sandbox

## What it is

The agent's sole tool—`execute_code(code: str)`—executes Python code in a sandbox. This is the agent's only way to interact with the outside world.

## Core Mechanism

### Execution Flow (`_exec.py`)
- The LLM outputs an `execute_code` tool_call (LangChain native `AIMessage.tool_calls`, normalized by `_tool_calls.py`), extracting `args["code"]`
- `_run_in_thread` starts a **worker thread** (not a subprocess) inside the main process to run `exec(compile(code))`; the main task polls liveness/cancel/deadline every 50ms
- Double-layered hard timeout: per-code `exec_timeout_seconds` (default 300s) + graph-level `exec_node_timeout_seconds` (default 1200s, defense in depth); both inject `TimeoutError` via ctypes `PyThreadState_SetAsyncExc`
- **Orphan reaper** (`_exec_threads.py`, Task #1058): a thread that survives the injection — stuck in a native call like `time.sleep`, or code that swallows `Exception` (TimeoutError is one) — is orphaned after the 2s join grace; a bounded daemon reaper then re-injects `KeyboardInterrupt` every 1s for up to 60s. It is a `BaseException`, so `except Exception` swallow loops cannot survive it; only infinite native loops / `except BaseException` loops leak to process exit (daemon, freed at exit)
- stdout+stderr are combined into the same `StreamingTextIO` (`_exec_stream.py`), preserving order; streamed to Redis in real-time, finally returned to the LLM

### Sandbox Environment
- Each execution uses a fresh `fresh_globals` (`__name__="__agent_code__"`)
- `ava.*` SDK is available, but **not auto-imported**—agent code must explicitly `import ava`
- The agent's working directory (workspace) is sandboxed

### Output Handling
- `_exec_output.py:wrap_code_output()` wraps the result (cancelled/timed_out markers)
- Long output `_exec_output.py:truncate_both_ends` **keeps the head and tail, drops the middle** (each half `max_chars//2`); the full text is saved to a workspace `.exec_output/` file ring (keeping the last 20 copies, `_OVERFLOW_KEEP=20`) for grep
- Results are dispatched by sum type (`_ExecDone|_ExecCancelled|_ExecTimedOut|_ExecLifecycle|_ExecCrashed`): user code exceptions **do not** raise; they are returned as tracebacks for the agent to judge; lifecycle exceptions (terminate/restart/compact) take highest priority

### In-memory system-note injection (`_exec_notes.py`, user ruling 2026-08-11)
- AGENTS.md / CLAUDE.md context notes (ava_code plugin) and prompt-injection security findings (ava.security) are delivered **inside the exec's own messages delta** — no side-channel file
- `_exec_notes.py:merge_exec_notes()` appends both after the exec-result ToolMessage: the Anthropic-compat wire contract requires `tool_use` to be immediately followed by `tool_result` (verified against the DeepSeek anthropic endpoint 2026-08-11), so notes must not be sandwiched between the AIMessage and its ToolMessage; the compact path (`_SystemHalt`) drops both (history is REMOVE_ALL'd anyway)

## Key Dependencies

- [[sdk-surface.ava.okf.md]] — The agent calls all `ava.*` tools through `execute_code`
- [[agent/graph/graph.ava.okf.md]] — `_exec_node_impl` is the second node of the execution graph
- [[system-prompt.ava.okf.md]] — The system prompt specifies output format (text + optional execute_code)

## Entry Points

- `agent/graph/_exec.py:exec_node()` — Sandbox execution main logic + sum type dispatch
- `agent/graph/_exec_stream.py` — `StreamingTextIO` + `ExecOutputChunkPublisher` (streaming output incremental push)
- `agent/graph/_tool_calls.py` — Multiple tool_call normalization (merging, not parsing)
- `agent/graph/_exec_output.py` — Output envelope formatting

## Notes

- The single-tool design principle: like a person with one pair of hands but can pick up any tool
- Avoids the heavy escaping issues of JSON mode
- Long-running operations should not go through `execute_code`: one-time long commands use `ava.shell.run_background` (auto-reports on completion), interactive/long-lived processes use `ava.shell.sessions` persistent sessions
