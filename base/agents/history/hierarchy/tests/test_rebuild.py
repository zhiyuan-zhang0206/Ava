# pyright: reportUnknownMemberType = warning
# pyright: reportUnknownArgumentType = warning
# pyright: reportUnknownLambdaType = warning
# pyright: reportUnknownVariableType = warning
"""The upper-level rebuild: its queue (claim order, who blocks whom) and the replay."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage
from psycopg_pool import AsyncConnectionPool

from base.agents.history.hierarchy import chunk_consumer as loop
from base.agents.history.hierarchy import group_consumer as gc
from base.agents.history.hierarchy.chunks import (
    Chunk,
    ChunkJob,
    GroupNode,
    claim_job,
    enqueue_chunk,
    finish_job,
    write_group_nodes,
)
from base.agents.history.hierarchy.group import Group, GroupCall, OpenNode
from base.agents.history.hierarchy.rebuild import (
    claim_rebuild,
    finish_rebuild,
    rebuild_pending,
    release_rebuild,
    run_rebuild,
)
from base.config import settings
from base.host.env.agent_slices import ModelOverrides

AGENT = 7


async def _rows(pool: AsyncConnectionPool, sql: str, *params: object) -> list[tuple[Any, ...]]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, params)  # pyright: ignore[reportArgumentType]
        return await cur.fetchall() if cur.description else []


async def _leaf(
    pool: AsyncConnectionPool, i: int, *, depth: int = 1, engine: str = "chunk-0.2"
) -> int:
    start = datetime(2026, 10, 5, tzinfo=UTC) + timedelta(hours=i)
    rows = await _rows(
        pool,
        "INSERT INTO understanding_nodes (agent_id, depth, span_start, span_end, start_ts, end_ts,"
        " segment_key, text, text_hash, input_hash, children_count, model, engine_version,"
        " prompt_version, schema_version)"
        " VALUES (%s, %s, %s, %s, %s, %s, 'k', %s, 'h', 'i', 0, 'm', %s, 'p', 1) RETURNING id",
        AGENT,
        depth,
        i * 10,
        i * 10 + 9,
        start,
        start + timedelta(minutes=30),
        f"leaf {i}" if depth == 1 else f"stale {i}",
        engine,
    )
    return int(rows[0][0])


async def _enqueue_rebuild(pool: AsyncConnectionPool, agent: int = AGENT) -> int:
    rows = await _rows(
        pool, "INSERT INTO understanding_rebuilds (agent_id) VALUES (%s) RETURNING id", agent
    )
    return int(rows[0][0])


@pytest.fixture
def _groups(monkeypatch: pytest.MonkeyPatch) -> list[list[int]]:
    """Grouping checks of five open nodes (no decay), closing the first three; each ask is recorded."""
    monkeypatch.setattr(settings.agent, "understanding_group_check_open", 5)
    monkeypatch.setattr(settings.agent, "understanding_group_check_decay", 1)
    monkeypatch.setattr(gc, "MIN_CHECK_OPEN", 1)
    monkeypatch.setattr(
        gc, "_group_model", lambda *_a: ("deepseek-flash", ModelOverrides.from_pins(None))
    )
    asked: list[list[int]] = []

    def generate(
        _models: object,
        model: str,
        _o: object,
        _level: int,
        nodes: list[OpenNode],
        calls: list,
        _agent_id: int,
    ) -> list[Group]:
        asked.append([n.id for n in nodes])
        calls.append(GroupCall(0, model, "prompt", AIMessage(content="r"), 5.0, None, None))
        return [Group(nodes[0].id, nodes[2].id, f"group of {nodes[0].id}")]

    monkeypatch.setattr(gc, "_generate", generate)
    return asked


async def _tree(pool: AsyncConnectionPool) -> list[tuple[Any, ...]]:
    return await _rows(
        pool,
        "SELECT depth, span_start, span_end, children_count FROM understanding_nodes"
        " WHERE agent_id = %s AND depth > 1 ORDER BY depth, span_start",
        AGENT,
    )


async def test_the_rebuild_replays_leaves_in_order_and_replaces_the_old_upper_levels(
    aops_pool: AsyncConnectionPool, _groups: list[list[int]]
) -> None:
    leaves = [await _leaf(aops_pool, i) for i in range(12)]
    stale = await _leaf(aops_pool, 50, depth=2)
    await _rows(
        aops_pool,
        "INSERT INTO understanding_group_state (agent_id, level, last_checked_open) VALUES (%s, 1, 99)",
        AGENT,
    )
    await _rows(
        aops_pool, "UPDATE understanding_nodes SET parent_id = %s WHERE id = %s", stale, leaves[0]
    )

    assert await run_rebuild(aops_pool, MagicMock(), MagicMock(), AGENT) == 12

    # Each check saw only the leaves that had "landed" by then (the replay horizon), though all
    # twelve exist: the first five; then, with two left open and five more arrived, seven.
    assert _groups == [leaves[:5], leaves[3:10]]
    # Non-overlapping parents whose range is their children's: three leaves each.
    assert await _tree(aops_pool) == [(2, 0, 29, 3), (2, 30, 59, 3)]
    assert await _rows(aops_pool, "SELECT 1 FROM understanding_nodes WHERE id = %s", stale) == []
    children = await _rows(
        aops_pool,
        "SELECT p.span_start, p.span_end, min(c.span_start), max(c.span_end), count(*)"
        " FROM understanding_nodes p JOIN understanding_nodes c ON c.parent_id = p.id"
        " WHERE p.agent_id = %s GROUP BY p.id ORDER BY p.span_start",
        AGENT,
    )
    assert [(a, b, c, d) for a, b, c, d, _ in children] == [
        (0, 29, 0, 29),
        (30, 59, 30, 59),
    ]


async def test_rebuilding_twice_gives_the_same_tree(
    aops_pool: AsyncConnectionPool, _groups: list[list[int]]
) -> None:
    for i in range(12):
        await _leaf(aops_pool, i)
    await run_rebuild(aops_pool, MagicMock(), MagicMock(), AGENT)
    once = await _tree(aops_pool)
    await run_rebuild(aops_pool, MagicMock(), MagicMock(), AGENT)
    assert await _tree(aops_pool) == once and len(once) == 2


async def test_a_rebuild_touches_only_its_own_agent(
    aops_pool: AsyncConnectionPool, _groups: list[list[int]]
) -> None:
    for i in range(6):
        await _leaf(aops_pool, i)
    other = await _rows(
        aops_pool,
        "INSERT INTO understanding_nodes (agent_id, depth, span_start, span_end, start_ts, end_ts,"
        " segment_key, text, text_hash, input_hash, children_count, model, engine_version,"
        " prompt_version, schema_version) VALUES (8, 2, 0, 5, now(), now(), 'k', 't', 'h', 'i', 0,"
        " 'm', 'group-0.1', 'p', 1) RETURNING id",
    )
    await run_rebuild(aops_pool, MagicMock(), MagicMock(), AGENT)
    assert await _rows(aops_pool, "SELECT 1 FROM understanding_nodes WHERE id = %s", other[0][0])


async def test_the_claim_waits_for_the_agents_chunk_jobs_and_chunk_jobs_wait_for_a_running_rebuild(
    aops_pool: AsyncConnectionPool,
) -> None:
    await enqueue_chunk(aops_pool, AGENT, compact_version=0, chunk=Chunk(0, 10), end_msg_id="m10")
    rebuild_id = await _enqueue_rebuild(aops_pool)
    assert await rebuild_pending(aops_pool, AGENT)
    assert await claim_rebuild(aops_pool) is None  # a chunk job of the agent is live
    job = await claim_job(aops_pool)
    assert job is not None
    assert await claim_rebuild(aops_pool) is None  # running counts as live
    await finish_job(aops_pool, job.id, status="done")

    claimed = await claim_rebuild(aops_pool)
    assert claimed is not None and (claimed.id, claimed.agent_id, claimed.attempts) == (
        rebuild_id,
        AGENT,
        1,
    )
    assert not await rebuild_pending(aops_pool, AGENT)
    # While it runs, the agent's next chunk job is held back; another agent's is not.
    await enqueue_chunk(aops_pool, AGENT, compact_version=0, chunk=Chunk(10, 20), end_msg_id="m20")
    assert await claim_job(aops_pool) is None
    await enqueue_chunk(aops_pool, 8, compact_version=0, chunk=Chunk(0, 5), end_msg_id="x5")
    other = await claim_job(aops_pool)
    assert other is not None and other.agent_id == 8
    await finish_rebuild(aops_pool, claimed.id, status="done", leaves=4)
    held = await claim_job(aops_pool)
    assert held is not None and held.agent_id == AGENT


async def test_a_rebuild_handed_back_waits_out_the_spacing_and_one_rebuild_runs_per_agent(
    aops_pool: AsyncConnectionPool,
) -> None:
    first = await _enqueue_rebuild(aops_pool)
    second = await _enqueue_rebuild(aops_pool)  # a build that found the first already running
    claimed = await claim_rebuild(aops_pool)
    assert claimed is not None and claimed.id == first
    assert await claim_rebuild(aops_pool) is None  # the agent has one running
    await release_rebuild(aops_pool, first, error="boom")
    again = await claim_rebuild(aops_pool)  # the handed-back one waits out the spacing
    assert again is not None and again.id == second
    await finish_rebuild(aops_pool, second, status="done")
    assert await claim_rebuild(aops_pool) is None
    await _rows(
        aops_pool,
        "UPDATE understanding_rebuilds SET claimed_at = now() - interval '1 hour' WHERE id = %s",
        first,
    )
    retaken = await claim_rebuild(aops_pool)
    assert retaken is not None and (retaken.id, retaken.attempts) == (first, 2)


async def test_the_consumer_runs_a_claimed_rebuild_and_settles_it(
    aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[int] = []

    async def fake_rebuild(
        _pool: object, _db: object, _models: object, agent: int, **_kw: object
    ) -> int:
        ran.append(agent)
        return 7

    monkeypatch.setattr(loop, "run_rebuild", fake_rebuild)
    rebuild_id = await _enqueue_rebuild(aops_pool)
    await loop._Consumer(aops_pool, MagicMock(), []).run_until_idle()
    assert ran == [AGENT]
    assert await _rows(
        aops_pool, "SELECT status, leaves FROM understanding_rebuilds WHERE id = %s", rebuild_id
    ) == [("done", 7)]


async def test_a_failing_rebuild_is_retried_then_failed(
    aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_a: object, **_kw: object) -> int:
        raise RuntimeError("provider down")

    monkeypatch.setattr(loop, "run_rebuild", broken)
    monkeypatch.setattr(loop.telemetry, "emit", lambda *_a, **_kw: None)
    rebuild_id = await _enqueue_rebuild(aops_pool)
    consumer = loop._Consumer(aops_pool, MagicMock(), [])
    for attempt in (1, 2, 3):
        await _rows(
            aops_pool,
            "UPDATE understanding_rebuilds SET claimed_at = now() - interval '1 hour' WHERE id = %s",
            rebuild_id,
        )
        await consumer.run_until_idle()
        status = (
            await _rows(
                aops_pool,
                "SELECT status, attempts, error FROM understanding_rebuilds WHERE id = %s",
                rebuild_id,
            )
        )[0]
        assert status[1] == attempt
    assert status[0] == "failed" and "provider down" in status[2]


async def _land(pool: AsyncConnectionPool, i: int) -> None:
    """Land leaf `i` the way a chunk job does (same spans as `_leaf`)."""
    at = datetime(2026, 10, 5, tzinfo=UTC) + timedelta(hours=i)
    job = ChunkJob(1, AGENT, 0, i * 10, i * 10 + 9, "m", None, 1)
    node = GroupNode((i * 10, i * 10 + 9), at, at + timedelta(minutes=30), f"leaf {i}")
    await write_group_nodes(pool, job, [node], model="m")


async def _open_before_parented(pool: AsyncConnectionPool) -> list[tuple[Any, ...]]:
    return await _rows(
        pool,
        "SELECT o.id FROM understanding_nodes o WHERE o.agent_id = %s AND o.depth = 1"
        " AND o.parent_id IS NULL AND EXISTS (SELECT 1 FROM understanding_nodes p"
        " WHERE p.agent_id = o.agent_id AND p.depth = 1 AND p.parent_id IS NOT NULL"
        " AND p.span_start > o.span_end)",
        AGENT,
    )


async def test_a_leaf_landing_before_a_grouped_one_queues_one_rebuild_that_closes_the_gap(
    aops_pool: AsyncConnectionPool, _groups: list[list[int]]
) -> None:
    """The later segment lands first and is grouped; the earlier one arrives afterwards."""
    for i in range(5, 10):
        await _land(aops_pool, i)
    await gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), AGENT)
    assert not await rebuild_pending(aops_pool, AGENT)  # in order so far: nothing queued

    for i in range(5):
        await _land(aops_pool, i)
    assert await rebuild_pending(aops_pool, AGENT)
    assert len(await _rows(aops_pool, "SELECT 1 FROM understanding_rebuilds")) == 1
    assert await _open_before_parented(aops_pool)  # the gap exists until the rebuild runs

    await run_rebuild(aops_pool, MagicMock(), MagicMock(), AGENT)
    assert await _open_before_parented(aops_pool) == []


async def test_leaves_landing_in_order_queue_no_rebuild(
    aops_pool: AsyncConnectionPool, _groups: list[list[int]]
) -> None:
    for i in range(10):
        await _land(aops_pool, i)
        await gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), AGENT)
    assert await _rows(aops_pool, "SELECT 1 FROM understanding_rebuilds") == []
    await _land(aops_pool, 9)  # a rewrite of an existing, grouped span is not out of order
    assert await _rows(aops_pool, "SELECT 1 FROM understanding_rebuilds") == []
