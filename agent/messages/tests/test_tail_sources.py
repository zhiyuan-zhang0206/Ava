import pytest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage

from agent.messages import (
    inbound_message,
    tail_has_recallable_inbound,
    tail_has_skill_recall_inbound,
)


def _inbound(source: str) -> AnyMessage:
    return inbound_message(content="hello", source=source, inbound_id=1, body_start=0)


@pytest.mark.parametrize("source", ["user", "ui:page:dashboard", "schedule:7"])
def test_a_person_or_a_schedule_wakes_skill_recall(source: str) -> None:
    assert tail_has_skill_recall_inbound([AIMessage(content="prev", id="a0"), _inbound(source)])


@pytest.mark.parametrize(
    "source", ["agent:402", "system", "system:update", "watcher:3", "shell:9", "external_agent:x"]
)
def test_every_other_source_does_not(source: str) -> None:
    assert not tail_has_skill_recall_inbound([AIMessage(content="prev", id="a0"), _inbound(source)])


def test_one_person_among_other_inbounds_is_enough() -> None:
    tail = [AIMessage(content="prev", id="a0"), _inbound("agent:5"), _inbound("user")]
    assert tail_has_skill_recall_inbound(tail)


def test_only_the_tail_since_the_last_assistant_turn_counts() -> None:
    tail = [_inbound("user"), AIMessage(content="answered", id="a1"), _inbound("agent:5")]
    assert not tail_has_skill_recall_inbound(tail)


def test_a_message_that_is_not_an_inbound_does_not_count() -> None:
    assert not tail_has_skill_recall_inbound([HumanMessage(content="user", id="h0")])


def test_the_memory_recall_gate_keeps_its_wider_meaning() -> None:
    tail = [AIMessage(content="prev", id="a0"), _inbound("agent:5")]
    assert tail_has_recallable_inbound(tail)
    assert not tail_has_skill_recall_inbound(tail)
