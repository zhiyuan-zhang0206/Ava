"""Inbound producers own live timestamp readers without changing early-return semantics."""

from datetime import UTC, datetime

import pytest

from base.agents.messages.envelope import EnvelopeReadInputs, inbound_head, wrap_inbound
from base.clock import Clock, ClockConfig


def test_envelopes_are_lazy_isolated_and_live() -> None:
    reads: list[str] = []
    flags = [True, False]
    instant = datetime(2026, 10, 4, 12, tzinfo=UTC)
    clock = Clock(ClockConfig("UTC", "UTC", False), now=lambda: instant)

    def enabled(index: int) -> bool:
        reads.append(f"flag{index}")
        return flags[index]

    def make_clock(index: int) -> Clock:
        reads.append(f"clock{index}")
        return clock

    first = EnvelopeReadInputs(lambda: make_clock(0), lambda: enabled(0))
    second = EnvelopeReadInputs(lambda: make_clock(1), lambda: enabled(1))
    assert reads == []
    assert wrap_inbound("body", "agent:7", inputs=first) == (
        "Agent 7 [2026-10-04 12:00:00]:\n\nbody"
    )
    assert wrap_inbound("body", "agent:7", inputs=second) == "Agent 7:\n\nbody"
    assert reads == ["flag0", "clock0", "flag1"]
    flags[0], flags[1] = False, True
    assert inbound_head("user", inputs=first) == ""
    assert inbound_head("user", inputs=second, created_at=instant) == "[2026-10-04 12:00:00]\n\n"
    assert reads[-3:] == ["flag0", "flag1", "clock1"]


def test_system_and_caller_headers_do_not_read_policy_or_clock() -> None:
    def unused() -> bool:
        raise AssertionError("early envelope returns do not read policy")

    def no_clock() -> Clock:
        raise AssertionError("early envelope returns do not construct a clock")

    inputs = EnvelopeReadInputs(no_clock, unused)
    assert inbound_head("system:update", inputs=inputs) == "[system] "
    assert inbound_head("external_agent:codex:run-42", inputs=inputs).startswith("External agent")


def test_unknown_source_reads_policy_at_original_validation_point() -> None:
    reads: list[str] = []

    def enabled() -> bool:
        reads.append("flag")
        return False

    def no_clock() -> Clock:
        raise AssertionError("disabled timestamps construct no clock")

    with pytest.raises(ValueError, match="Unrecognized inbound source"):
        inbound_head("unknown", inputs=EnvelopeReadInputs(no_clock, enabled))
    assert reads == ["flag"]
