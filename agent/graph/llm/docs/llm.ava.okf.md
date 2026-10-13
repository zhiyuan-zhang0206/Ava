---
type: doc
title: LLM Interface
description: Ava agent model invocation — one streaming request per try with bounded transient retry.
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
- **Streaming** (`agent/graph/llm/_stream.py:consume_llm`): one `astream` request per try, bounded by TTFT, inter-chunk and total deadlines. A stall ends the turn without a non-streaming fallback or delayed retry.
- **provider exception classification** (`base/lm/errors.py:classify_error`): official LangChain `ModelError.is_retryable` and actual SDK types distinguish trusted `TRANSIENT` failures from deterministic `PERMANENT` rejections. `UNKNOWN` errors propagate once unchanged; unrelated attributes or arbitrary causes cannot grant retry authority. Raw HTTPX transport is normalized only at the model invoke/iterator await, excluding output callbacks and unrelated services. `emit_provider_error` records the shared classification and billing signal; the graph applies its configured fatal-provider policy.
- **Failures**: permanent, protocol, stall and unknown errors end the current turn with diagnostics. Permanent rejections block background wakes; fresh chat or an explicit control can retry after the cause is fixed. Context overflow requires explicit compaction rather than a model-free history wipe.
- **streaming validation**: `_sanitize_thinking_blocks` repairs the established missing-thinking-field shape in place. Missing terminal fields, abnormal stop reasons and truncation raise once without automatic re-streaming; the failed turn remains visible.
- **typed content block reading**: `AIMessage(Chunk).content`'s list branch is flattened by langchain into `list[str | dict[str, Any]]`, `base/lm/reasoning.py:content_blocks()` re-tags it as `ContentBlock` (`TypedDict, total=False`), validators like `_validate_thinking_blocks` use it for block-type traversal instead of bare `.get()`; similarly, `additional_kwargs`'s `ava_*` fields via `base/agents/messages/kwargs.py:read_ava_kwargs()` obtain typed views (see [[messages.ava.okf.md]])
- **silent-idle handling**: when only reasoning is present, no text, no tool_call, reasoning is kept and a continue-loop entered (`halted=False`, delegated to `ava_silent_idle` plugin injected nudge). Consecutive silent idles consume output-token budget units; a provider-reported zero-output turn still consumes one unit, so the 2,048-token default (`llm_silent_idle_max_output_tokens`) halts before another model call while the event retains its actual estimated output cost.
- **streaming output**: AIMessage text goes through two paths — real-time (RedisStreamHandler → ChatStart/ChatDelta → UI) and persistence (LangGraph state.messages → timeline endpoint)
- **cancel-race tests**: `tests/cancel_fixture.py` is opt-in and bound beside its real LLM, exec and compact consumers, rather than in the root plugin roster. Tests retain real node logic and restore subscription patches at teardown.
- **interrupt handling**: `agent/graph/llm/cancel.py:race_stream_vs_cancel` — `subscribe_interrupt` RAII, node entry listens for cancel/terminate inbound, `asyncio.wait` races streaming task; cancel discards partial generation of current turn, not entering history

## Key dependencies

- [[tool-calls.ava.okf.md]] — LLM-generated code executed by exec node
- [[system-prompt.ava.okf.md]] — dynamic assembly of system prompt
- [[agent/docs/state.ava.okf.md]] — AIMessage stored into state.messages
- [[agent/db/docs/db.ava.okf.md]] — Redis pub/sub wakeup for inbound messages

## Entry points

- `agent/llm/__init__.py:execute_code` — tool schema definition
- `agent/graph/llm/node.py:llm_node()` — LLM node (`_llm_node_impl` is the implementation body)
- `agent/graph/llm/_stream.py:consume_llm()` — single streaming entry
- `base/lm/errors.py:classify_error()` — cross-provider classification of provider exceptions (transient/permanent/unknown)
- `base/lm/factory.py:build_chat_model()` — ChatModel factory

## Notes

- State type hint uses `from __future__ import annotations` + module attribute references to avoid capturing `BaseAgentState` alias and losing plugin dynamic fields
- The node retries trusted 429/transport/server failures under the per-model attempt cap and total wall-clock budget, with bounded backoff. Provider SDK construction keeps its existing finite retries. `llm_retry` records the final node retry duration. No retry re-enters the graph or repeats code execution.
