---
type: doc
title: LLM Interface
description: Ava agent's LLM invocation layer — tool schema definition (`agent/llm.py`) and LLM streaming reasoning node (`agent/graph/_llm.py`). single-tool architecture, streaming-first with one non-streaming fallback.
tags: []
---

# LLM Interface

## What it is

Ava agent's LLM invocation layer — containing tool schema definition (`agent/llm.py`) and the LLM streaming reasoning node (`agent/graph/_llm.py`). Follows a **single-tool** architecture: only one `execute_code` tool, all capabilities accessible via Python namespace.

## Core responsibilities

- **Tool Schema** (`agent/llm.py`): `@tool("execute_code")` defines `execute_code(code: str) -> str`, consumed by `bind_tools` via its name + docstring + arg types. Docstring explicitly states that `ava` is no longer auto-imported — agent must explicitly `import ava`
- **LLM Node** (`agent/graph/_llm.py`): streaming LLM reasoning
  - Normal (has tool_call): `Command(goto="before_exec")`
  - No tool_call stop-turn / cancel: `Command(update={halted: True}, goto="after_exec")` (returns to claim to wait for next inbound, process does not exit)
- **streaming-first + one non-streaming fallback** (`agent/graph/_llm_stream.py:_consume_llm`): first `astream`, on hitting one of three recoverable error classes, degrades to a single `ainvoke` HTTP (bypassing the SSE event layer): `LLMStreamCorruptedError` (SSE event loss), `LLMStreamStallTimeoutError` (TTFT / inter-chunk timeout), configured fatal provider error type (e.g., Kimi K3 `engine_overloaded_error`). Fallback runs only once; that turn loses progressive UI streaming
- **provider exception classification** (`shared/lm/errors.py:classify_error`, cross-provider normalized, same kind as `stop.py`): maps provider SDK exceptions to `ErrorClass` — `TRANSIENT` (429/5xx/408/409/425/network transport errors, retried with RetryPolicy), `PERMANENT` (400 context too long/schema, 401/402/403 auth/billing/forbidden, 404 unknown model, 422 schema, fast-fail to idle), `UNKNOWN` (unrecognized exceptions not guessed, propagate and retry as usual then surface). `shared/lm/errors.py:emit_provider_error` records every classified streaming or synchronous SDK failure as `llm_provider_error`; the graph adds its permanent/fatal policy and throws `FatalProviderError` when a turn must stop. The event carries `vendor`/`model`, so Grafana separately alerts a provider-grouped 429 burst and a first-occurrence billing/quota failure.
- **fatal error fast-fail** (no retry, go idle to stay alive): `FatalProviderError` (PERMANENT classification or fatal error type, after both streaming and non-streaming fail; carries structured `error_class`/`provider`/`status`), `FatalLLMStreamError` (same error consecutively hitting `llm_retry_max_consecutive_same_error` cap, default 3), and `LLMRetryBudgetExceededError` (the 420-second retry budget was already exhausted before a subsequent attempt) are excluded from `_build.py`'s RetryPolicy; after exiting the graph, agent loop emits an ERROR event and idles. A permanent non-overflow rejection also carries its classification, reason, and user recovery action to the live UI, opens the heartbeat circuit, and sends a metadata-only `system_note` to the nearest alive immutable-`spawner` ancestor; it never forwards the rejected history or provider body. Context overflow stays on its forced-compaction self-recovery path and is not escalated.
- **streaming validation**: `agent/graph/_llm_chunk.py:_sanitize_thinking_blocks` (thinking block missing fields), `_validate_stop_reason` (missing/abnormal stop reason, truncation) — fail-fast, let LangGraph retry the entire llm_node
- **typed content block reading**: `AIMessage(Chunk).content`'s list branch is flattened by langchain into `list[str | dict[str, Any]]`, `shared/lm/content.py:content_blocks()` re-tags it as `ContentBlock` (`TypedDict, total=False`), validators like `_validate_thinking_blocks` use it for block-type traversal instead of bare `.get()`; similarly, `additional_kwargs`'s `ava_*` fields via `shared/message_kwargs.py:read_ava_kwargs()` obtain typed views (see [[messages.ava.okf.md]])
- **silent-idle handling**: when only reasoning is present, no text, no tool_call, reasoning is kept and a continue-loop entered (`halted=False`, delegated to `ava_silent_idle` plugin injected nudge). Consecutive silent idles consume output-token budget units; a provider-reported zero-output turn still consumes one unit, so the 2,048-token default (`llm_silent_idle_max_output_tokens`) halts before another model call while the event retains its actual estimated output cost.
- **streaming output**: AIMessage text goes through two paths — real-time (RedisStreamHandler → ChatStart/ChatDelta → UI) and persistence (LangGraph state.messages → timeline endpoint)
- **interrupt handling**: `agent/graph/_llm_cancel.py:_race_stream_vs_cancel` — `subscribe_interrupt` RAII, node entry listens for cancel/terminate inbound, `asyncio.wait` races streaming task; cancel discards partial generation of current turn, not entering history

## Key dependencies

- [[tool-calls.ava.okf.md]] — LLM-generated code executed by exec node
- [[system-prompt.ava.okf.md]] — dynamic assembly of system prompt
- [[state.ava.okf.md]] — AIMessage stored into state.messages
- [[db.ava.okf.md]] — Redis pub/sub wakeup for inbound messages

## Entry points

- `agent/llm.py:execute_code` — tool schema definition
- `agent/graph/_llm.py:llm_node()` — LLM node (`_llm_node_impl` is the implementation body)
- `agent/graph/_llm_stream.py:_consume_llm()` — unified streaming/non-streaming entry
- `shared/lm/errors.py:classify_error()` — cross-provider classification of provider exceptions (transient/permanent/unknown)
- `shared/lm/factory.py:build_chat_model()` — ChatModel factory

## Notes

- State type hint uses `from __future__ import annotations` + module attribute references to avoid capturing `BaseAgentState` alias and losing plugin dynamic fields
- Retry policy is built in `agent/graph/_build.py:_build_llm_retry()` reading from `settings.llm_retry_*` (default max_attempts=6 / initial=30s / max=480s / total=420s / backoff=2). It clips waits to the remaining total budget and emits `llm_retry.duration_seconds` when a retry sequence succeeds or exhausts its attempt/time budget; fatal stream/provider errors remain excluded.
