from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException, Request
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from psycopg import Connection

from base.agents.history.checkpoint import single_segment_history
from base.agents.history.hierarchy.store import StoredNode
from base.agents.history.hierarchy.units import (
    display_blocks,
    divide_units,
    read_times,
)
from base.agents.history.hierarchy.usage import MessageUsage
from base.agents.history.timeline_inputs import TimelineReadInputs
from base.clock import Clock
from base.config import settings
from base.db import Database
from services.derived.insights.run_timeline import router
from services.derived.insights.run_timeline.history import HistoryView
from services.derived.insights.run_timeline.projection.derive import project_segment
from services.derived.insights.run_timeline.projection.jobs import IndexJobs
from services.derived.insights.run_timeline.projection.reader import Snapshot, TimelineReader

_TIMELINE_INPUTS = TimelineReadInputs(
    Clock.from_settings, lambda: settings.general.message_timestamps
)

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
    return [
        SystemMessage(content="prompt"),
        HumanMessage(
            content="start",
            additional_kwargs={"ava_msg_type": "inbound", "ava_created_at": at(0)},
        ),
        AIMessage(
            content=[{"type": "thinking", "thinking": "plan"}],
            tool_calls=[{"name": "execute_code", "args": {"code": "ls"}, "id": "t1"}],
            usage_metadata=usage(100),
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
            usage_metadata=usage(200),
            additional_kwargs={"ava_created_at": at(10)},
        ),
    ]


def view(messages: list[BaseMessage] | None = None) -> HistoryView:
    msgs = history_messages() if messages is None else messages
    read = read_times(msgs, timeline_inputs=_TIMELINE_INPUTS)
    units = display_blocks(divide_units(msgs, timeline_inputs=_TIMELINE_INPUTS), msgs, read)
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
    def __init__(self, database: Database, db_conn: Connection[Any]) -> None:
        self.view = view()
        self.nodes: list[StoredNode] = []
        self.db, self.conn = database, db_conn


def read(
    world: World,
    from_: datetime | None = None,
    to: datetime | None = None,
    *,
    recent_hours: int | None = None,
    max_units: int | None = None,
) -> router.RunTimelineResponse:
    world.conn.execute("DELETE FROM understanding_nodes WHERE agent_id = 405")
    for n in world.nodes:
        world.conn.execute(
            """INSERT INTO understanding_nodes
            (id, agent_id, depth, span_start, span_end, start_ts, end_ts, text, segment_key,
             text_hash, input_hash, children_count, model, engine_version, prompt_version, schema_version)
            VALUES (%s, 405, %s, %s, %s, %s, %s, %s, 's', 'h', 'i', 1, 'm', 'e', 'p', 1)""",
            (n.id, n.depth, n.span_start, n.span_end, n.start_ts, n.end_ts, n.text),
        )
    world.conn.commit()
    projection = project_segment("fixture", world.view.history, None, inputs=_TIMELINE_INPUTS)
    extent = world.view.extent
    if extent is not None and extent[0] == extent[1]:
        extent = extent[0], extent[1] + timedelta(seconds=1)
    cut = Snapshot(405, "fixture-snapshot", 0, "fixture", None, projection, extent)
    pool = world.db.pool(min_size=1, max_size=4)
    jobs = IndexJobs(lambda _agent: False)
    try:

        def no_body(_db: Database, _agent: int, _id: str):
            raise AssertionError("a window query must not read a checkpoint")

        reader = TimelineReader(
            world.db, pool, inputs=_TIMELINE_INPUTS, jobs=jobs, read_segment=no_body
        )

        def snapshot(_agent: int, _snapshot: str | None) -> Snapshot:
            return cut

        state = SimpleNamespace(
            run_timeline_reader=SimpleNamespace(get=snapshot, window=reader.window)
        )
        request = cast(Request, SimpleNamespace(app=SimpleNamespace(state=state)))
        return router.get_run_timeline(request, 405, from_, to, recent_hours, max_units)
    finally:
        jobs.close()
        pool.close()


def test_recent_day_is_relative_to_the_last_activity_and_keeps_the_lifetime(
    database: Database,
    db_conn: Connection[Any],
) -> None:
    world = World(database, db_conn)
    messages = history_messages()
    messages[-1].additional_kwargs["ava_created_at"] = at(3 * 24 * 60)
    world.view = view(messages)
    result = read(world, recent_hours=24, max_units=5000)
    assert result.lifetime is not None and result.lifetime.from_ == T0
    assert result.window.to == T0 + timedelta(days=3)
    assert result.window.from_ == T0 + timedelta(days=2)
    assert [(u.kind, u.i0) for u in result.units] == [("text", 4)]
    assert result.snapshot == "fixture-snapshot"


def test_dense_windows_report_omitted_detail_without_constructing_it(
    database: Database,
    db_conn: Connection[Any],
) -> None:
    world = World(database, db_conn)
    world.nodes = [stored(1, level=1, span=(1, 4), start=0, end=10)]
    result = read(world, max_units=4)
    assert result.units == [] and result.units_omitted == 5
    assert [n.id for n in result.nodes] == ["1"]
    assert len(read(world, max_units=5).units) == 5
    assert read(world, max_units=5).units_omitted == 0


def test_recent_window_rejects_conflicting_explicit_bounds(
    database: Database, db_conn: Connection[Any]
) -> None:
    with pytest.raises(HTTPException) as caught:
        read(World(database, db_conn), from_=T0, recent_hours=24)
    assert caught.value.status_code == 422


def test_default_window_is_the_lifetime_of_the_messages_read_times(
    database: Database,
    db_conn: Connection[Any],
) -> None:
    world = World(database, db_conn)

    world.nodes = [stored(1, level=1, span=(1, 4), start=-5, end=20)]
    result = read(world)
    assert (result.window.from_, result.window.to) == (T0, T0 + timedelta(minutes=10))
    assert result.lifetime == result.window


def test_all_levels_and_every_unit_are_served_in_the_lifetime_window(
    database: Database,
    db_conn: Connection[Any],
) -> None:
    world = World(database, db_conn)
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


def test_a_unit_names_the_leaf_covering_its_first_message(
    database: Database, db_conn: Connection[Any]
) -> None:
    world = World(database, db_conn)
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
    database: Database,
    db_conn: Connection[Any],
) -> None:
    world = World(database, db_conn)
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


def test_a_unit_no_leaf_covers_has_no_parent(database: Database, db_conn: Connection[Any]) -> None:
    world = World(database, db_conn)
    world.nodes = [stored(1, level=1, span=(2, 3), start=0, end=2)]
    result = read(world)
    assert {u.i0: u.parent for u in result.units} == {1: None, 2: "1", 4: None}


def test_a_narrowed_window_keeps_what_intersects_it(
    database: Database, db_conn: Connection[Any]
) -> None:
    world = World(database, db_conn)
    world.nodes = [
        stored(1, level=1, span=(1, 3), start=0, end=2),
        stored(2, level=1, span=(4, 4), start=10, end=10),
        stored(3, level=2, span=(1, 4), start=0, end=10),
    ]
    result = read(world, T0 + timedelta(minutes=5), T0 + timedelta(minutes=11))
    assert [n.id for n in result.nodes] == ["2", "3"]
    assert [(u.kind, u.i0) for u in result.units] == [("text", 4)]


def test_a_unit_straddling_the_window_edge_is_kept(
    database: Database, db_conn: Connection[Any]
) -> None:
    world = World(database, db_conn)
    result = read(world, T0 + timedelta(seconds=90), T0 + timedelta(minutes=5))
    assert [(u.kind, u.i0, u.i1) for u in result.units] == [
        ("output", 2, 3),
        ("text", 4, 4),
    ]


def test_an_agent_with_nothing_has_a_fallback_window_and_no_lifetime(
    database: Database,
    db_conn: Connection[Any],
) -> None:
    world = World(database, db_conn)
    world.view = view([SystemMessage(content="prompt")])
    result = read(world)
    assert result.lifetime is None
    assert result.window.to - result.window.from_ == timedelta(hours=24)
    assert result.nodes == [] and result.units == []


def test_an_orphan_node_is_left_out_and_the_page_still_answers(
    database: Database,
    db_conn: Connection[Any],
) -> None:
    world = World(database, db_conn)
    world.nodes = [
        stored(1, level=1, span=(1, 4), start=0, end=10),
        stored(2, level=1, span=(400, 410), start=11, end=12),
    ]
    result = read(world)
    assert [n.id for n in result.nodes] == ["1"]
    assert [u.kind for u in result.units] != []


@pytest.mark.parametrize(
    ("from_", "to"),
    [
        (datetime.fromisoformat("2026-10-04T12:00:00"), None),
        (T0 + timedelta(minutes=5), T0),
    ],
)
def test_a_bad_window_is_a_422(
    database: Database, db_conn: Connection[Any], from_: datetime, to: datetime | None
) -> None:
    world = World(database, db_conn)
    with pytest.raises(HTTPException) as caught:
        read(world, from_, to)
    assert caught.value.status_code == 422


def test_nodes_and_units_follow_the_read_order_when_the_stamps_do_not() -> None:

    msgs: list[BaseMessage] = [
        SystemMessage(content="prompt"),
        AIMessage(content="first", additional_kwargs={"ava_created_at": at(5)}),
        HumanMessage(
            content="late reader",
            additional_kwargs={"ava_msg_type": "inbound", "ava_created_at": at(3)},
        ),
        AIMessage(content="second", additional_kwargs={"ava_created_at": at(8)}),
    ]
    read = read_times(msgs, timeline_inputs=_TIMELINE_INPUTS)
    assert read == [
        None,
        T0 + timedelta(minutes=5),
        T0 + timedelta(minutes=5),
        T0 + timedelta(minutes=8),
    ]
    units = display_blocks(divide_units(msgs, timeline_inputs=_TIMELINE_INPUTS), msgs, read)
    spans = [(u.start, u.end) for u in units]
    assert all(a[1] <= b[0] for a, b in pairwise(spans))

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
    assert read_times(msgs, timeline_inputs=_TIMELINE_INPUTS) == raw


def test_a_recorded_pickup_time_is_the_read_time_and_older_messages_fall_back_to_arrival() -> None:
    msgs: list[BaseMessage] = [
        SystemMessage(content="prompt"),
        AIMessage(content="old", additional_kwargs={"ava_created_at": at(5)}),
        HumanMessage(
            content="picked up",
            additional_kwargs={
                "ava_msg_type": "inbound",
                "ava_created_at": at(3),
                "ava_picked_up_at": at(6),
            },
        ),
        HumanMessage(
            content="old inbound",
            additional_kwargs={"ava_msg_type": "inbound", "ava_created_at": at(4)},
        ),
    ]
    assert read_times(msgs, timeline_inputs=_TIMELINE_INPUTS) == [
        None,
        T0 + timedelta(minutes=5),
        T0 + timedelta(minutes=6),
        T0 + timedelta(minutes=6),
    ]


def test_a_block_carries_the_context_through_it_and_a_reply_its_request(
    database: Database,
    db_conn: Connection[Any],
) -> None:
    units = {(u.kind, u.i0): u for u in read(World(database, db_conn)).units}

    assert units[("inbound", 1)].context_total == 100

    thinking, call = units[("thinking", 2)], units[("call", 2)]
    assert thinking.context_total == 100 + (thinking.context_tokens or 0)
    assert call.context_total == 101

    assert units[("output", 2)].context_total == 200
    assert units[("text", 4)].context_total == 201
    assert {u.session for u in units.values()} == {0}

    assert [(k, i) for (k, i), u in units.items() if u.request is not None] == [
        ("thinking", 2),
        ("call", 2),
        ("text", 4),
    ]
    assert thinking.request is not None and (thinking.request.input, thinking.request.output) == (
        100,
        1,
    )
    request = units[("text", 4)].request
    assert request is not None and request.input == 200


def test_a_block_no_request_has_read_has_no_context_total(
    database: Database, db_conn: Connection[Any]
) -> None:
    world = World(database, db_conn)
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
    database: Database,
    db_conn: Connection[Any],
) -> None:
    world = World(database, db_conn)
    units = {(u.kind, u.i0): u for u in read(world).units}

    result = units[("output", 2)]
    assert (result.context_tokens, result.generation_tokens, result.estimated) == (99, None, False)

    inbound = units[("inbound", 1)]
    assert inbound.estimated is True
    assert 0 < (inbound.context_tokens or 0) < 100

    thinking, call = units[("thinking", 2)], units[("call", 2)]
    assert (thinking.estimated, call.estimated) == (True, True)
    assert (thinking.context_tokens or 0) + (call.context_tokens or 0) == 1
    assert (thinking.generation_tokens or 0) + (call.generation_tokens or 0) == 1

    done = units[("text", 4)]
    assert (done.context_tokens, done.generation_tokens, done.estimated) == (1, 1, False)


def test_a_unit_no_request_has_read_has_no_tokens(
    database: Database, db_conn: Connection[Any]
) -> None:
    world = World(database, db_conn)
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


def test_a_node_sums_the_context_tokens_of_its_messages(
    database: Database, db_conn: Connection[Any]
) -> None:
    world = World(database, db_conn)
    world.nodes = [stored(1, level=1, span=(1, 3), start=0, end=2)]
    node = read(world).nodes[0]
    units = [u for u in read(world).units if node.span_start <= u.i0 <= node.span_end]
    whole = [u for u in units if u.kind in ("inbound", "output")]
    assert node.estimated is True
    assert node.context_tokens is not None and node.context_tokens >= sum(
        u.context_tokens or 0 for u in whole
    )


def test_a_node_no_request_has_read_has_no_tokens(
    database: Database, db_conn: Connection[Any]
) -> None:
    world = World(database, db_conn)
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
