---
type: doc
title: LLM Interface
description: Ava agent's LLM invocation layer — tool schema definition (`agent/llm/__init__.py`) and LLM streaming reasoning node (`agent/graph/llm/node.py`). single-tool architecture, streaming-first with one non-streaming fallback.
tags: []
---

# LLM Interface

## What it is

Ava agent's LLM invocation layer — containing tool schema definition (`agent/llm/__init__.py`) and the LLM streaming reasoning node (`agent/graph/llm/node.py`). Follows a **single-tool** architecture: only one `execute_code` tool, all capabilities accessible via Python namespace.

## Core responsibilities

- **Tool Schema** (`agent/llm/__init__.py`): `@tool("execute_code")` defines `execute_code(code: str) -> str`, consumed by `bind_tools` via its name + docstring + arg types. Docstring explicitly states that `ava` is no longer auto-imported — agent must explicitly `import ava`
- **LLM Node** (`agent/graph/llm/node.py`): streaming LLM reasoning
  - Normal (has tool_call): `Command(goto="before_exec")`
  - After the final message is assembled, `agent/hooks/understanding_chunks.py:due_chunk_update` may enqueue an understanding chunk (provider `input_tokens` past the segment's previous cut by `AVA_UNDERSTANDING_CHUNK_TOKENS`) and returns the moved cut as a `compact` update merged into whichever command ends the turn; off unless `AVA_UNDERSTANDING_ENABLED` — see [[base/agents/history/hierarchy/docs/chunks.ava.okf.md]]
  - No tool_call stop-turn / cancel: `Command(update={halted: True}, goto="after_exec")` (returns to claim to wait for next inbound, process does not exit)
- **streaming-first + one non-streaming fallback** (`agent/graph/llm/_stream.py:_consume_llm`): first `astream`, on hitting one of two recoverable error classes, degrades to a single `ainvoke` HTTP (bypassing the SSE event layer): `LLMStreamStallTimeoutError` (TTFT / inter-chunk timeout) or a configured fatal provider error type (e.g., Kimi K3 `engine_overloaded_error`); `LLMStreamCorruptedError` is no longer a trigger (thinking-drift is repaired in place). Fallback runs only once; that turn loses progressive UI streaming. A stall-triggered fallback runs under the SAME bound as its stream segment (one key, one value: `llm_stream_ttft_timeout_seconds`); when it also times out, the call ends as `LLMStreamStallPairError` — two adjacent stalls — which retries on the delayed stall schedule below, not the generic transient one. `stream_stalled_retry` (vendor/model/stage/elapsed_s) and `stream_stall_pair_terminated` (vendor/model/stage/timeout_s) make stalls countable per provider
- **provider exception classification** (`base/lm/errors.py:classify_error`): official LangChain `ModelError.is_retryable` and actual SDK types distinguish trusted `TRANSIENT` failures from deterministic `PERMANENT` rejections. `UNKNOWN` errors propagate once unchanged; unrelated attributes or arbitrary causes cannot grant retry authority. Raw HTTPX transport is normalized only at the model invoke/iterator await, excluding output callbacks and unrelated services. `emit_provider_error` records the shared classification and billing signal; the graph applies its configured fatal-provider policy.
- **fatal error fast-fail** (no retry, go idle to stay alive): `FatalProviderError` (PERMANENT classification or fatal error type, after both streaming and non-streaming fail; carries structured `error_class`/`provider`/`status`), `FatalLLMStreamError` (owned stream stall consecutively hitting `llm_retry_max_consecutive_same_error` cap, default 3), `LLMStreamStallPairExhaustedError` (the delayed stall-retry budget — default 4 consecutive stall pairs — is spent; raised at node entry), and `LLMRetryBudgetExceededError` (the 420-second retry budget was already exhausted before a subsequent attempt) are excluded from the node's retry loop; after exiting the graph, agent loop emits an ERROR event and idles. A permanent non-overflow rejection also carries its classification, reason, and user recovery action to the live UI, opens the heartbeat circuit, and sends a metadata-only `system_note` to the nearest alive immutable-`spawner` ancestor; it never forwards the rejected history or provider body. Context overflow stays on its forced-compaction self-recovery path and is not escalated.
- **streaming validation**: `_sanitize_thinking_blocks` repairs the established missing-thinking-field shape in place. Missing terminal fields, abnormal stop reasons and truncation raise once without automatic re-streaming; the failed turn remains visible.
- **typed content block reading**: `AIMessage(Chunk).content`'s list branch is flattened by langchain into `list[str | dict[str, Any]]`, `base/lm/content.py:content_blocks()` re-tags it as `ContentBlock` (`TypedDict, total=False`), validators like `_validate_thinking_blocks` use it for block-type traversal instead of bare `.get()`; similarly, `additional_kwargs`'s `ava_*` fields via `base/agents/messages/kwargs.py:read_ava_kwargs()` obtain typed views (see [[messages.ava.okf.md]])
- **silent-idle handling**: when only reasoning is present, no text, no tool_call, reasoning is kept and a continue-loop entered (`halted=False`, delegated to `ava_silent_idle` plugin injected nudge). Consecutive silent idles consume output-token budget units; a provider-reported zero-output turn still consumes one unit, so the 2,048-token default (`llm_silent_idle_max_output_tokens`) halts before another model call while the event retains its actual estimated output cost.
- **streaming output**: AIMessage text goes through two paths — real-time (RedisStreamHandler → ChatStart/ChatDelta → UI) and persistence (LangGraph state.messages → timeline endpoint)
- **cancel-race tests**: `tests/cancel_fixture.py` is opt-in and bound beside its real LLM, exec and compact consumers, rather than in the root plugin roster. Tests retain real node logic and restore subscription patches at teardown.
- **interrupt handling**: `agent/graph/llm/_cancel.py:_race_stream_vs_cancel` — `subscribe_interrupt` RAII, node entry listens for cancel/terminate inbound, `asyncio.wait` races streaming task; cancel discards partial generation of current turn, not entering history

## Key dependencies

- [[tool-calls.ava.okf.md]] — LLM-generated code executed by exec node
- [[system-prompt.ava.okf.md]] — dynamic assembly of system prompt
- [[agent/docs/state.ava.okf.md]] — AIMessage stored into state.messages
- [[agent/db/docs/db.ava.okf.md]] — Redis pub/sub wakeup for inbound messages

## Entry points

- `agent/llm/__init__.py:execute_code` — tool schema definition
- `agent/graph/llm/node.py:llm_node()` — LLM node (`_llm_node_impl` is the implementation body)
- `agent/graph/llm/_stream.py:_consume_llm()` — unified streaming/non-streaming entry
- `base/lm/errors.py:classify_error()` — cross-provider classification of provider exceptions (transient/permanent/unknown)
- `base/lm/factory.py:build_chat_model()` — ChatModel factory

## Notes

- State type hint uses `from __future__ import annotations` + module attribute references to avoid capturing `BaseAgentState` alias and losing plugin dynamic fields
- The node's retry loop (`agent/graph/llm/node.py:llm_node` around `llm_attempt`) decides each wait in `agent/graph/llm/_retry.py:retry_wait`, reading from `settings.llm_retry_*` (default max_attempts=6 / initial=30s / max=480s / total=420s / backoff=2). It clips waits to the remaining total budget and emits `llm_retry.duration_seconds` when a retry sequence succeeds or exhausts its attempt/time budget; fatal stream/provider errors remain excluded. Stall pairs instead run on a separate delayed schedule (`llm_stall_retry_*`): initial 300s doubling to a 1800s cap, at most 4 consecutive pairs, every wait jittered ±25% so fleet retries do not re-synchronize; the transient budget/attempts gates do not apply within it.
