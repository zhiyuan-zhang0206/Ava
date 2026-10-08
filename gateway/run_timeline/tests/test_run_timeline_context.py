"""The LLM requests of the stitched history and the context breakdown of one of them."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import HTTPException, Request
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from base.agents.history.checkpoint import FullHistory
from base.agents.history.hierarchy.units import display_blocks, divide_units, read_times
from base.agents.history.hierarchy.usage import MessageUsage
from base.db import Database
from gateway.agents.context_breakdown import RequestBreakdown
from gateway.agents.schemas import ContextBreakdownResponse, ContextCategory
from gateway.run_timeline import context
from gateway.run_timeline.history import HistoryView

T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def stamp(minutes: int) -> dict[str, object]:
    return {"ava_created_at": (T0 + timedelta(minutes=minutes)).isoformat()}


def ai(text: str, tokens: int, minutes: int) -> AIMessage:
    return AIMessage(
        content=text,
        usage_metadata={"input_tokens": tokens, "output_tokens": 1, "total_tokens": tokens + 1},
        additional_kwargs=stamp(minutes),
    )


def human(text: str, minutes: int) -> HumanMessage:
    return HumanMessage(
        content=text, additional_kwargs={"ava_msg_type": "inbound", **stamp(minutes)}
    )


def two_sessions() -> HistoryView:
    """Session 0: prompt, ask@0, reply@1 (100 tokens). Session 1 (after a compaction): its own
    prompt (dropped from the stitched list), ask@10, reply@11 (40 tokens)."""
    messages: list[BaseMessage] = [
        SystemMessage(content="first prompt"),
        human("ask one", 0),
        ai("reply one", 100, 1),
        human("ask two", 10),
        ai("reply two", 40, 11),
    ]
    read = read_times(messages)
    units = display_blocks(divide_units(messages), messages, read)
    history = FullHistory(
        messages,
        (messages[0], SystemMessage(content="second prompt")),  # type: ignore[arg-type]
        (1, 3),
    )
    return HistoryView.of(history, units, MessageUsage(messages), read)


def test_requests_carry_their_session_input_size_and_send_time() -> None:
    requests = context.llm_requests(two_sessions())
    assert [(r.idx, r.session, r.input_tokens, r.output_tokens) for r in requests] == [
        (2, 0, 100, 1),
        (4, 1, 40, 1),
    ]
    # A request is sent when the message before it was read.
    assert requests[0].ts == T0
    assert requests[1].ts == T0 + timedelta(minutes=10)


def with_output(msg: AIMessage, output: int) -> AIMessage:
    msg.usage_metadata = {
        "input_tokens": msg.usage_metadata["input_tokens"],  # type: ignore[index]
        "output_tokens": output,
        "total_tokens": msg.usage_metadata["input_tokens"] + output,  # type: ignore[index]
    }
    return msg


def three_requests_then_a_session() -> HistoryView:
    """Session 0: prompt, ask@0, reply1@1 (input 100, output 5), two asks @2 and @3, reply2@4
    (input 160, output 7), ask@5, reply3@6 (input 190). Session 1: ask@10, reply4@11 (input 40)."""
    messages: list[BaseMessage] = [
        SystemMessage(content="first prompt"),
        human("ask one", 0),
        with_output(ai("reply one", 100, 1), 5),
        human("ask two", 2),
        human("ask three", 3),
        with_output(ai("reply two", 160, 4), 7),
        human("ask four", 5),
        ai("reply three", 190, 6),
        human("ask five", 10),
        ai("reply four", 40, 11),
    ]
    read = read_times(messages)
    units = display_blocks(divide_units(messages), messages, read)
    history = FullHistory(
        messages,
        (messages[0], SystemMessage(content="second prompt")),  # type: ignore[arg-type]
        (1, 8),
    )
    return HistoryView.of(history, units, MessageUsage(messages), read)


def test_a_request_adds_what_entered_the_context_since_the_previous_one() -> None:
    view = three_requests_then_a_session()
    requests = context.llm_requests(view)
    assert [r.idx for r in requests] == [2, 5, 7, 9]
    # The previous reply is re-sent, so its output counts: 5 + the two asks make up the whole growth.
    assert requests[1].added_tokens == 160 - 100
    assert requests[1].added_estimated is True  # the asks share a provider total
    assert requests[2].added_tokens == 190 - 160
    assert requests[2].added_estimated is False  # reply two and one ask: both anchored exactly
    assert requests[2].added_tokens == sum(t.context_tokens or 0 for t in view.tokens[5:7])


def test_a_sessions_first_request_adds_its_first_messages_without_the_system_prompt() -> None:
    view = three_requests_then_a_session()
    requests = context.llm_requests(view)
    # Session 0 starts at the first ask (the prompt, message 0, is the head); session 1 at its own first message.
    assert requests[0].added_tokens == view.tokens[1].context_tokens
    assert requests[0].added_tokens < requests[0].input_tokens
    assert requests[3].added_tokens == view.tokens[8].context_tokens
    # The previous session's last reply is not re-sent after a compaction.
    assert requests[3].added_tokens != sum(t.context_tokens or 0 for t in view.tokens[7:9])


def test_a_point_resolves_to_the_next_request_else_the_last() -> None:
    requests = context.llm_requests(two_sessions())
    assert [context.request_at(requests, at).idx for at in (0, 2, 3, 4, 99)] == [2, 2, 4, 4, 4]  # type: ignore[union-attr]
    assert context.request_at([], 0) is None


class Views:
    def __init__(self, view: HistoryView) -> None:
        self.view = view

    def get(self, _db: object, _agent: int) -> HistoryView:
        return self.view


def call(view: HistoryView, at: int, monkeypatch: pytest.MonkeyPatch):

    def breakdown(
        _request: Request, _agent: int, found: RequestBreakdown
    ) -> ContextBreakdownResponse:
        # The window and thresholds come from the model registry; the bucketing is the real one.
        return ContextBreakdownResponse(
            total_input_tokens=found.total.tokens,
            estimated=found.total.estimated,
            exact_fraction=found.total.exact_fraction,
            sections=[],
            categories=[
                ContextCategory(
                    kind=c.kind,
                    tokens=c.total.tokens,
                    estimated=c.total.estimated,
                    exact_fraction=c.total.exact_fraction,
                )
                for c in found.categories
            ],
        )

    monkeypatch.setattr(context, "context_breakdown_response", breakdown)
    app_state = SimpleNamespace(db=cast(Database, object()), run_timeline_views=Views(view))
    request = cast(Request, SimpleNamespace(app=SimpleNamespace(state=app_state)))
    return context.get_run_timeline_context(request, 7, at)


def test_the_context_is_the_breakdown_of_that_request_summed_from_its_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = call(two_sessions(), 3, monkeypatch)
    assert (result.request, result.session, result.sessions) == (4, 1, 2)
    assert result.ts == T0 + timedelta(minutes=10)
    assert result.total_input_tokens == 40
    assert result.estimated is False  # the request's own input_tokens
    kinds = {c.kind: c.tokens for c in result.categories}
    assert set(kinds) == {"system_prompt", "user_input"}
    assert sum(kinds.values()) == 40
    first = call(two_sessions(), 0, monkeypatch)
    assert (first.session, first.total_input_tokens) == (0, 100)


def test_an_agent_with_no_request_has_no_context(monkeypatch: pytest.MonkeyPatch) -> None:
    messages: list[BaseMessage] = [human("hello", 0)]
    read = read_times(messages)
    view = HistoryView.of(FullHistory(messages, (None,), (0,)), [], MessageUsage(messages), read)
    with pytest.raises(HTTPException) as caught:
        call(view, 0, monkeypatch)
    assert caught.value.status_code == 404


def test_a_request_reports_the_message_range_its_addition_covers() -> None:
    view = three_requests_then_a_session()
    requests = context.llm_requests(view)
    # Half-open, ending at the request's own AIMessage; later requests start at the previous reply (re-sent).
    assert [(r.added_from, r.added_to) for r in requests] == [(1, 2), (2, 5), (5, 7), (8, 9)]
    assert all(r.added_to == r.idx for r in requests)
