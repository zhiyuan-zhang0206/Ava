"""Deterministic cost: AIMessage usage sums per span, and generation cost per node."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from base.agents.history.hierarchy.usage import MessageUsage


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
