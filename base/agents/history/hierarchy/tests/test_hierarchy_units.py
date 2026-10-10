"""Layer 0 of the understanding tree: the deterministic message-unit division."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from base.agents.history.hierarchy.units import PREVIEW_CHARS, MessageUnit, divide_units
from base.agents.history.timeline_inputs import TimelineReadInputs
from base.clock import Clock
from base.config import settings

_TIMELINE_INPUTS = TimelineReadInputs(
    Clock.from_settings, lambda: settings.general.message_timestamps
)


def ts(second: int) -> str:
    return f"2026-10-04T12:00:{second:02d}+00:00"


def ai(
    second: int,
    *,
    think: str | None = None,
    text: str | None = None,
    code: str | None = None,
) -> AIMessage:
    blocks: list[str | dict[str, str]] = []
    if think is not None:
        blocks.append({"type": "thinking", "thinking": think})
    if text is not None:
        blocks.append({"type": "text", "text": text})
    calls = [{"name": "execute_code", "args": {"code": code}, "id": f"tc{second}"}] if code else []
    return AIMessage(
        content=blocks, tool_calls=calls, additional_kwargs={"ava_created_at": ts(second)}
    )


def result(second: int, text: str = "ok") -> ToolMessage:
    return ToolMessage(
        content=text,
        tool_call_id="tc",
        additional_kwargs={
            "ava_msg_type": "exec_output",
            "ava_exec_body_start": 0,
            "ava_created_at": ts(second),
        },
    )


def inbound(second: int, text: str, source: str = "user") -> HumanMessage:
    return HumanMessage(
        content=text,
        additional_kwargs={
            "ava_msg_type": "inbound",
            "ava_created_at": ts(second),
            "ava_source": source,
        },
    )


def note(second: int, text: str = "heartbeat") -> HumanMessage:
    return HumanMessage(
        content=text,
        additional_kwargs={"ava_msg_type": "system_note", "ava_created_at": ts(second)},
    )


def shape(units: list[MessageUnit]) -> list[tuple[str, int, int]]:
    return [(unit.kind, unit.i0, unit.i1) for unit in units]


def test_reasoning_call_and_result_are_one_work_unit() -> None:
    messages: list[BaseMessage] = [
        SystemMessage(content="prompt"),
        ai(1, think="plan", code="ls"),
        result(2),
        ai(3, think="again", code="cat"),
        result(4),
    ]
    assert shape(divide_units(messages, timeline_inputs=_TIMELINE_INPUTS)) == [
        ("work", 1, 2),
        ("work", 3, 4),
    ]


def test_text_is_its_own_unit_even_in_a_message_with_a_call() -> None:
    messages = [ai(1, think="plan", text="on it", code="ls"), result(2)]
    units = divide_units(messages, timeline_inputs=_TIMELINE_INPUTS)
    assert shape(units) == [("text", 0, 0), ("work", 0, 1)]
    assert units[0].preview == "on it"
    assert units[1].preview == "plan"


def test_text_only_message_closes_the_work_before_it() -> None:
    messages = [ai(1, think="plan", code="ls"), result(2), ai(3, text="done")]
    assert shape(divide_units(messages, timeline_inputs=_TIMELINE_INPUTS)) == [
        ("work", 0, 1),
        ("text", 2, 2),
    ]


def test_reasoning_without_a_call_is_a_work_unit_apart_from_its_text() -> None:
    messages = [ai(1, think="musing", text="hello")]
    assert shape(divide_units(messages, timeline_inputs=_TIMELINE_INPUTS)) == [
        ("text", 0, 0),
        ("work", 0, 0),
    ]


def test_every_inbound_is_a_unit_and_closes_the_open_work() -> None:
    messages = [
        ai(1, code="ls"),
        inbound(2, "hi", source="user"),
        result(3),
        inbound(4, "again", source="agent:7"),
    ]
    units = divide_units(messages, timeline_inputs=_TIMELINE_INPUTS)
    assert shape(units) == [("work", 0, 0), ("inbound", 1, 1), ("work", 2, 2), ("inbound", 3, 3)]
    assert [unit.source for unit in units if unit.kind == "inbound"] == ["user", "agent:7"]


def test_a_note_between_call_and_result_does_not_split_the_work() -> None:
    messages = [ai(1, code="sleep"), note(2), result(3)]
    assert shape(divide_units(messages, timeline_inputs=_TIMELINE_INPUTS)) == [
        ("work", 0, 2),
        ("note", 1, 1),
    ]


def test_a_result_with_no_call_forms_a_work_unit() -> None:
    assert shape(divide_units([result(1), ai(2, code="ls")], timeline_inputs=_TIMELINE_INPUTS)) == [
        ("work", 0, 0),
        ("work", 1, 1),
    ]


def test_the_system_prompt_belongs_to_no_unit() -> None:
    assert divide_units([SystemMessage(content="prompt")], timeline_inputs=_TIMELINE_INPUTS) == []


def test_times_span_the_unit_and_the_preview_is_clipped() -> None:
    long = "word " * 100
    units = divide_units(
        [ai(1, think=long, code="ls"), result(9)], timeline_inputs=_TIMELINE_INPUTS
    )
    (unit,) = units
    assert unit.start == datetime(2026, 10, 4, 12, 0, 1, tzinfo=UTC)
    assert unit.end == datetime(2026, 10, 4, 12, 0, 9, tzinfo=UTC)
    assert len(unit.preview) == PREVIEW_CHARS and unit.preview.endswith("…")


def test_a_message_without_a_real_time_yields_a_unit_with_no_time() -> None:
    (unit,) = divide_units(
        [AIMessage(content=[{"type": "text", "text": "old"}])], timeline_inputs=_TIMELINE_INPUTS
    )
    assert unit.start is None and unit.end is None


def test_a_mixed_kind_message_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from base.agents.history.hierarchy import units as units_module
    from base.agents.history.timeline import TimelineItem

    items = [
        TimelineItem(item_id="0.0", kind="inbound_chat", payload="a"),
        TimelineItem(item_id="0.1", kind="code_output", payload="b"),
    ]

    def build(*_args: object, **_kwargs: object) -> tuple[list[TimelineItem], int]:
        return items, 1

    monkeypatch.setattr(units_module, "build_timeline_items", build)
    with pytest.raises(ValueError, match="uncovered mix"):
        divide_units([], timeline_inputs=_TIMELINE_INPUTS)
