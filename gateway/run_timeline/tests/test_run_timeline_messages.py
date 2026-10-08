"""The raw-message range read behind a node's or unit's expand."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import HTTPException, Request
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from base.agents.history.checkpoint import single_segment_history
from base.agents.history.hierarchy.units import display_blocks, divide_units, read_times
from base.agents.history.hierarchy.usage import MessageUsage
from base.db import Database
from gateway.run_timeline import messages as route
from gateway.run_timeline.history import HistoryView

STAMP = "2026-10-04T12:00:00+00:00"


def history(count: int) -> list[BaseMessage]:
    out: list[BaseMessage] = []
    for n in range(count):
        out.append(
            HumanMessage(
                content=f"message {n}" + "x" * 40,
                additional_kwargs={"ava_msg_type": "inbound", "ava_created_at": STAMP},
            )
        )
    return out


class Views:
    """The `HistoryViewCache` the app state serves, pinned to one view."""

    def __init__(self, messages: list[BaseMessage]) -> None:
        read = read_times(messages)
        units = display_blocks(divide_units(messages), messages, read)
        self.built = HistoryView.of(
            single_segment_history(messages), units, MessageUsage(messages), read
        )

    def get(self, _db: object, _agent: int) -> HistoryView:
        return self.built


def request(messages: list[BaseMessage]) -> Request:
    state = SimpleNamespace(db=cast(Database, object()), run_timeline_views=Views(messages))
    return cast(Request, SimpleNamespace(app=SimpleNamespace(state=state)))


@pytest.fixture(autouse=True)
def small_text_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        route.settings, "display", SimpleNamespace(run_timeline_message_text_max=20), raising=False
    )


def test_a_range_returns_those_messages_in_order() -> None:
    result = route.get_run_timeline_messages(
        request(history(5)), 1, start=1, end=3, limit=50, full=True
    )
    assert [m.idx for m in result.messages] == [1, 2, 3]
    assert result.next_start is None
    assert result.messages[0].parts[0].kind == "inbound"
    assert result.messages[0].parts[0].text.startswith("message 1")


def test_a_long_range_is_cut_at_the_limit_and_says_where_to_continue() -> None:
    first = route.get_run_timeline_messages(
        request(history(5)), 1, start=0, end=4, limit=2, full=False
    )
    assert [m.idx for m in first.messages] == [0, 1] and first.next_start == 2
    rest = route.get_run_timeline_messages(
        request(history(5)), 1, start=2, end=4, limit=50, full=False
    )
    assert [m.idx for m in rest.messages] == [2, 3, 4] and rest.next_start is None


def test_a_long_part_is_clipped_unless_full_is_asked() -> None:
    clipped = route.get_run_timeline_messages(
        request(history(5)), 1, start=0, end=0, limit=50, full=False
    )
    (part,) = clipped.messages[0].parts
    assert part.text_truncated and len(part.text) == 20 and part.chars > 20
    full = route.get_run_timeline_messages(
        request(history(5)), 1, start=0, end=0, limit=50, full=True
    )
    assert not full.messages[0].parts[0].text_truncated
    assert len(full.messages[0].parts[0].text) == full.messages[0].parts[0].chars


def test_a_range_past_the_history_is_a_404() -> None:
    with pytest.raises(HTTPException) as caught:
        route.get_run_timeline_messages(
            request(history(5)), 1, start=3, end=9, limit=50, full=False
        )
    assert caught.value.status_code == 404


def test_a_reversed_range_is_a_422() -> None:
    with pytest.raises(HTTPException) as caught:
        route.get_run_timeline_messages(
            request(history(5)), 1, start=3, end=1, limit=50, full=False
        )
    assert caught.value.status_code == 422


def test_ai_parts_carry_their_kinds() -> None:
    messages: list[BaseMessage] = [
        AIMessage(
            content=[{"type": "thinking", "thinking": "plan"}, {"type": "text", "text": "hi"}],
            tool_calls=[{"name": "execute_code", "args": {"code": "ls"}, "id": "t"}],
            additional_kwargs={"ava_created_at": STAMP},
        )
    ]
    result = route.get_run_timeline_messages(
        request(messages), 1, start=0, end=0, limit=50, full=True
    )
    assert [p.kind for p in result.messages[0].parts] == ["think", "text", "call"]
