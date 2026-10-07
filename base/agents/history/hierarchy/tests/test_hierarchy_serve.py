"""`base.agents.history.hierarchy.serve` — stored nodes served with their two cost figures."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from langchain_core.messages import AIMessage, BaseMessage

from base.agents.history.hierarchy.serve import serve_nodes
from base.agents.history.hierarchy.store import StoredNode
from base.agents.history.hierarchy.usage import GenerationUsage, MessageUsage

T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
T1 = datetime(2026, 10, 4, 13, 0, tzinfo=UTC)

READ = [T0 + timedelta(minutes=n) for n in range(6)]

MESSAGES: list[BaseMessage] = [
    AIMessage(
        content="x",
        usage_metadata={"input_tokens": 10 * n, "output_tokens": n, "total_tokens": 11 * n},
    )
    for n in range(1, 7)
]


def node(
    node_id: int,
    *,
    level: int,
    span: tuple[int, int],
    start: datetime | None = T0,
    end: datetime | None = T1,
    parent_id: int | None = None,
) -> StoredNode:
    return StoredNode(
        id=node_id,
        depth=level,  # StoredNode.depth is the ENGINE level (1 = finest)
        span_start=span[0],
        span_end=span[1],
        start_ts=start,
        end_ts=end,
        text=f"node {node_id}",
        parent_id=parent_id,
        engine_version="0.3",
        prompt_version="0.3",
    )


def test_every_level_is_served_finest_first_with_stable_levels() -> None:
    nodes = [
        node(3, level=2, span=(0, 5)),
        node(2, level=1, span=(3, 5), parent_id=3),
        node(1, level=1, span=(0, 2), parent_id=3),
    ]
    served = serve_nodes(nodes, MessageUsage(MESSAGES), {}, READ)
    assert [(n.id, n.level, n.parent) for n in served] == [
        ("1", 1, "3"),
        ("2", 1, "3"),
        ("3", 2, None),
    ]


def test_a_node_carries_the_agent_cost_over_its_span() -> None:
    (served,) = serve_nodes([node(1, level=1, span=(1, 2))], MessageUsage(MESSAGES), {}, READ)
    assert (served.usage.calls, served.usage.input, served.usage.output) == (2, 50, 5)


def test_only_a_leaf_with_a_call_record_carries_generation_cost() -> None:
    gen = GenerationUsage(calls=2, input=300, cache_read=250, output=40, seconds=12.5)
    nodes = [node(1, level=1, span=(0, 2)), node(2, level=2, span=(0, 2))]
    leaf, parent = serve_nodes(nodes, MessageUsage(MESSAGES), {(0, 2): gen}, READ)
    assert leaf.generation == gen
    assert parent.generation is None


def test_a_node_with_unknown_time_is_not_served() -> None:
    nodes = [node(1, level=1, span=(0, 1), start=None, end=None)]
    assert serve_nodes(nodes, MessageUsage(MESSAGES), {}, READ) == []


def test_a_span_beyond_the_history_is_an_error() -> None:
    with pytest.raises(IndexError):
        serve_nodes([node(1, level=1, span=(4, 9))], MessageUsage(MESSAGES), {}, READ)


def test_a_node_is_placed_on_the_read_times_of_its_first_and_last_message() -> None:
    read = [T0, T0 + timedelta(minutes=5), T0 + timedelta(minutes=5), T0 + timedelta(minutes=9)] + [
        T0 + timedelta(minutes=9)
    ] * 2
    first, second, parent = serve_nodes(
        [
            node(1, level=1, span=(0, 1), start=T0, end=T0 + timedelta(minutes=1)),
            node(2, level=1, span=(2, 3), start=T0 + timedelta(minutes=2), end=T0),
            node(3, level=2, span=(0, 3)),
        ],
        MessageUsage(MESSAGES),
        {},
        read,
    )
    assert (first.start, first.end) == (T0, T0 + timedelta(minutes=5))
    assert (second.start, second.end) == (T0 + timedelta(minutes=5), T0 + timedelta(minutes=9))
    assert first.end <= second.start  # the stored times of these two overlapped
    assert (parent.start, parent.end) == (T0, T0 + timedelta(minutes=9))
