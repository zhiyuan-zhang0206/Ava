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
from gateway.agents.context_breakdown import compute_breakdown
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
    return HistoryView(history, units, MessageUsage(messages), read)


def test_requests_carry_their_session_input_size_and_send_time() -> None:
    requests = context.llm_requests(two_sessions())
    assert [(r.idx, r.session, r.input_tokens) for r in requests] == [(2, 0, 100), (4, 1, 40)]
    # A request is sent when the message before it was read.
    assert requests[0].ts == T0
    assert requests[1].ts == T0 + timedelta(minutes=10)


def test_a_point_resolves_to_the_next_request_else_the_last() -> None:
    requests = context.llm_requests(two_sessions())
    assert [context.request_at(requests, at).idx for at in (0, 2, 3, 4, 99)] == [2, 2, 4, 4, 4]  # type: ignore[union-attr]
    assert context.request_at([], 0) is None


def test_a_request_input_is_its_segment_head_and_the_segment_before_it() -> None:
    view = two_sessions()
    first, second = context.llm_requests(view)

    def texts(messages: list[BaseMessage]) -> list[str]:
        return [str(m.content) for m in messages]  # pyright: ignore[reportUnknownMemberType]

    assert texts(context.request_input(view, first)) == ["first prompt", "ask one"]
    assert texts(context.request_input(view, second)) == ["second prompt", "ask two"]


class Views:
    def __init__(self, view: HistoryView) -> None:
        self.view = view

    def get(self, _db: object, _agent: int) -> HistoryView:
        return self.view


def call(view: HistoryView, at: int, monkeypatch: pytest.MonkeyPatch):

    def breakdown(
        _request: Request, _agent: int, messages: list[BaseMessage], total: int
    ) -> ContextBreakdownResponse:
        # The window and thresholds come from the model registry; the bucketing is the real one.
        categories, _sections, estimated = compute_breakdown(messages, total)
        return ContextBreakdownResponse(
            total_input_tokens=total,
            estimated_total=estimated,
            sections=[],
            categories=[ContextCategory(kind=k, tokens=n) for k, n in categories],
        )

    monkeypatch.setattr(context, "context_breakdown_response", breakdown)
    app_state = SimpleNamespace(db=cast(Database, object()), run_timeline_views=Views(view))
    request = cast(Request, SimpleNamespace(app=SimpleNamespace(state=app_state)))
    return context.get_run_timeline_context(request, 7, at)


def test_the_context_is_the_breakdown_of_that_request_anchored_to_its_input_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = call(two_sessions(), 3, monkeypatch)
    assert (result.request, result.session, result.sessions) == (4, 1, 2)
    assert result.ts == T0 + timedelta(minutes=10)
    assert result.total_input_tokens == 40
    kinds = {c.kind: c.tokens for c in result.categories}
    assert set(kinds) == {"system_prompt", "user_input"}
    assert sum(kinds.values()) == 40
    first = call(two_sessions(), 0, monkeypatch)
    assert (first.session, first.total_input_tokens) == (0, 100)


def test_an_agent_with_no_request_has_no_context(monkeypatch: pytest.MonkeyPatch) -> None:
    messages: list[BaseMessage] = [human("hello", 0)]
    read = read_times(messages)
    view = HistoryView(FullHistory(messages, (None,), (0,)), [], MessageUsage(messages), read)
    with pytest.raises(HTTPException) as caught:
        call(view, 0, monkeypatch)
    assert caught.value.status_code == 404
