from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from cProfile import Profile
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from fastapi import HTTPException
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.base import CheckpointMetadata, empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection

from base.agents.history.checkpoint import (
    CheckpointReadError,
    FullHistory,
    load_checkpoint_history_full,
)
from base.agents.history.checkpoint_segment import read_checkpoint_segment
from base.agents.history.hierarchy.serve import serve_nodes
from base.agents.history.hierarchy.store import load_nodes
from base.agents.history.timeline_inputs import TimelineReadInputs
from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from base.db import Database
from services.derived.insights.run_timeline.projection.derive import segment_view
from services.derived.insights.run_timeline.projection.jobs import IndexJobs, index_next
from services.derived.insights.run_timeline.projection.reader import TimelineReader
from services.derived.insights.run_timeline.projection.source import capture_source
from services.derived.insights.run_timeline.tokens import timeline_units

T0 = datetime(2026, 10, 1, tzinfo=UTC)
AGENT = 2697


def messages(day: int) -> list[BaseMessage]:
    return [
        SystemMessage(content=f"system {day}"),
        HumanMessage(
            content=f"ask {day}",
            additional_kwargs={
                "ava_msg_type": "inbound",
                "ava_inbound_body_start": 0,
                "ava_created_at": (T0 + timedelta(days=day)).isoformat(),
            },
        ),
        AIMessage(
            content=f"answer {day}",
            additional_kwargs={
                "ava_created_at": (T0 + timedelta(days=day, minutes=1)).isoformat(),
                "ava_usage": {"cost_usd": 0.1234567891},
            },
            usage_metadata={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
        ),
    ]


class World:
    def __init__(self, database: Database, url: str) -> None:
        self.db, self.url = database, url
        self.pool = database.pool(min_size=1, max_size=4)
        self.loads: list[str] = []
        self.clock = 0.0
        self.version = 0
        self.inputs = TimelineReadInputs(
            lambda: Clock(ClockConfig("UTC", "UTC", False), timers=SYSTEM_TIMERS),
            lambda: False,
        )
        self.jobs = IndexJobs(self.step)
        self.reader = self.new_reader()

    def new_reader(self) -> TimelineReader:
        return TimelineReader(
            self.db,
            self.pool,
            inputs=self.inputs,
            jobs=self.jobs,
            read_segment=self.read_segment,
            timer=lambda: self.clock,
        )

    def read_segment(self, db: Database, agent: int, checkpoint: str) -> FullHistory:
        self.loads.append(checkpoint)
        return read_checkpoint_segment(db, agent, checkpoint)

    def put(self, body: list[BaseMessage], *, sealed: bool, agent: int = AGENT) -> str:
        checkpoint = empty_checkpoint()
        self.version += 1
        version = str(self.version)
        checkpoint["channel_values"] = {"messages": body}
        checkpoint["channel_versions"] = {"messages": version}
        with PostgresSaver.from_conn_string(self.url) as saver:
            saver.setup()
            saved = saver.put(
                {"configurable": {"thread_id": str(agent), "checkpoint_ns": ""}},
                checkpoint,
                cast(
                    CheckpointMetadata,
                    {
                        "source": "input",
                        "step": self.version,
                        "parents": {},
                        "compact_boundary": sealed,
                    },
                ),
                {"messages": version},
            )
        return saved.get("configurable", {})["checkpoint_id"]

    def step(self, agent: int = AGENT) -> bool:
        return index_next(self.db, agent, inputs=self.inputs, read_segment=self.read_segment)

    def prepare(self) -> None:
        while self.step():
            pass
        self.loads.clear()


@pytest.fixture
def world(database: Database, db_url: str) -> Iterator[World]:
    result = World(database, db_url)
    try:
        yield result
    finally:
        result.jobs.close()
        result.pool.close()


def node(conn: Connection[Any], span: tuple[int, int], *, level: int = 1) -> None:
    conn.execute(
        """INSERT INTO understanding_nodes
        (agent_id, depth, span_start, span_end, start_ts, end_ts, segment_key, text,
         text_hash, input_hash, children_count, model, engine_version, prompt_version, schema_version)
        VALUES (%s, %s, %s, %s, %s, %s, 's', 'summary', 'h', 'i', 1, 'm', 'e', 'p', 1)""",
        (AGENT, level, *span, T0 - timedelta(days=1), T0 + timedelta(days=20)),
    )
    conn.commit()


def test_cold_recent_range_and_pan_never_read_historical_bodies(world: World) -> None:
    first = world.put(messages(0), sealed=True)
    world.put(messages(1), sealed=True)
    latest = world.put(messages(2), sealed=False)
    world.prepare()
    cut = world.reader.get(AGENT)
    assert world.loads == [latest]
    assert cut.count == 7 and cut.sessions == 3
    _, blocks, omitted = world.reader.window(
        cut, T0 + timedelta(days=2), T0 + timedelta(days=3), 5000
    )
    assert [b.i0 for b in blocks] == [5, 6] and omitted == 0
    _, older, _ = world.reader.window(cut, T0, T0 + timedelta(hours=1), 5000)
    assert [b.i0 for b in older] == [1, 2]
    assert world.loads == [latest]
    expanded = world.reader.span(cut, 1, 2)
    assert len(expanded) == 1 and expanded[0][1].history.messages[1].text == "ask 0"
    assert world.loads == [latest, first]
    world.reader.span(cut, 5, 6)
    assert world.loads == [latest, first]


def test_projection_survives_a_cold_reader_and_latest_boundary_is_not_duplicated(
    world: World,
) -> None:
    world.put(messages(0), sealed=True)
    world.put(messages(1), sealed=True)
    world.prepare()
    cut = world.new_reader().get(AGENT)
    assert cut.tail is None and cut.count == 5 and cut.sessions == 2
    assert world.loads == []
    _, blocks, _ = world.reader.window(cut, T0, T0 + timedelta(days=2), 5000)
    assert [b.i0 for b in blocks] == [1, 2, 3, 4]


def test_cross_segment_nodes_equal_full_history_including_cost_and_zero_tokens(
    world: World,
    db_conn: Connection[Any],
) -> None:
    world.put(messages(0), sealed=True)
    world.put(messages(1), sealed=True)
    world.put(messages(2), sealed=False)
    world.prepare()
    node(db_conn, (1, 4))
    node(db_conn, (5, 6))
    node(db_conn, (1, 6), level=2)
    reference = segment_view(
        load_checkpoint_history_full(world.db, AGENT), None, inputs=world.inputs
    )
    expected = serve_nodes(
        load_nodes(world.db, AGENT), reference.usage, {}, {}, reference.read, reference.units
    )
    cut = world.reader.get(AGENT)
    found, blocks, _ = world.reader.window(cut, T0, T0 + timedelta(days=4), 5000)
    assert len(found) == len(expected) == 3
    for actual, old in zip(found, expected, strict=True):
        assert (actual.start, actual.end, actual.span_start, actual.span_end) == (
            old.start,
            old.end,
            old.span_start,
            old.span_end,
        )
        assert actual.usage.model_dump() == vars(old.usage)
        records = reference.tokens[old.span_start : old.span_end + 1]
        assert actual.context_tokens == sum(r.context_tokens or 0 for r in records)
        assert actual.estimated == any(r.source == "estimated" for r in records)
    expected_blocks = timeline_units(reference, T0, T0 + timedelta(days=4))
    assert [b.model_copy(update={"parent": None}) for b in blocks] == expected_blocks

    narrow, _, _ = world.reader.window(
        cut, T0 + timedelta(days=2), T0 + timedelta(days=2, seconds=30), 5000
    )
    assert [n.level for n in narrow] == [1, 2]


def test_capture_rejects_unindexed_compaction_before_a_new_tail(world: World) -> None:
    world.put(messages(0), sealed=True)
    world.prepare()
    world.put(messages(1), sealed=True)
    world.put(messages(2), sealed=False)
    with world.pool.connection() as conn:
        source = capture_source(conn, AGENT)
    assert not source.ready and source.boundaries == 2
    assert world.loads == []
    world.prepare()
    cut = world.reader.get(AGENT)
    assert cut.count == 7


@pytest.mark.parametrize("compact_kind", ["compact_summary", "compact_request"])
def test_compact_replacement_closes_work_before_the_next_segment(
    world: World, compact_kind: str
) -> None:
    first, replacement = messages(0), messages(1)
    for body, call_id in ((first, "old"), (replacement, "new")):
        body[2] = AIMessage(
            content="",
            tool_calls=[{"name": "execute_code", "args": {"code": "work()"}, "id": call_id}],
            additional_kwargs=body[2].additional_kwargs,
            usage_metadata={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
        )

    replacement[1] = HumanMessage(
        content="Summary of prior work",
        additional_kwargs={
            "ava_msg_type": compact_kind,
            "ava_created_at": (T0 + timedelta(days=1)).isoformat(),
        },
    )
    replacement.append(
        ToolMessage(
            content="new result",
            tool_call_id="new",
            additional_kwargs={
                "ava_msg_type": "exec_output",
                "ava_exec_body_start": 0,
                "ava_created_at": (T0 + timedelta(days=1, minutes=2)).isoformat(),
            },
        )
    )
    world.put(first, sealed=True)
    world.put(replacement, sealed=False)
    world.prepare()
    reference = segment_view(
        load_checkpoint_history_full(world.db, AGENT), None, inputs=world.inputs
    )
    cut = world.reader.get(AGENT)
    _, blocks, omitted = world.reader.window(cut, T0, T0 + timedelta(days=2), 5000)
    assert omitted == 0
    assert blocks == timeline_units(reference, T0, T0 + timedelta(days=2))
    assert [(block.i0, block.i1) for block in blocks if block.kind == "output"] == [(4, 5)]
    assert [(block.i0, block.session) for block in blocks if block.kind == "note"] == [(3, 1)]


def test_snapshot_is_pinned_and_expiration_does_not_read_again(world: World) -> None:
    first = world.put(messages(0), sealed=False)
    cut = world.reader.get(AGENT)
    world.put(messages(1), sealed=False)
    assert world.reader.get(AGENT, cut.id) is cut and world.loads == [first]
    world.clock = 601
    with pytest.raises(HTTPException) as caught:
        world.reader.get(AGENT, cut.id)
    assert caught.value.status_code == 409 and world.loads == [first]


def test_next_request_across_a_segment_boundary_fetches_only_its_own_segment(world: World) -> None:
    world.put(messages(0), sealed=True)
    second = world.put(messages(1), sealed=True)
    latest = world.put(messages(2), sealed=False)
    world.prepare()
    cut = world.reader.get(AGENT)
    idx, ts, segment, view = world.reader.request(cut, 3)
    assert (idx, ts, segment.session) == (4, T0 + timedelta(days=1), 1)
    head = view.history.segment_heads[0]
    assert head is not None and head.text == "system 1"
    assert world.loads == [latest, second]
    idx, _, segment, _ = world.reader.request(cut, 100)
    assert idx == 6 and segment.session == 2 and world.loads == [latest, second]


def test_dense_window_omits_all_detail_but_preserves_summaries(
    world: World, db_conn: Connection[Any]
) -> None:
    world.put(messages(0), sealed=True)
    world.put(messages(1), sealed=False)
    world.prepare()
    node(db_conn, (1, 4))
    cut = world.reader.get(AGENT)
    with Profile() as profile:
        nodes, blocks, omitted = world.reader.window(cut, T0, T0 + timedelta(days=2), 1)
    assert not any(
        not isinstance(e.code, str) and e.code.co_name == "ai_message_parts"
        for e in profile.getstats()
    )
    assert len(nodes) == 1 and blocks == [] and omitted == 4


def test_projection_preserves_nul_in_previews(world: World) -> None:
    raw = messages(0)
    raw[1].content = "before\x00after"
    world.put(raw, sealed=True)
    world.prepare()
    cut = world.reader.get(AGENT)
    _, blocks, _ = world.reader.window(cut, T0, T0 + timedelta(hours=1), 5000)
    assert "\x00" in blocks[0].preview


def test_competing_writers_publish_one_ordered_prefix(world: World) -> None:
    first = world.put(messages(0), sealed=True)
    second = world.put(messages(1), sealed=True)
    world.put(messages(2), sealed=False)
    with ThreadPoolExecutor(max_workers=2) as workers:
        assert list(workers.map(world.step, [AGENT, AGENT])) == [True, True]
    assert world.loads == [first, second]
    with world.pool.connection() as conn:
        source = capture_source(conn, AGENT)
    assert source.ready and source.previous is not None
    assert source.previous.session == 1 and source.previous.message_end == 5
    assert not world.step()


def test_failed_segment_keeps_the_committed_prefix_and_retries(world: World) -> None:
    world.put(messages(0), sealed=True)
    assert world.step()
    world.put(messages(1), sealed=True)

    def unavailable(_db: Database, _agent: int, _checkpoint: str) -> FullHistory:
        raise CheckpointReadError("missing ancestry")

    with pytest.raises(CheckpointReadError, match="missing ancestry"):
        index_next(world.db, AGENT, inputs=world.inputs, read_segment=unavailable)
    with world.pool.connection() as conn:
        rows = conn.execute(
            "SELECT session, message_end FROM run_timeline_segments WHERE agent_id = %s",
            (AGENT,),
        ).fetchall()
    assert rows == [(0, 3)]
    assert world.step()
    with world.pool.connection() as conn:
        assert capture_source(conn, AGENT).ready
