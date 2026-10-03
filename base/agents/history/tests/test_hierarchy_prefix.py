"""The request prefix of a generation call is the agent's own head per segment.

Contracts locked here: a node inside compaction segment k rides segment k's own
SystemMessage plus that segment's messages up to the node (never the stitched
history of every earlier segment); the prefix is capped by the model's window
and a node that would overflow falls back to the material-only request; a
segment without a head SystemMessage sends material-only. Model calls go through
a deterministic fake — no network.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from base.agents.history.checkpoint import FullHistory
from base.agents.history.hierarchy import pipeline as pipeline_module
from base.agents.history.hierarchy.pipeline import build_agent_tree
from base.agents.history.hierarchy.prefix import (
    REQUEST_OVERHEAD_TOKENS,
    PrefixPlanner,
    message_tokens,
)
from base.db import Database

MODEL = "deepseek-v4-flash"
T0 = "2026-09-12T12:00:00+08:00"


def inbound(text: str) -> HumanMessage:
    return HumanMessage(
        content=text, additional_kwargs={"ava_msg_type": "inbound", "ava_created_at": T0}
    )


def summary() -> HumanMessage:
    return HumanMessage(
        content="[system] compacted",
        additional_kwargs={"ava_msg_type": "compact_summary", "ava_created_at": T0},
    )


def two_segment_history(*, second_head: SystemMessage | None) -> FullHistory:
    """[S1, m0..m5] then a compaction join: [S2?, summary, n0..n5], stitched."""
    first_head = SystemMessage(content="SP-1")
    messages: list[BaseMessage] = [
        first_head,
        *(inbound(f"m{i}") for i in range(6)),
        summary(),
        *(inbound(f"n{i}") for i in range(6)),
    ]
    return FullHistory(messages, (first_head, second_head), (1, 7))


class FakeLLM:
    def __init__(self) -> None:
        self.calls: list[list[BaseMessage]] = []

    def bind_tools(self, _tools: list[Any]) -> FakeLLM:
        return self

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        self.calls.append(messages)
        return AIMessage(content="summary text")


def build_calls(
    monkeypatch: pytest.MonkeyPatch,
    history: FullHistory,
    *,
    context_window_tokens: int | None = None,
) -> list[list[BaseMessage]]:

    def load(_db: Database, _agent_id: int) -> FullHistory:
        return history

    monkeypatch.setattr(pipeline_module, "load_checkpoint_history_full", load)
    fake = FakeLLM()
    tree = build_agent_tree(
        cast(Database, object()),
        7,
        llm=fake,
        model=MODEL,
        tools=[object()],
        context_window_tokens=context_window_tokens,
    )
    assert tree.errors == () and len(fake.calls) == 2
    return sorted(fake.calls, key=len)


def test_a_later_segment_rides_its_own_head_not_the_stitched_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    second_head = SystemMessage(content="SP-2")
    history = two_segment_history(second_head=second_head)
    first_call, second_call = build_calls(monkeypatch, history)

    # Segment 1's node: the first head and nothing before its span.
    assert list(first_call[:-1]) == [history.messages[0]]
    # Segment 2's node: segment 2's own head plus its body up to the node (the
    # compaction summary) — not segment 1's messages, which the agent no longer
    # sent after the compaction.
    assert list(second_call[:-1]) == [second_head, history.messages[7]]


def test_a_segment_without_a_head_sends_material_only(monkeypatch: pytest.MonkeyPatch) -> None:
    history = two_segment_history(second_head=None)
    material_only, headed = build_calls(monkeypatch, history)  # sorted by length
    assert len(material_only) == 1  # segment 2 has no head: no prefix to ride
    assert list(headed[:-1]) == [history.messages[0]]


def test_a_prefix_over_the_window_cap_falls_back_to_material_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = two_segment_history(second_head=SystemMessage(content="SP-2"))
    calls = build_calls(monkeypatch, history, context_window_tokens=10)
    assert [len(call) for call in calls] == [1, 1]


def test_the_cap_counts_the_segment_body_not_earlier_segments() -> None:
    big = inbound("x" * 4000)
    head = SystemMessage(content="SP")
    # Segment 0 is huge; segment 1 is tiny. A node in segment 1 must not pay for segment 0.
    messages: list[BaseMessage] = [head, big, big, big, summary(), inbound("tiny")]
    history = FullHistory(messages, (head, head), (1, 4))
    # The cap fits one big message beside the fixed request overhead, not two.
    cap = REQUEST_OVERHEAD_TOKENS + message_tokens(big) + 200
    planner = PrefixPlanner(history, window_tokens=int(cap / 0.8) + 1)
    assert planner.prefix_for(5, 10) == (head, messages[4])
    assert planner.capped == 0
    # A node late in segment 0 carries two big messages and overflows.
    assert planner.prefix_for(3, 10) == ()
    assert planner.capped == 1


def test_no_cap_without_a_window() -> None:
    head = SystemMessage(content="SP")
    planner = PrefixPlanner.single_segment([head, inbound("a"), inbound("b")])
    assert len(planner.prefix_for(2, 10**9)) == 2
