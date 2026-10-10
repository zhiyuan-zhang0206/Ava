"""History producers retain independent live policy and original conditional clock reads."""

from datetime import UTC, datetime

from langchain_core.messages import HumanMessage

from base.agents.history.timeline import build_timeline_items, timeline_default_limit
from base.agents.history.timeline_inputs import TimelineReadInputs
from base.clock import Clock, ClockConfig


def test_renderers_are_lazy_isolated_live_and_read_flag_before_clock() -> None:
    reads: list[str] = []
    flags = [True, False]
    instant = datetime(2026, 10, 4, 12, tzinfo=UTC)
    clock = Clock(ClockConfig("UTC", "UTC", False), now=lambda: instant)

    def flag(index: int) -> bool:
        reads.append(f"flag{index}")
        return flags[index]

    def make_clock(index: int) -> Clock:
        reads.append(f"clock{index}")
        return clock

    first = TimelineReadInputs(lambda: make_clock(0), lambda: flag(0))
    second = TimelineReadInputs(lambda: make_clock(1), lambda: flag(1))
    assert reads == []
    message = HumanMessage(content="summary", additional_kwargs={"ava_msg_type": "compact_summary"})
    rendered, _ = build_timeline_items([message], [], inputs=first)
    assert rendered[0].payload == "Compact summary [2026-10-04 12:00:00]:\n\nsummary"
    rendered, _ = build_timeline_items([message], [], inputs=second)
    assert rendered[0].payload == "Compact summary:\n\nsummary"
    assert reads == ["flag0", "clock0", "flag1"]
    flags[0], flags[1] = False, True
    build_timeline_items([message], [], inputs=first)
    build_timeline_items([message], [], inputs=second)
    assert reads[-3:] == ["flag0", "flag1", "clock1"]


def test_plain_messages_skip_policy_and_limit_is_live() -> None:
    def unused() -> bool:
        raise AssertionError("plain messages have no compact timestamp policy")

    def no_clock() -> Clock:
        raise AssertionError("plain messages construct no clock")

    items, count = build_timeline_items(
        [HumanMessage(content="plain")], [], inputs=TimelineReadInputs(no_clock, unused)
    )
    assert count == 1 and items[0].payload == "plain"
    limit = 7
    assert timeline_default_limit(limit_reader=lambda: limit) == 7
    limit = 11
    assert timeline_default_limit(limit_reader=lambda: limit) == 11
