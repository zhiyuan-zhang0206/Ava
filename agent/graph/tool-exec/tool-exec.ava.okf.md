---
type: doc
title: Tool Execution — Fault-Isolated Code Execution
description: 'The agent''s sole tool—`execute_code(code: str)`—executes Python code in a disposable child process. This is the agent''s only way to interact with the outside world.'
tags: []
---

# Tool Execution — Fault-Isolated Code Execution

## What it is

The agent's sole tool—`execute_code(code: str)`—executes Python code in a disposable child process. This is the agent's only way to interact with the outside world. The process boundary provides cancellation and fault isolation; it is not a security sandbox.

## Core Mechanism

### Execution Flow (`_exec.py`)
- The LLM outputs an `execute_code` tool_call (LangChain native `AIMessage.tool_calls`, normalized by `_tool_calls.py`), extracting `args["code"]`
- `_run_in_subprocess` (`_exec_subprocess.py`) first verifies the current interpreter's editable-install records. Poison is repaired before any request file or child exists, and that call returns a structured retryable crash; an unrepairable guard failure tells the agent not to retry. It then spawns one disposable child per exec (`python -I -X utf8 -m agent.exec_child`, same venv) — isolated mode keeps the inherited process cwd and `PYTHON*` environment out of trusted bootstrap import resolution, and explicit UTF-8 mode preserves portable text after `-I` ignores encoding env vars; a stuck native call is hard-killable without touching the agent process (issue #184); the parent polls every 50ms. POSIX gives the child its own process group. Windows gates child entry until the parent attaches a `KILL_ON_JOB_CLOSE | BREAKAWAY_OK` Job Object. Persistent `ava.shell.sessions` add `CREATE_BREAKAWAY_FROM_JOB` only in this verified exec context, so they survive domain close
- Double-layered hard timeout: per-code `exec_timeout_seconds` (default 300s) + graph-level `exec_node_timeout_seconds` (default 1200s, defense in depth — the parent-side `asyncio.wait_for` above the whole run). Every path has one non-reaping root-exit observer, one domain-close owner, one `Popen.wait`, and one reader-join task. POSIX cancel/timeout runs signal → grace → hard process-group close; Windows immediately closes the Job. Natural exit and both stop paths then close domain → reap → bounded pipe-reader join → envelope cleanup, covering `os._exit` and watchdog exits that bypass child cleanup. On POSIX the zombie root pins its pid/pgid until group close, so cleanup never targets a recycled numeric process group. Ordinary outer-task cancellation crosses the asynchronous barrier; if signal-driven `asyncio.Runner` shutdown has already cancelled those resource-owner tasks, a bounded synchronous barrier closes the process domain, reaps the direct child, and joins the reader without submitting new default-executor work
- The child writes stdout/stderr line-buffered onto the pipe (chronological merge); the parent drains the pipe into a `StreamingTextIO` (`_exec_stream.py`) and streams chunks to Redis in real time. While a live child is silent, the same parent poll loop emits an empty SSE-only keepalive after each 0.5s without output so a frontend that joined mid-exec can materialize the running output block; keepalives never enter the accumulator, child envelopes, or LangGraph messages. The parent finally returns the unchanged accumulator envelope to the LLM
- `exec_child_boot` measures child setup after imports, immediately before user code. Parent timing covers the complete exec lifecycle.
- `ava.self.attach()` uses the plugin-state result envelope. The parent validates metadata into `state.attach`; claim reads bytes at the completed-turn boundary.
- **Accumulation cap** (`exec_output_accumulation_max_chars`, default 1,000,000): the accumulator pins its first half and rolls its last half, dropping the middle **as the code runs**, so a runaway `print` loop cannot grow the child until it is OOM-killed. Execution is **not** killed — the output is truncated with an explicit marker and the model self-corrects; the result taxonomy is unchanged. Redis pushes come off the same accumulator so they inherit the bound: past the budget the retained text is a rolling window with no append-only increment, so one notice goes out and streaming stops until the final `ExecOutput` upsert

### Execution Environment
- Each execution uses a fresh `fresh_globals` (`__name__="__agent_code__"`)
- `ava.*` SDK is available, but **not auto-imported**—agent code must explicitly `import ava`
- `ava.cwd` is logical plugin state, not a process-global `chdir`: AvaCode's `files` / `shell` / `understand` wrappers resolve it explicitly, while bare `open`, `Path.cwd`, imports, and user subprocesses retain the disposable child's stable OS cwd

### Output Handling
- `agent/graph/_exec_output.py:wrap_code_output()` wraps the result (cancelled/timed_out markers)
- Configurable soft line previews and the two existing hard caps have distinct retention contracts; see [[agent/graph/tool-exec/output-preview.ava.okf.md]]. Soft archives protect current context references within a byte budget; legacy hard-overflow files retain their 20-file ring
- Results are dispatched by sum type (`_ExecDone|_ExecCancelled|_ExecTimedOut|_ExecLifecycle|_ExecCrashed`): runtime imports occur under the child entry guard, so boot/config and user-code exceptions become a `crashed` envelope with their traceback; lifecycle exceptions (terminate/restart/compact) take highest priority

### In-memory system-note injection (`_exec_notes.py`, user ruling 2026-08-11)
- AGENTS.md / CLAUDE.md context notes (ava_code plugin) and prompt-injection security findings (ava.security) are delivered **inside the exec's own messages delta** — no side-channel file
- `agent/graph/_exec_notes.py:merge_exec_notes()` appends both after the exec-result ToolMessage: the Anthropic-compat wire contract requires `tool_use` to be immediately followed by `tool_result` (verified against the DeepSeek anthropic endpoint 2026-08-11), so notes must not be sandwiched between the AIMessage and its ToolMessage; the compact path (`_SystemHalt`) drops both (history is REMOVE_ALL'd anyway)

## Key Dependencies

- [[sdk-surface.ava.okf.md]] — The agent calls all `ava.*` tools through `execute_code`
- [[agent/graph/graph.ava.okf.md]] — `_exec_node_impl` is the second node of the execution graph
- [[system-prompt.ava.okf.md]] — The system prompt specifies output format (text + optional execute_code)

## Entry Points

- `agent/graph/_exec.py:exec_node()` — Code execution main logic + sum type dispatch
- `agent/graph/_exec_stream.py` — `StreamingTextIO` + `ExecOutputChunkPublisher` (streaming output incremental push)
- `agent/graph/_tool_calls.py` — Multiple tool_call normalization (merging, not parsing)
- `agent/graph/_exec_output.py` — Output envelope formatting
- `agent/graph/_exec_crop.py` — Soft line previews and bounded, reference-protected archives

## Notes

- Hosted resource completion: [[hosted-quiescence.ava.okf.md]].
- The single-tool design principle: like a person with one pair of hands but can pick up any tool
- Avoids the heavy escaping issues of JSON mode
- Long-running operations should not go through `execute_code`: one-time long commands use `ava.shell.run_background` (auto-reports on completion), interactive/long-lived processes use `ava.shell.sessions` persistent sessions
