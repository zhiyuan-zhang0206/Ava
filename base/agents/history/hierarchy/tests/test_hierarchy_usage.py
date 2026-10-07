"""Deterministic cost: AIMessage usage sums per span, and generation cost per node."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from base.agents.history.checkpoint import FullHistory
from base.agents.history.hierarchy.usage import CallRecord, MessageUsage, generation_by_span


def ai(input_tokens: int, cache_read: int, output_tokens: int) -> AIMessage:
    return AIMessage(
        content="x",
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "input_token_details": {"cache_read": cache_read},
        },
    )


MESSAGES: list[BaseMessage] = [
    SystemMessage(content="p"),
    ai(100, 0, 10),
    HumanMessage(content="r"),
    ai(150, 100, 20),
    AIMessage(content="no usage"),
    ai(200, 150, 5),
]


def test_span_sums_the_usage_of_the_ai_messages_inside() -> None:
    usage = MessageUsage(MESSAGES)
    assert (usage.span(1, 3).calls, usage.span(1, 3).input) == (2, 250)
    whole = usage.span(0, 5)
    assert (whole.calls, whole.input, whole.cache_read, whole.output) == (3, 450, 250, 35)


def test_a_span_without_ai_usage_is_zero_and_one_message_is_its_own_span() -> None:
    usage = MessageUsage(MESSAGES)
    assert usage.span(2, 2).calls == 0
    assert usage.span(3, 3).input == 150


def test_a_span_outside_the_history_is_refused() -> None:
    with pytest.raises(IndexError):
        MessageUsage(MESSAGES).span(0, 6)


def call(version: int, start_offset: int, prefix_len: int, input_tokens: int = 10) -> CallRecord:
    return CallRecord(
        compact_version=version,
        start_offset=start_offset,
        prefix_len=prefix_len,
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": 2,
            "input_token_details": {"cache_read": 4},
        },
        duration_ms=1500,
    )


def test_generation_cost_is_keyed_by_the_node_span_it_describes() -> None:
    # One segment, head kept: the request is [head, m1..]; a call whose chunk is request
    # positions 8..68 describes the stitched span 8..68 (the head is stitched index 0).
    messages: list[BaseMessage] = [SystemMessage(content="p"), *[HumanMessage(content="m")] * 80]
    history = FullHistory(messages, (messages[0],), (1,))  # type: ignore[arg-type]
    costs = generation_by_span(history, [call(0, 8, 69), call(0, 8, 69, 20), call(0, 69, 81)])
    first = costs[(8, 68)]
    assert (first.calls, first.input, first.cache_read, first.output) == (2, 30, 8, 4)
    assert first.seconds == 3.0
    assert costs[(69, 80)].calls == 1


def test_generation_cost_follows_the_segment_of_the_job() -> None:
    # Segment 1 starts at stitched index 50 and kept its own head: request position p
    # of that segment is stitched index 50 + p - 1.
    messages: list[BaseMessage] = [SystemMessage(content="p"), *[HumanMessage(content="m")] * 99]
    history = FullHistory(messages, (messages[0], messages[0]), (1, 50))  # type: ignore[arg-type]
    costs = generation_by_span(history, [call(1, 3, 10)])
    assert list(costs) == [(52, 58)]


def test_a_call_of_an_unknown_segment_yields_no_entry() -> None:
    messages: list[BaseMessage] = [SystemMessage(content="p"), HumanMessage(content="m")]
    history = FullHistory(messages, (messages[0],), (1,))  # type: ignore[arg-type]
    assert generation_by_span(history, [call(3, 1, 2)]) == {}
