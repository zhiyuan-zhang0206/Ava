# pyright: reportOptionalSubscript=false
"""Llm helpers cases: llm node billing error logs billing vendor."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage
from langgraph.types import Command

from agent.graph import llm_node
from agent.graph.llm._retry import Attempt, retry_wait
from agent.graph.llm.node import llm_attempt
from agent.graph.llm_errors import LlmLedger
from agent.graph.tests.test_llm_helpers import (
    _CONFIG,
    _astream_raising,
    _FakeAnthropicError,
    _FakeOpenAIError,
    _FakeProviderStatusError,
    _fatal,
    _make_runtime,
)
from agent.graph.tests.test_llm_helpers import (
    ledger as ledger,
)
from agent.state import AgentState
from base.host.env.agent_slices import AgentSlices, ModelOverrides
from base.lm.catalog import ModelCatalog
from base.lm.plugin_providers import build_model_catalog


async def test_llm_node_billing_error_logs_billing_vendor_and_model(
    loguru_records, monkeypatch: pytest.MonkeyPatch, ledger: LlmLedger
) -> None:
    """Provider 402 logs the billing flag, vendor, and model for alert routing."""
    from agent.graph.llm_errors import FatalProviderError
    from base.config import settings
    from base.lm.context_budget import ContextBudget

    def fixture_budget(
        _model: str, _overrides: ModelOverrides, *, catalog: ModelCatalog
    ) -> ContextBudget:
        return ContextBudget(10_000, 3_000, 4_000)

    monkeypatch.setattr("agent.hooks.compact.resolve_context_budget", fixture_budget)
    fake_llm = MagicMock()
    fake_llm.astream.return_value = _astream_raising(_FakeProviderStatusError(402))
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    original = settings.lm.llm_model
    try:
        settings.lm.llm_model = "deepseek-v4-flash"
        with pytest.raises(FatalProviderError) as exc_info:
            await llm_node(
                state,
                _make_runtime(llm=fake_llm, event_publisher=MagicMock()),
                _CONFIG,
                ledger=ledger,
            )
    finally:
        settings.lm.llm_model = original

    assert "out of credit or quota" in str(exc_info.value)
    assert "vendor=deepseek" in str(exc_info.value)
    classify_logs = [r for r in loguru_records if r["extra"].get("event") == "llm_provider_error"]  # pyright: ignore[reportUnknownMemberType]
    assert len(classify_logs) == 1  # pyright: ignore[reportUnknownArgumentType]
    extra = classify_logs[0]["extra"]
    assert extra["billing"] is True
    assert extra["status"] == 402
    assert extra["vendor"] == "deepseek"
    assert extra["model"] == "deepseek-v4-flash"


async def test_llm_node_transient_provider_error_propagates_for_retry(ledger: LlmLedger) -> None:
    """A TRANSIENT provider error (HTTP 500) is re-raised as-is — NOT wrapped in
    FatalProviderError — so the node's retry loop retries it. Fail-fast is
    reserved for permanent classes; a transient blip must keep retrying."""
    from agent.graph.llm_errors import FatalProviderError

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _astream_raising(_FakeProviderStatusError(500))
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    runtime = _make_runtime(llm=fake_llm, event_publisher=MagicMock())
    with pytest.raises(_FakeProviderStatusError) as exc_info:
        await llm_attempt(state, runtime, _CONFIG, Attempt(1, time.time()), ledger=ledger)
    assert not isinstance(exc_info.value, FatalProviderError)
    assert (
        retry_wait(
            exc_info.value,
            1,
            model="deepseek-flash",
            agent_id=7,
            ledger=ledger,
            catalog=build_model_catalog(),
            max_attempts_pin=AgentSlices.resolve().read("lm", "llm_retry_max_attempts"),
        )
        is not None
    )


async def test_llm_node_configured_fatal_error_type_fails_fast(ledger: LlmLedger) -> None:
    """A configured fatal error *type* (e.g. engine_overloaded_error) surfacing on
    a transient-nature status (429) still fails fast: retrying an overloaded engine
    in-turn is futile, so it becomes a FatalProviderError (error_class records the
    transient nature; fatal=True records the fail-fast action)."""
    from agent.graph.llm_errors import FatalProviderError
    from base.config import settings

    original = settings.lm.llm_fatal_provider_error_types
    try:
        settings.lm.llm_fatal_provider_error_types = "engine_overloaded_error"
        exc = _FakeProviderStatusError(
            429, {"error": {"type": "engine_overloaded_error", "message": "overloaded"}}
        )
        fake_llm = MagicMock()
        fake_llm.astream.return_value = _astream_raising(exc)
        # A configured-fatal error type triggers _consume_llm's one non-streaming
        # fallback; make ainvoke fail the same way so the error survives to the
        # classify block instead of the fallback masking it.
        fake_llm.ainvoke = AsyncMock(side_effect=exc)
        state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

        with pytest.raises(FatalProviderError) as exc_info:
            await llm_node(
                state,
                _make_runtime(llm=fake_llm, event_publisher=MagicMock()),
                _CONFIG,
                ledger=ledger,
            )
        assert exc_info.value.error_class == "transient"
        assert exc_info.value.status == 429
    finally:
        settings.lm.llm_fatal_provider_error_types = original


async def test_silent_idle_guard_halts_at_cumulative_output_token_cap(
    loguru_records, ledger: LlmLedger
) -> None:
    """Silent-idle output consumes one token budget and reports its cost."""
    from base.config import settings

    cap = settings.lm.llm_silent_idle_max_output_tokens
    assert cap == 2048

    async def _reasoning_only() -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(
            content="",
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 1, "output_tokens": 1_100, "total_tokens": 1_101},
        )

    for turn in range(1, 3):
        fake_llm = MagicMock()
        fake_llm.astream.return_value = _reasoning_only()
        state = AgentState(messages=[HumanMessage(content="hi")], halted=False)
        result = await llm_node(
            state, _make_runtime(llm=fake_llm, event_publisher=MagicMock()), _CONFIG, ledger=ledger
        )
        assert isinstance(result, Command)
        assert result.goto == "after_exec"
        if turn == 1:
            assert result.update["halted"] is False
        else:
            assert result.update["halted"] is True

    # The budget is popped at the cap, so the next run starts fresh.
    assert ledger.silent_idle_output_tokens("7") == 0
    silent_logs = [
        r
        for r in loguru_records
        if r["extra"].get("event") == "silent_idle"  # pyright: ignore[reportUnknownMemberType]
    ]
    assert silent_logs[-1]["extra"]["cumulative_output_tokens"] == 2_200
    assert silent_logs[-1]["extra"]["estimated_cost_usd"] > 0


async def test_retried_llm_node_records_total_retry_duration(
    loguru_records, ledger: LlmLedger
) -> None:
    """A success after retry exports the full sequence duration as telemetry."""

    async def _text_turn() -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(
            content="done",
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _text_turn()
    runtime = _make_runtime(llm=fake_llm, event_publisher=MagicMock())

    await llm_attempt(
        AgentState(messages=[HumanMessage(content="hi")], halted=False),
        runtime,
        _CONFIG,
        Attempt(2, time.time() - 3.0),
        ledger=ledger,
    )

    retry_logs = [
        record
        for record in loguru_records
        if record["extra"].get("event") == "llm_retry"  # pyright: ignore[reportUnknownMemberType]
    ]
    assert retry_logs[-1]["extra"]["outcome"] == "succeeded"
    assert retry_logs[-1]["extra"]["duration_seconds"] >= 3.0


async def test_silent_idle_streak_resets_after_normal_turn(ledger: LlmLedger) -> None:
    """A real action clears the silent-idle output-token budget."""

    async def _reasoning_only() -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(
            content="",
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    async def _text_turn() -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(
            content="here is my answer",
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 1, "output_tokens": 4, "total_tokens": 5},
        )

    # 1) silent idle accumulates its output-token cost.
    fake_llm = MagicMock()
    fake_llm.astream.return_value = _reasoning_only()
    await llm_node(
        AgentState(messages=[HumanMessage(content="hi")], halted=False),
        _make_runtime(llm=fake_llm, event_publisher=MagicMock()),
        _CONFIG,
        ledger=ledger,
    )
    assert ledger.silent_idle_output_tokens("7") == 1

    # 2) a normal text turn resets the streak
    fake_llm = MagicMock()
    fake_llm.astream.return_value = _text_turn()
    result = await llm_node(
        AgentState(messages=[HumanMessage(content="hi")], halted=False),
        _make_runtime(llm=fake_llm, event_publisher=MagicMock()),
        _CONFIG,
        ledger=ledger,
    )
    assert result.update["halted"] is True  # text, no tool_call → halt
    assert ledger.silent_idle_output_tokens("7") == 0

    # 3) a later silent idle starts back at one output token, not two.
    fake_llm = MagicMock()
    fake_llm.astream.return_value = _reasoning_only()
    result = await llm_node(
        AgentState(messages=[HumanMessage(content="hi")], halted=False),
        _make_runtime(llm=fake_llm, event_publisher=MagicMock()),
        _CONFIG,
        ledger=ledger,
    )
    assert result.update["halted"] is False
    assert ledger.silent_idle_output_tokens("7") == 1


def test_parse_provider_error_type_openai_shape() -> None:
    """OpenAI SDK errors carry body.error.type — extract it."""
    from agent.graph.llm_errors import _parse_provider_error_type

    exc = _FakeOpenAIError(
        {
            "error": {
                "type": "engine_overloaded_error",
                "message": "The engine is currently overloaded",
            }
        }
    )
    assert _parse_provider_error_type(exc) == "engine_overloaded_error"


def test_parse_provider_error_type_anthropic_shape() -> None:
    """Anthropic SDK errors use the same body.error.type shape."""
    from agent.graph.llm_errors import _parse_provider_error_type

    exc = _FakeAnthropicError(
        {
            "error": {
                "type": "overloaded_error",
                "message": "Overloaded",
            }
        }
    )
    assert _parse_provider_error_type(exc) == "overloaded_error"


def test_parse_provider_error_type_no_body() -> None:
    """Exception without a body attribute returns None."""
    from agent.graph.llm_errors import _parse_provider_error_type

    assert _parse_provider_error_type(ConnectionError("net")) is None


def test_parse_provider_error_type_body_none() -> None:
    """Exception with body=None returns None."""
    from agent.graph.llm_errors import _parse_provider_error_type

    exc = _FakeOpenAIError(None)
    assert _parse_provider_error_type(exc) is None


def test_parse_provider_error_type_body_not_dict() -> None:
    """Exception with body as a non-dict (string, list) returns None."""
    from agent.graph.llm_errors import _parse_provider_error_type

    exc = _FakeOpenAIError("not a dict")  # type: ignore[arg-type]
    assert _parse_provider_error_type(exc) is None


def test_parse_provider_error_type_no_error_key() -> None:
    """body without 'error' key returns None."""
    from agent.graph.llm_errors import _parse_provider_error_type

    exc = _FakeOpenAIError({"status": "error"})
    assert _parse_provider_error_type(exc) is None


def test_parse_provider_error_type_error_not_dict() -> None:
    """body.error not a dict returns None."""
    from agent.graph.llm_errors import _parse_provider_error_type

    exc = _FakeOpenAIError({"error": "server_error"})
    assert _parse_provider_error_type(exc) is None


def test_parse_provider_error_type_no_type_key() -> None:
    """body.error without 'type' key returns None."""
    from agent.graph.llm_errors import _parse_provider_error_type

    exc = _FakeOpenAIError({"error": {"message": "oops"}})
    assert _parse_provider_error_type(exc) is None


def test_parse_provider_error_type_empty_string() -> None:
    """body.error.type is an empty string — returns None (not a meaningful type)."""
    from agent.graph.llm_errors import _parse_provider_error_type

    exc = _FakeOpenAIError({"error": {"type": ""}})
    assert _parse_provider_error_type(exc) is None


def test_is_fatal_provider_error_type_matches_configured() -> None:
    """When the error type is in the configured fatal set, returns True."""
    assert _fatal("engine_overloaded_error", "engine_overloaded_error") is True


def test_is_fatal_provider_error_type_not_in_set() -> None:
    """Error type not in the configured set returns False."""
    assert _fatal("rate_limit_exceeded", "engine_overloaded_error") is False


def test_is_fatal_provider_error_type_empty_config() -> None:
    """Empty configured set is a fast no-op (always returns False)."""
    assert _fatal("engine_overloaded_error", "") is False


def test_is_fatal_provider_error_type_no_body() -> None:
    """Exception without body (generic exception) returns False."""
    from agent.graph.llm_errors import _is_fatal_provider_error_type

    assert (
        _is_fatal_provider_error_type(ConnectionError("net"), AgentSlices.resolve().llm_policy)
        is False
    )


async def test_llm_usage_event_carries_latency_ms(loguru_records, ledger: LlmLedger) -> None:
    """The whole-call wall-clock lands on the llm_usage agent_event.

    `_stream_with_cache_retry` stamps `handler.llm_latency_ms` after the call
    completes, and `_finalize_turn_observability` forwards it to
    `log_llm_usage(latency_ms=...)` — the ops monitor panel's latency/TPS
    source. A real stream (one chunk with usage_metadata) must produce an
    llm_usage record with a positive latency_ms in its payload extras.
    """

    async def _one_chunk() -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(
            content="hi",
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _one_chunk()
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)
    await llm_node(
        state, _make_runtime(llm=fake_llm, event_publisher=MagicMock()), _CONFIG, ledger=ledger
    )

    usage = [r for r in loguru_records if r["extra"].get("event") == "llm_usage"]  # pyright: ignore[reportUnknownMemberType]
    assert len(usage) == 1, "exactly one llm_usage record per completed call"  # pyright: ignore[reportUnknownArgumentType]
    lat = usage[0]["extra"]["latency_ms"]
    assert lat is not None and lat > 0, f"latency_ms should be a positive ms float, got {lat!r}"
    assert usage[0]["extra"]["model"] == "deepseek-flash"
    assert usage[0]["extra"]["agent_id"] == 7
