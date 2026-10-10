"""The run-timeline window: lifetime default from messages and nodes, units and nodes per window."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import HTTPException, Request
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from base.agents.history.checkpoint import single_segment_history
from base.agents.history.hierarchy.store import StoredNode
from base.agents.history.hierarchy.units import (
    display_blocks,
    divide_units,
    read_times,
)
from base.agents.history.hierarchy.usage import MessageUsage
from base.db import Database
from services.derived.insights.run_timeline import router
from services.derived.insights.run_timeline.history import HistoryView
from services.derived.insights.run_timeline.schemas import RunTimelineEvent

T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def at(minutes: int) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat()


def usage(tokens: int) -> dict[str, object]:
    return {
        "input_tokens": tokens,
        "output_tokens": 1,
        "total_tokens": tokens + 1,
        "input_token_details": {"cache_read": tokens // 2},
    }


def history_messages() -> list[BaseMessage]:
    """system, inbound@0, work@1..2 (call + result), text@10."""
    return [
        SystemMessage(content="prompt"),
        HumanMessage(
            content="start",
            additional_kwargs={"ava_msg_type": "inbound", "ava_created_at": at(0)},
        ),
        AIMessage(
            content=[{"type": "thinking", "thinking": "plan"}],
            tool_calls=[{"name": "execute_code", "args": {"code": "ls"}, "id": "t1"}],
            usage_metadata=usage(100),  # type: ignore[arg-type]
            additional_kwargs={"ava_created_at": at(1)},
        ),
        ToolMessage(
            content="out",
            tool_call_id="t1",
            additional_kwargs={
                "ava_msg_type": "exec_output",
                "ava_exec_body_start": 0,
                "ava_created_at": at(2),
            },
        ),
        AIMessage(
            content="done",
            usage_metadata=usage(200),  # type: ignore[arg-type]
            additional_kwargs={"ava_created_at": at(10)},
        ),
    ]


def view(messages: list[BaseMessage] | None = None) -> HistoryView:
    msgs = history_messages() if messages is None else messages
    read = read_times(msgs)
    units = display_blocks(divide_units(msgs), msgs, read)
    return HistoryView.of(single_segment_history(msgs), units, MessageUsage(msgs), read)


def stored(node_id: int, *, level: int, span: tuple[int, int], start: int, end: int) -> StoredNode:
    return StoredNode(
        id=node_id,
        depth=level,
        span_start=span[0],
        span_end=span[1],
        start_ts=T0 + timedelta(minutes=start),
        end_ts=T0 + timedelta(minutes=end),
        text=f"node {node_id}",
        parent_id=None,
        engine_version="0.3",
        prompt_version="0.3",
    )


class World:
    """Everything the endpoint reads, stubbed in one place."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.view = view()
        self.nodes: list[StoredNode] = []
        self.events: list[RunTimelineEvent] = []
        self.fresh_reads = 0
        monkeypatch.setattr(router, "load_nodes", self._nodes)
        monkeypatch.setattr(router, "load_generation_costs", self._costs)
        monkeypatch.setattr(router._lifecycle, "read", self._lifecycle)

    def get(self, _db: object, _agent: int, *, needs: int = 0) -> HistoryView:
        """The `HistoryViewCache.get` the app state serves."""
        self.fresh_reads += needs > 0
        return self.view

    def _nodes(self, _db: object, _agent: int) -> list[StoredNode]:
        return list(self.nodes)

    def _costs(self, _db: object, _agent: int) -> tuple[dict[int, object], dict[str, object]]:
        return {}, {}

    def _lifecycle(
        self, _db: object, _agent: int, _start: datetime, _end: datetime
    ) -> list[RunTimelineEvent]:
        return self.events


def read(
    world: World, from_: datetime | None = None, to: datetime | None = None
) -> router.RunTimelineResponse:
    state = SimpleNamespace(db=cast(Database, object()), run_timeline_views=world)
    request = cast(Request, SimpleNamespace(app=SimpleNamespace(state=state)))
    return router.get_run_timeline(request, 405, from_, to)


def test_default_window_is_the_lifetime_of_the_messages_read_times(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = World(monkeypatch)
    # The messages span minutes 0..10; a node's stored times (here -5..20) are not what is served:
    # it is placed on the read times of its messages (minutes 1..2).
    world.nodes = [stored(1, level=1, span=(1, 4), start=-5, end=20)]
    result = read(world)
    assert (result.window.from_, result.window.to) == (T0, T0 + timedelta(minutes=10))
    assert result.lifetime == result.window


def test_lifecycle_events_never_move_the_default_window(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(monkeypatch)
    world.events = [
        RunTimelineEvent(ts=T0 + timedelta(days=1), kind="spawn", label=None, source="user")
    ]
    result = read(world)
    assert (result.window.from_, result.window.to) == (T0, T0 + timedelta(minutes=10))
    assert [e.kind for e in result.events] == ["spawn"]


def test_all_levels_and_every_unit_are_served_in_the_lifetime_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = World(monkeypatch)
    world.nodes = [
        stored(1, level=1, span=(1, 3), start=0, end=2),
        stored(2, level=1, span=(4, 4), start=10, end=10),
        stored(3, level=2, span=(1, 4), start=0, end=10),
    ]
    result = read(world)
    assert [(n.id, n.level) for n in result.nodes] == [("1", 1), ("2", 1), ("3", 2)]
    assert [(u.kind, u.i0, u.i1) for u in result.units] == [
        ("inbound", 1, 1),
        ("thinking", 2, 2),
        ("call", 2, 2),
        ("output", 2, 3),
        ("text", 4, 4),
    ]
    leaf = result.nodes[0]
    assert (leaf.usage.calls, leaf.usage.input, leaf.usage.cache_read) == (1, 100, 50)
    assert result.nodes[2].usage.calls == 2 and result.nodes[2].usage.input == 300


def test_a_unit_names_the_leaf_covering_its_first_message(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(monkeypatch)
    world.nodes = [
        stored(1, level=1, span=(1, 3), start=0, end=2),
        stored(2, level=1, span=(4, 4), start=10, end=10),
        stored(3, level=2, span=(1, 4), start=0, end=10),
    ]
    result = read(world)
    assert [(u.kind, u.i0, u.parent) for u in result.units] == [
        ("inbound", 1, "1"),
        ("thinking", 2, "1"),
        ("call", 2, "1"),
        ("output", 2, "1"),
        ("text", 4, "2"),
    ]


def test_an_inbound_block_names_the_inbound_row_the_checkpoint_stamped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = World(monkeypatch)
    messages = history_messages()
    messages[1].additional_kwargs["ava_inbound_id"] = 77
    world.view = view(messages)
    result = read(world)
    assert {u.kind: u.inbound_id for u in result.units} == {
        "inbound": 77,
        "thinking": None,
        "call": None,
        "output": None,
        "text": None,
    }


def test_a_unit_no_leaf_covers_has_no_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(monkeypatch)
    world.nodes = [stored(1, level=1, span=(2, 3), start=0, end=2)]
    result = read(world)
    assert {u.i0: u.parent for u in result.units} == {1: None, 2: "1", 4: None}


def test_a_narrowed_window_keeps_what_intersects_it(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(monkeypatch)
    world.nodes = [
        stored(1, level=1, span=(1, 3), start=0, end=2),
        stored(2, level=1, span=(4, 4), start=10, end=10),
        stored(3, level=2, span=(1, 4), start=0, end=10),
    ]
    result = read(world, T0 + timedelta(minutes=5), T0 + timedelta(minutes=11))
    assert [n.id for n in result.nodes] == ["2", "3"]
    assert [(u.kind, u.i0) for u in result.units] == [("text", 4)]


def test_a_unit_straddling_the_window_edge_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(monkeypatch)
    result = read(world, T0 + timedelta(seconds=90), T0 + timedelta(minutes=5))
    assert [(u.kind, u.i0, u.i1) for u in result.units] == [
        ("output", 2, 3),
        ("text", 4, 4),  # a text-only turn spans its stream: minute 2 to minute 10
    ]


def test_an_agent_with_nothing_has_a_fallback_window_and_no_lifetime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = World(monkeypatch)
    world.view = view([SystemMessage(content="prompt")])
    result = read(world)
    assert result.lifetime is None
    assert result.window.to - result.window.from_ == timedelta(hours=24)
    assert result.nodes == [] and result.units == []


def test_a_node_beyond_the_cached_view_asks_for_a_view_that_reaches_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = World(monkeypatch)
    world.nodes = [stored(1, level=1, span=(1, 4), start=0, end=10)]
    read(world)
    assert world.fresh_reads == 0


def test_an_orphan_node_is_left_out_and_the_page_still_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A node whose span lies past the history (checkpoints rolled back or restored) used to fail
    the whole read with an IndexError (a 500 for the agent's page, for good)."""
    world = World(monkeypatch)
    world.nodes = [
        stored(1, level=1, span=(1, 4), start=0, end=10),
        stored(2, level=1, span=(400, 410), start=11, end=12),  # past the 5 messages
    ]
    result = read(world)
    assert [n.id for n in result.nodes] == ["1"]
    assert [u.kind for u in result.units] != []


def test_a_failed_lifecycle_read_leaves_the_markers_out(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(monkeypatch)

    def boom(*_args: object) -> list[RunTimelineEvent]:
        raise RuntimeError("audit table unreadable")

    monkeypatch.setattr(router._lifecycle, "read", boom)
    assert read(world).events == []


@pytest.mark.parametrize(
    ("from_", "to"),
    [
        (datetime.fromisoformat("2026-10-04T12:00:00"), None),  # naive
        (T0 + timedelta(minutes=5), T0),  # reversed
    ],
)
def test_a_bad_window_is_a_422(
    monkeypatch: pytest.MonkeyPatch, from_: datetime, to: datetime | None
) -> None:
    world = World(monkeypatch)
    with pytest.raises(HTTPException) as caught:
        read(world, from_, to)
    assert caught.value.status_code == 422


def test_nodes_and_units_follow_the_read_order_when_the_stamps_do_not() -> None:
    # An inbound message that arrived while the agent streamed (minute 3) sits after the
    # AIMessage (stamped at its end, minute 5): its own stamp is earlier than the one before it.
    msgs: list[BaseMessage] = [
        SystemMessage(content="prompt"),
        AIMessage(content="first", additional_kwargs={"ava_created_at": at(5)}),
        HumanMessage(
            content="late reader",
            additional_kwargs={"ava_msg_type": "inbound", "ava_created_at": at(3)},
        ),
        AIMessage(content="second", additional_kwargs={"ava_created_at": at(8)}),
    ]
    read = read_times(msgs)
    assert read == [
        None,
        T0 + timedelta(minutes=5),
        T0 + timedelta(minutes=5),
        T0 + timedelta(minutes=8),
    ]
    units = display_blocks(divide_units(msgs), msgs, read)
    spans = [(u.start, u.end) for u in units]
    assert all(a[1] <= b[0] for a, b in pairwise(spans))
    # The raw stamps stay what they were.
    assert msgs[2].additional_kwargs["ava_created_at"] == at(3)


def test_the_read_time_is_the_stamp_itself_when_the_stamps_are_monotone() -> None:
    msgs = history_messages()
    raw = [
        None,
        T0,
        T0 + timedelta(minutes=1),
        T0 + timedelta(minutes=2),
        T0 + timedelta(minutes=10),
    ]
    assert read_times(msgs) == raw


def test_a_recorded_pickup_time_is_the_read_time_and_older_messages_fall_back_to_arrival() -> None:
    msgs: list[BaseMessage] = [
        SystemMessage(content="prompt"),
        AIMessage(content="old", additional_kwargs={"ava_created_at": at(5)}),
        HumanMessage(
            content="picked up",
            additional_kwargs={
                "ava_msg_type": "inbound",
                "ava_created_at": at(3),  # arrival, while the agent was streaming
                "ava_picked_up_at": at(6),
            },
        ),
        HumanMessage(
            content="old inbound",
            additional_kwargs={"ava_msg_type": "inbound", "ava_created_at": at(4)},
        ),
    ]
    assert read_times(msgs) == [
        None,
        T0 + timedelta(minutes=5),
        T0 + timedelta(minutes=6),  # the recorded pickup, not the arrival
        T0 + timedelta(minutes=6),  # no pickup: arrival, lifted to the read order
    ]


def test_a_view_behind_the_tree_is_rebuilt_but_not_more_than_every_two_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.derived.insights.run_timeline import history as history_module

    loads: list[int] = []
    clock = {"now": 100.0}

    def load(_db: object, _agent: int) -> object:
        loads.append(1)
        return single_segment_history(history_messages())

    def head(_db: object, _agent: int) -> str:
        return "c1"

    monkeypatch.setattr(history_module, "load_checkpoint_history_full", load)
    # Even an unchanged checkpoint id does not keep a view the tree reaches past.
    monkeypatch.setattr(history_module, "latest_checkpoint_id", head)
    monkeypatch.setattr(history_module.time, "monotonic", lambda: clock["now"])
    cache = history_module.HistoryViewCache()
    db = cast(Database, object())
    cache.get(db, 1)
    cache.get(db, 1, needs=99)  # an orphan reaches past the history: too soon to rebuild
    assert len(loads) == 1
    clock["now"] += 2.5
    cache.get(db, 1, needs=99)  # long enough since the build: rebuilt once ...
    cache.get(db, 1, needs=99)  # ... and not again at once
    assert len(loads) == 2


def test_a_block_carries_the_context_through_it_and_a_reply_its_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    units = {(u.kind, u.i0): u for u in read(World(monkeypatch)).units}
    # The first request sent 100 tokens (the prompt and the ask); the ask is the context through it.
    assert units[("inbound", 1)].context_total == 100
    # The reply's one output token is split between its thinking and its call: they add up to it.
    thinking, call = units[("thinking", 2)], units[("call", 2)]
    assert thinking.context_total == 100 + (thinking.context_tokens or 0)
    assert call.context_total == 101
    # The tool result adds its 99; the next request sent exactly that.
    assert units[("output", 2)].context_total == 200
    assert units[("text", 4)].context_total == 201
    assert {u.session for u in units.values()} == {0}
    # The reply's blocks carry its request (200 is the second one's input); other blocks carry none.
    assert [(k, i) for (k, i), u in units.items() if u.request is not None] == [
        ("thinking", 2),
        ("call", 2),
        ("text", 4),
    ]
    assert thinking.request is not None and (thinking.request.input, thinking.request.output) == (
        100,
        1,
    )
    assert units[("text", 4)].request is not None and units[("text", 4)].request.input == 200  # type: ignore[union-attr]


def test_a_block_no_request_has_read_has_no_context_total(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(monkeypatch)
    world.view = view(
        [
            *history_messages(),
            HumanMessage(
                content="later",
                additional_kwargs={"ava_msg_type": "inbound", "ava_created_at": at(20)},
            ),
        ]
    )
    later = [u for u in read(world).units if u.i0 == 5]
    assert [(u.context_total, u.request) for u in later] == [(None, None)]


def test_units_carry_their_tokens_and_whether_they_are_estimated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = World(monkeypatch)
    units = {(u.kind, u.i0): u for u in read(world).units}
    # The tool result is alone between the two requests: 200 - 100 - 1 (the first reply's output).
    result = units[("output", 2)]
    assert (result.context_tokens, result.generation_tokens, result.estimated) == (99, None, False)
    # The inbound shares the first request's input with the system prompt: a share, so estimated.
    inbound = units[("inbound", 1)]
    assert inbound.estimated is True
    assert 0 < (inbound.context_tokens or 0) < 100
    # The first reply's one output token is divided between its reasoning and its call.
    thinking, call = units[("thinking", 2)], units[("call", 2)]
    assert (thinking.estimated, call.estimated) == (True, True)
    assert (thinking.context_tokens or 0) + (call.context_tokens or 0) == 1
    assert (thinking.generation_tokens or 0) + (call.generation_tokens or 0) == 1
    # A reply that is only text is the provider's own output count.
    done = units[("text", 4)]
    assert (done.context_tokens, done.generation_tokens, done.estimated) == (1, 1, False)


def test_a_unit_no_request_has_read_has_no_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(monkeypatch)
    world.view = view(
        [
            *history_messages(),
            HumanMessage(
                content="later",
                additional_kwargs={"ava_msg_type": "inbound", "ava_created_at": at(20)},
            ),
        ]
    )
    later = [u for u in read(world).units if u.i0 == 5]
    assert [(u.context_tokens, u.generation_tokens, u.estimated) for u in later] == [
        (None, None, None)
    ]


def test_a_node_sums_the_context_tokens_of_its_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(monkeypatch)
    world.nodes = [stored(1, level=1, span=(1, 3), start=0, end=2)]
    node = read(world).nodes[0]
    units = [u for u in read(world).units if node.span_start <= u.i0 <= node.span_end]
    whole = [u for u in units if u.kind in ("inbound", "output")]
    assert node.estimated is True
    assert node.context_tokens is not None and node.context_tokens >= sum(
        u.context_tokens or 0 for u in whole
    )


def test_a_node_no_request_has_read_has_no_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(monkeypatch)
    world.view = view(
        [
            *history_messages(),
            HumanMessage(
                content="later",
                additional_kwargs={"ava_msg_type": "inbound", "ava_created_at": at(20)},
            ),
        ]
    )
    world.nodes = [stored(1, level=1, span=(5, 5), start=20, end=20)]
    node = read(world).nodes[0]
    assert (node.context_tokens, node.estimated) == (None, None)
