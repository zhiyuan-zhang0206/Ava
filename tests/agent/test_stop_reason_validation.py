"""`_validate_stop_reason` unit tests — agent 169 max_tokens + abnormal terminal-reason defense.

DeepSeek V4 Pro extended thinking single-turn thinking exceeding max_tokens causes server to
return `stop_reason='max_tokens'` + a truncated thinking block (signature intact but content
cut mid-sentence), tool_calls empty.

Previously `_llm_node_impl` saw `if not final_msg.tool_calls` and returned halted=True
treating it as idle — user saw agent silent with status=idling, no clue.

Fix (classify_stop design): after stream accumulates final_msg, immediately check the terminal
reason via `classify_stop` (dispatched on `model_provider`), mapping each provider's vocabulary
to NORMAL / TRUNCATED / UNEXPECTED / CORRUPTED. Non-normal → raise (`LLMStreamTruncatedError`
for truncation, `LLMStreamUnexpectedStopReasonError` for others). langgraph arun_with_retry
retries llm_node; failure goes through turn_end ok=False + traceback, frontend sees Error
instead of silent idle.

Covers anthropic (`stop_reason`), openai (`finish_reason`), and google_genai (`finish_reason`).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from langchain_core.messages import AIMessage

from agent.graph._llm import (
    LLMStreamCorruptedError,
    LLMStreamError,
    LLMStreamTruncatedError,
    LLMStreamUnexpectedStopReasonError,
    _validate_stop_reason,
)
from shared.lm._plugin_providers import ensure_provider_plugins_loaded

ensure_provider_plugins_loaded()


def test_validate_passes_on_end_turn() -> None:
    """`stop_reason='end_turn'` is natural end of model turn (text-only idle or turn complete) → no raise."""
    msg = AIMessage(
        content="ok", response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"}
    )
    _validate_stop_reason(msg)


def test_validate_raises_on_max_tokens() -> None:
    """`stop_reason='max_tokens'` is server truncation due to output budget → raise
    LLMStreamTruncatedError. Error carries stop_reason as attribute (programmatic
    dispatch without regex-parsing the message).

    `output_tokens=0` is the fallback when usage_metadata is missing — locks the
    contract that validator does not crash on missing usage_metadata."""
    msg = AIMessage(
        content="", response_metadata={"model_provider": "anthropic", "stop_reason": "max_tokens"}
    )
    with pytest.raises(LLMStreamTruncatedError, match=r"max_tokens.*output_tokens=0") as exc_info:
        _validate_stop_reason(msg)
    assert exc_info.value.stop_reason == "max_tokens"
    assert exc_info.value.output_tokens == 0


def test_validate_raises_includes_output_tokens_for_diagnosis() -> None:
    """Error message includes output_tokens — ops can see at a glance how many tokens
    were used before truncation, to judge whether to bump max_tokens or model produced
    a huge thinking block."""
    msg = AIMessage(
        content="",
        response_metadata={"model_provider": "anthropic", "stop_reason": "max_tokens"},
        usage_metadata={"input_tokens": 1000, "output_tokens": 4096, "total_tokens": 5096},
    )
    with pytest.raises(LLMStreamTruncatedError, match=r"output_tokens=4096"):
        _validate_stop_reason(msg)


def test_validate_raises_on_missing_stop_reason() -> None:
    """response_metadata missing the provider's terminal-reason key (server drift / final chunk lost) →
    raise LLMStreamCorruptedError, NOT silently skip. Without the terminal reason, validator
    cannot distinguish idle from truncation; the if-not-tool_calls path below would misjudge as idle.
    Provider SDK types the field Optional — theoretically can be None if the tail frame is lost;
    fail-fast in the validator is better than letting the upstream path misjudge."""
    msg_empty = AIMessage(content="ok", response_metadata={"model_provider": "anthropic"})
    with pytest.raises(LLMStreamCorruptedError):
        _validate_stop_reason(msg_empty)
    msg_missing = AIMessage(
        content="ok", response_metadata={"model_provider": "anthropic", "stop_reason": None}
    )
    with pytest.raises(LLMStreamCorruptedError):
        _validate_stop_reason(msg_missing)


def test_validate_raises_on_pause_turn() -> None:
    """`stop_reason='pause_turn'` (Anthropic server-side pause); framework has no continuation
    protocol → raise LLMStreamUnexpectedStopReasonError rather than silent halt."""
    msg = AIMessage(
        content="", response_metadata={"model_provider": "anthropic", "stop_reason": "pause_turn"}
    )
    with pytest.raises(LLMStreamUnexpectedStopReasonError, match=r"pause_turn"):
        _validate_stop_reason(msg)


def test_validate_passes_on_refusal() -> None:
    """`stop_reason='refusal'` (model safety decline) is a valid terminal response — Anthropic
    handling-stop-reasons docs: "Claude declined to respond, consider rephrasing". Model already
    wrote a refusal message in content; passing through lets it reach state.messages and the user.
    Raising would drop the content."""
    msg = AIMessage(
        content="I'm sorry, I can't help with that.",
        response_metadata={"model_provider": "anthropic", "stop_reason": "refusal"},
    )
    _validate_stop_reason(msg)


def test_validate_raises_on_tool_use_without_tool_calls() -> None:
    """`stop_reason='tool_use'` but final_msg.tool_calls empty — server protocol inconsistency
    (claimed emit tool but no tool_use block in stream) → raise LLMStreamCorruptedError;
    cannot fallthrough and let if-not-tool_calls treat as idle (reproduces bug 169)."""
    msg = AIMessage(
        content="", response_metadata={"model_provider": "anthropic", "stop_reason": "tool_use"}
    )
    with pytest.raises(LLMStreamCorruptedError, match=r"tool_use.*tool_calls"):
        _validate_stop_reason(msg)


def test_validate_raises_on_openai_tool_calls_without_tool_calls() -> None:
    """OpenAI analog of bug 169: finish_reason='tool_calls' but final_msg.tool_calls
    empty — the stream lost the tool block; must fail-fast, not silently idle."""
    msg = AIMessage(
        content="", response_metadata={"model_provider": "openai", "finish_reason": "tool_calls"}
    )
    with pytest.raises(LLMStreamCorruptedError, match=r"tool_calls.*empty"):
        _validate_stop_reason(msg)


def test_validate_passes_on_openai_tool_calls_with_tool_calls() -> None:
    """openai tool_calls with non-empty tool_calls — normal path, no raise."""
    msg = AIMessage(
        content="",
        response_metadata={"model_provider": "openai", "finish_reason": "tool_calls"},
        tool_calls=[{"name": "execute_code", "args": {"code": "x"}, "id": "call_1"}],
    )
    _validate_stop_reason(msg)


def test_validate_passes_on_tool_use_with_tool_calls() -> None:
    """tool_use with non-empty tool_calls — normal path, no raise."""
    msg = AIMessage(
        content=[],
        response_metadata={"model_provider": "anthropic", "stop_reason": "tool_use"},
        tool_calls=[{"name": "execute_code", "args": {"code": "x"}, "id": "call_1"}],
    )
    _validate_stop_reason(msg)


def test_validate_raises_on_stop_sequence() -> None:
    """`stop_reason='stop_sequence'` appearing without actively set stop sequences = abnormal → raise."""
    msg = AIMessage(
        content="",
        response_metadata={"model_provider": "anthropic", "stop_reason": "stop_sequence"},
    )
    with pytest.raises(LLMStreamUnexpectedStopReasonError, match=r"stop_sequence"):
        _validate_stop_reason(msg)


def test_validate_raises_on_model_context_window_exceeded() -> None:
    """`stop_reason='model_context_window_exceeded'` (Sonnet 4.5+; input fills context window,
    server force-stops) → raise. Locks that each known non-normal stop_reason has an explicit
    case, preventing accidental silent-skip special-casing."""
    msg = AIMessage(
        content="",
        response_metadata={
            "model_provider": "anthropic",
            "stop_reason": "model_context_window_exceeded",
        },
    )
    with pytest.raises(LLMStreamUnexpectedStopReasonError, match=r"model_context_window_exceeded"):
        _validate_stop_reason(msg)


def test_validate_raises_on_unknown_stop_reason() -> None:
    """Unknown stop_reason (protocol upgrade / provider custom value) → raise, not silent fallthrough.
    Locks the whitelist design — only explicitly enumerated normal values pass; everything else fails loud."""
    msg = AIMessage(
        content="",
        response_metadata={"model_provider": "anthropic", "stop_reason": "future_value_xyz"},
    )
    with pytest.raises(LLMStreamUnexpectedStopReasonError, match=r"future_value_xyz"):
        _validate_stop_reason(msg)


# --- New cross-provider tests (gemini + openai) ---


def test_gemini_stop_passes() -> None:
    """google_genai finish_reason='STOP' is normal completion — no raise."""
    msg = AIMessage(
        content="hello",
        response_metadata={"model_provider": "google_genai", "finish_reason": "STOP"},
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    _validate_stop_reason(msg)


def test_openai_stop_passes() -> None:
    """openai finish_reason='stop' is normal completion — no raise."""
    msg = AIMessage(
        content="hello",
        response_metadata={"model_provider": "openai", "finish_reason": "stop"},
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    _validate_stop_reason(msg)


def test_anthropic_end_turn_passes() -> None:
    """anthropic stop_reason='end_turn' with explicit model_provider — no raise."""
    msg = AIMessage(
        content="hello",
        response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    _validate_stop_reason(msg)


def test_gemini_max_tokens_truncated() -> None:
    """google_genai finish_reason='MAX_TOKENS' → LLMStreamTruncatedError."""
    msg = AIMessage(
        content="",
        response_metadata={"model_provider": "google_genai", "finish_reason": "MAX_TOKENS"},
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    with pytest.raises(LLMStreamTruncatedError):
        _validate_stop_reason(msg)


def test_gemini_missing_corrupted() -> None:
    """google_genai with no finish_reason → LLMStreamCorruptedError."""
    msg = AIMessage(
        content="",
        response_metadata={"model_provider": "google_genai"},
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    with pytest.raises(LLMStreamCorruptedError):
        _validate_stop_reason(msg)


def test_gemini_safety_unexpected() -> None:
    """google_genai finish_reason='SAFETY' → LLMStreamUnexpectedStopReasonError."""
    msg = AIMessage(
        content="",
        response_metadata={"model_provider": "google_genai", "finish_reason": "SAFETY"},
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    with pytest.raises(LLMStreamUnexpectedStopReasonError):
        _validate_stop_reason(msg)


async def test_llm_node_validator_wired(
    fake_cancel_event: asyncio.Event,
) -> None:
    """Integration smoke: real _llm_node_impl path runs through, stream yields a max_tokens
    truncated chunk → should raise LLMStreamTruncatedError, locking in that
    _validate_stop_reason is actually called (no mocked validator).

    Guards against someone deleting the line wiring — all unit tests pass but
    production falls back to silent idle.

    Error event publish is not tested here — publish has been moved to the outer
    `services/agent_host/host.py:_invoke_until_done` (only publishes after
    langgraph retries are exhausted, avoiding "error" for every failed retry attempt)."""
    from unittest.mock import MagicMock

    from langchain_core.messages import AIMessageChunk, HumanMessage
    from langchain_core.runnables import RunnableConfig
    from langgraph.runtime import Runtime

    from agent.graph import llm_node
    from agent.graph._context import AvaContext
    from agent.state import AgentState
    from tests.agent._fakes import make_fake_ops_pool

    async def _truncated_stream() -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(
            content="thinking...",
            response_metadata={"model_provider": "anthropic", "stop_reason": "max_tokens"},
            usage_metadata={"input_tokens": 10, "output_tokens": 4096, "total_tokens": 4106},
        )

    fake_llm = MagicMock()
    fake_llm.bind_tools.return_value = fake_llm
    fake_llm.astream.return_value = _truncated_stream()
    pub = MagicMock()
    ops_db = make_fake_ops_pool()
    ctx = AvaContext(ops_pool=ops_db, llm=fake_llm, event_publisher=pub)
    runtime: Runtime[AvaContext] = Runtime(context=ctx)
    config: RunnableConfig = {"configurable": {"thread_id": "7"}}
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    with pytest.raises(LLMStreamTruncatedError):
        await llm_node(state, runtime, config)

    # llm_node does NOT emit Error event — locks the "moved to outer wrapper" design
    # point, preventing someone from re-adding emit inside llm_node because "frontend
    # doesn't show errors", which reintroduces the repeated-emit-during-retry bug.
    error_emits = [call for call in pub.emit.call_args_list if '"role":"error"' in call.args[0]]
    assert len(error_emits) == 0, (
        f"llm_node should NOT emit Error event (would spam during langgraph retries); "
        f"got {len(error_emits)} emit call(s): {error_emits!r}"
    )


def test_exception_hierarchy() -> None:
    """Lock the base class + subclass inheritance contract — caller / ops can catch
    `LLMStreamError` once for all stream-layer aborts (Stall / Corrupted / Truncated /
    UnexpectedStopReason), and SQL can query via `payload->>'exception_type' LIKE
    'LLMStream%'` in one ops sweep. `LLMStreamTruncatedError` is also a subclass of
    `LLMStreamUnexpectedStopReasonError` (max_tokens is one kind of unexpected
    stop_reason), so coarse catch at either parent level works, and fine catch at the
    subclass gives the max_tokens-specific path."""
    assert issubclass(LLMStreamUnexpectedStopReasonError, LLMStreamError)
    assert issubclass(LLMStreamCorruptedError, LLMStreamError)
    assert issubclass(LLMStreamTruncatedError, LLMStreamUnexpectedStopReasonError)
    msg = AIMessage(
        content="", response_metadata={"model_provider": "anthropic", "stop_reason": "max_tokens"}
    )
    with pytest.raises(LLMStreamError):
        _validate_stop_reason(msg)
    with pytest.raises(LLMStreamUnexpectedStopReasonError):
        _validate_stop_reason(msg)
