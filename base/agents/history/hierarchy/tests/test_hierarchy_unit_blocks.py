"""`base.agents.history.hierarchy.timeline_blocks` — a turn split into thinking, call and output blocks."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from base.agents.history.hierarchy.units import (
    DisplayBlock,
    display_blocks,
    divide_units,
    read_times,
)

T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def at(seconds: int) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat()


def inbound(seconds: int) -> HumanMessage:
    return HumanMessage(
        content="go", additional_kwargs={"ava_msg_type": "inbound", "ava_created_at": at(seconds)}
    )


def turn(
    seconds: int,
    *,
    think: str = "plan",
    text: str = "",
    code: str | None = "ls",
    reasoning_ms: int | None = None,
) -> AIMessage:
    content: list[str | dict[str, str]] = []
    if think:
        content.append({"type": "thinking", "thinking": think})
    if text:
        content.append({"type": "text", "text": text})
    kwargs: dict[str, object] = {"ava_created_at": at(seconds)}
    if reasoning_ms is not None:
        kwargs["ava_reasoning_ms_by_block"] = {"0": reasoning_ms}
    return AIMessage(
        content=content,  # type: ignore[arg-type]
        tool_calls=[{"name": "execute_code", "args": {"code": code}, "id": "t"}] if code else [],
        additional_kwargs=kwargs,
    )


def result(seconds: int) -> ToolMessage:
    return ToolMessage(
        content="Code execution output after running for 3s [x]:\n\nfiles",
        tool_call_id="t",
        additional_kwargs={
            "ava_msg_type": "exec_output",
            "ava_exec_body_start": len("Code execution output after running for 3s [x]:\n\n"),
            "ava_created_at": at(seconds),
        },
    )


def blocks(msgs: Sequence[BaseMessage]) -> list[tuple[str, int, int, int, int]]:
    out = display_blocks(divide_units(msgs), msgs, read_times(msgs))
    return [
        (b.kind, b.i0, b.i1, int((b.start - T0).total_seconds()), int((b.end - T0).total_seconds()))
        for b in out
    ]


def test_a_work_unit_is_shown_as_thinking_call_and_output_on_read_times() -> None:
    msgs = [inbound(0), turn(10), result(25)]
    assert blocks(msgs) == [
        ("inbound", 0, 0, 0, 0),
        ("thinking", 1, 1, 0, 10),  # from the previous message's read time to the end of the stream
        ("call", 1, 1, 10, 10),  # an instant at the end of the stream
        ("output", 1, 2, 10, 25),  # the execution, up to the tool result
    ]


def test_text_with_recorded_reasoning_time_takes_the_rest_of_the_stream() -> None:
    msgs = [inbound(0), turn(10, text="looking", reasoning_ms=4000), result(12)]
    assert blocks(msgs) == [
        ("inbound", 0, 0, 0, 0),
        ("text", 1, 1, 4, 10),
        ("thinking", 1, 1, 0, 4),
        ("call", 1, 1, 10, 10),
        ("output", 1, 2, 10, 12),
    ]


def test_text_without_a_recorded_reasoning_time_is_an_instant_at_the_end() -> None:
    msgs = [inbound(0), turn(10, text="looking"), result(12)]
    kinds = {k: (s, e) for k, _, _, s, e in blocks(msgs)}
    assert kinds["thinking"] == (0, 10) and kinds["text"] == (10, 10)


def test_a_turn_of_only_text_spans_its_stream_and_a_reasoning_only_turn_has_no_call_or_output() -> (
    None
):
    assert blocks([inbound(0), turn(8, think="", text="hello", code=None)]) == [
        ("inbound", 0, 0, 0, 0),
        ("text", 1, 1, 0, 8),
    ]
    assert blocks([inbound(0), turn(8, code=None)]) == [
        ("inbound", 0, 0, 0, 0),
        ("thinking", 1, 1, 0, 8),
    ]


def test_blocks_follow_the_read_order_when_an_arrival_is_stamped_early() -> None:
    # The inbound message arrived at second 3 while the agent streamed until second 10.
    msgs = [inbound(0), turn(10), inbound(3), turn(20), result(30)]
    assert blocks(msgs) == [
        ("inbound", 0, 0, 0, 0),
        ("thinking", 1, 1, 0, 10),
        ("call", 1, 1, 10, 10),
        ("inbound", 2, 2, 10, 10),  # read at 10, not its arrival at 3
        ("thinking", 3, 3, 10, 20),
        ("call", 3, 3, 20, 20),
        ("output", 3, 4, 20, 30),
    ]


def test_a_result_without_a_recorded_body_start_shows_whole_with_its_header() -> None:
    old = ToolMessage(
        content="Code execution output [x]:\n\nfiles",
        tool_call_id="t",
        additional_kwargs={"ava_msg_type": "exec_output", "ava_created_at": at(25)},
    )
    (output,) = [b for b in display_blocks_of([inbound(0), turn(10), old]) if b.kind == "output"]
    assert output.preview == "Code execution output [x]: files"


def test_a_result_with_a_recorded_body_start_shows_only_the_body() -> None:
    (output,) = [
        b for b in display_blocks_of([inbound(0), turn(10), result(25)]) if b.kind == "output"
    ]
    assert output.preview == "files"


def display_blocks_of(msgs: Sequence[BaseMessage]) -> list[DisplayBlock]:
    return display_blocks(divide_units(msgs), msgs, read_times(msgs))
