"""The chunk queue against the test Postgres: enqueue, claim, retry, backlog, node write."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

from base.agents.history.hierarchy.chunks import (
    Chunk,
    ChunkJob,
    GroupNode,
    backlog,
    claim_job,
    enqueue_chunk,
    finish_job,
    release_job,
    write_group_nodes,
)


async def _rows(pool: AsyncConnectionPool, sql: str, *params: object) -> list[tuple[Any, ...]]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, params)  # pyright: ignore[reportArgumentType]
        return await cur.fetchall()


async def test_enqueue_is_idempotent_per_chunk_identity(aops_pool: AsyncConnectionPool) -> None:
    for _ in range(2):
        assert await enqueue_chunk(
            aops_pool, 7, compact_version=2, chunk=Chunk(1, 40), end_msg_id="m39"
        )
    assert await enqueue_chunk(
        aops_pool,
        7,
        compact_version=2,
        chunk=Chunk(1, 55),
        end_msg_id="m54",
        boundary_checkpoint_id="cp-1",
    )
    rows = await _rows(
        aops_pool,
        "SELECT start_index, end_index, end_msg_id, boundary_checkpoint_id, status"
        " FROM understanding_chunk_jobs ORDER BY end_index",
    )
    assert rows == [(1, 40, "m39", None, "pending"), (1, 55, "m54", "cp-1", "pending")]


async def test_enqueue_failure_is_reported_not_raised(aops_pool: AsyncConnectionPool) -> None:
    # A chunk whose last message has no id cannot be located later: refused, not raised.
    assert not await enqueue_chunk(
        aops_pool, 7, compact_version=0, chunk=Chunk(0, 5), end_msg_id=None
    )
    assert await _rows(aops_pool, "SELECT 1 FROM understanding_chunk_jobs") == []


async def test_claim_takes_oldest_first_and_never_the_same_row_twice(
    aops_pool: AsyncConnectionPool,
) -> None:
    for agent, end in ((1, 10), (2, 20), (3, 30)):
        await enqueue_chunk(
            aops_pool, agent, compact_version=0, chunk=Chunk(0, end), end_msg_id=f"m{end}"
        )
    claimed = await asyncio.gather(*(claim_job(aops_pool) for _ in range(4)))
    taken = sorted(job.end_index for job in claimed if job is not None)
    assert taken == [10, 20, 30]  # every row once, the fourth claimer found nothing
    assert all(job.attempts == 1 for job in claimed if job is not None)


async def test_released_job_waits_out_the_spacing_and_a_lapsed_lease_is_taken_over(
    aops_pool: AsyncConnectionPool,
) -> None:
    await enqueue_chunk(aops_pool, 1, compact_version=0, chunk=Chunk(0, 10), end_msg_id="m10")
    job = await claim_job(aops_pool)
    assert job is not None
    await release_job(aops_pool, job.id, error="checkpoint behind")
    assert await claim_job(aops_pool) is None  # just retried: inside the spacing
    await _rows(
        aops_pool,
        "UPDATE understanding_chunk_jobs SET claimed_at = now() - interval '1 hour'"
        " WHERE id = %s RETURNING id",
        job.id,
    )
    again = await claim_job(aops_pool)
    assert again is not None and again.id == job.id and again.attempts == 2
    # A running claim past the lease (its claimer died) is taken over too.
    await _rows(
        aops_pool,
        "UPDATE understanding_chunk_jobs SET claimed_at = now() - interval '1 hour'"
        " WHERE id = %s RETURNING id",
        job.id,
    )
    taken_over = await claim_job(aops_pool)
    assert taken_over is not None and taken_over.attempts == 3


async def test_backlog_counts_pending_and_running_and_ages_the_oldest_pending(
    aops_pool: AsyncConnectionPool,
) -> None:
    empty = await backlog(aops_pool)
    assert (empty.pending, empty.running, empty.oldest_pending_age_seconds) == (0, 0, 0.0)
    for end in (10, 20):
        await enqueue_chunk(
            aops_pool, 1, compact_version=0, chunk=Chunk(0, end), end_msg_id=f"m{end}"
        )
    await _rows(
        aops_pool,
        "UPDATE understanding_chunk_jobs SET created_at = now() - interval '90 seconds'"
        " WHERE end_index = 20 RETURNING id",
    )
    job = await claim_job(aops_pool)  # the oldest id (10) goes running
    assert job is not None
    depth = await backlog(aops_pool)
    assert (depth.pending, depth.running) == (1, 1)
    assert 89 < depth.oldest_pending_age_seconds < 120
    await finish_job(aops_pool, job.id, status="done")
    assert (await backlog(aops_pool)).running == 0


async def test_finish_job_refuses_a_non_terminal_status(aops_pool: AsyncConnectionPool) -> None:
    with pytest.raises(ValueError, match="terminal"):
        await finish_job(aops_pool, 1, status="running")


async def test_one_agents_jobs_are_claimed_in_order_and_one_at_a_time(
    aops_pool: AsyncConnectionPool,
) -> None:
    # The next chunk starts where the previous one left an open group, so a later job of an
    # agent waits for every earlier one to be finished; another agent's job does not wait.
    for agent, end in ((1, 10), (1, 20), (2, 15)):
        await enqueue_chunk(
            aops_pool, agent, compact_version=0, chunk=Chunk(0, end), end_msg_id=f"m{end}"
        )
    first = await claim_job(aops_pool)
    assert first is not None and (first.agent_id, first.end_index) == (1, 10)
    other = await claim_job(aops_pool)
    assert other is not None and (other.agent_id, other.end_index) == (2, 15)
    assert await claim_job(aops_pool) is None  # agent 1's second job: the first still runs
    await release_job(aops_pool, first.id, error="checkpoint behind")
    assert await claim_job(aops_pool) is None  # still ahead of it, only waiting out the spacing
    await finish_job(aops_pool, first.id, status="done")
    second = await claim_job(aops_pool)
    assert second is not None and second.end_index == 20


def _job(job_id: int, version: int = 1, start: int = 0, end: int = 2) -> ChunkJob:
    return ChunkJob(
        id=job_id, agent_id=7, compact_version=version, start_index=start, end_index=end,
        end_msg_id="a", boundary_checkpoint_id=None, attempts=1,
    )  # fmt: skip


async def test_group_nodes_are_upserted_and_a_rewrite_does_not_duplicate_them(
    aops_pool: AsyncConnectionPool,
) -> None:
    await enqueue_chunk(aops_pool, 7, compact_version=1, chunk=Chunk(0, 5), end_msg_id="m5")
    [(job_id,)] = await _rows(aops_pool, "SELECT id FROM understanding_chunk_jobs")
    # A row of another cut that overlaps the span: a chunk write must leave it alone.
    await _rows(
        aops_pool,
        "INSERT INTO understanding_nodes (agent_id, depth, span_start, span_end, segment_key,"
        " text, text_hash, input_hash, children_count, model, engine_version, prompt_version,"
        " schema_version) VALUES (7, 1, 5, 12, 'other', 't', 'h', 'i', 0, 'm', 'e', 'p', 1)"
        " RETURNING id",
    )
    at = [datetime(2026, 10, 5, hour, tzinfo=UTC) for hour in (1, 2, 3)]
    job = _job(job_id, end=5)
    nodes = [GroupNode((10, 11), at[0], at[1], "first"), GroupNode((12, 14), at[1], at[2], "next")]
    await write_group_nodes(aops_pool, job, nodes, model="m1")
    await write_group_nodes(aops_pool, job, nodes[:1], model="m1")  # same span: updated, not added
    rows = await _rows(
        aops_pool,
        "SELECT span_start, span_end, depth, text, segment_key, engine_version, start_ts, end_ts"
        " FROM understanding_nodes WHERE agent_id = 7 ORDER BY span_start",
    )
    assert [r[:6] for r in rows] == [
        (5, 12, 1, "t", "other", "e"),
        (10, 11, 1, "first", "chunk:v1", "chunk-0.2"),
        (12, 14, 1, "next", "chunk:v1", "chunk-0.2"),
    ]
    assert rows[1][6:] == (at[0], at[1])


async def test_a_replay_claim_runs_segments_side_by_side_and_one_segment_in_order(
    aops_pool: AsyncConnectionPool,
) -> None:
    # Segments are independent, so a replay may take the first job of each segment together;
    # within one segment the order stays.
    for segment, end in ((0, 10), (0, 20), (1, 15), (1, 25)):
        await enqueue_chunk(
            aops_pool, 1, compact_version=segment, chunk=Chunk(0, end), end_msg_id=f"m{end}"
        )
    await enqueue_chunk(aops_pool, 2, compact_version=0, chunk=Chunk(0, 5), end_msg_id="m5")
    claimed = [await claim_job(aops_pool, agent_id=1, segment_parallel=True) for _ in range(3)]
    assert [(j.compact_version, j.end_index) for j in claimed if j] == [(0, 10), (1, 15)]
    assert claimed[2] is None  # each segment's next job waits; agent 2's is not this replay's
    done = claimed[0]
    assert done is not None
    await finish_job(aops_pool, done.id, status="done")
    nxt = await claim_job(aops_pool, agent_id=1, segment_parallel=True)
    assert nxt is not None and (nxt.compact_version, nxt.end_index) == (0, 20)


async def test_live_claims_never_split_one_agent_across_segments(
    aops_pool: AsyncConnectionPool,
) -> None:
    for segment, end in ((0, 10), (1, 15)):
        await enqueue_chunk(
            aops_pool, 1, compact_version=segment, chunk=Chunk(0, end), end_msg_id=f"m{end}"
        )
    first = await claim_job(aops_pool)
    assert first is not None and first.compact_version == 0
    assert await claim_job(aops_pool) is None


async def test_concurrent_claims_never_hand_one_agent_two_jobs(
    aops_pool: AsyncConnectionPool,
) -> None:
    for agent in (1, 2, 3):
        for end in (10, 20):
            await enqueue_chunk(
                aops_pool, agent, compact_version=0, chunk=Chunk(0, end), end_msg_id=f"m{end}"
            )
    claimed = [j for j in await asyncio.gather(*(claim_job(aops_pool) for _ in range(6))) if j]
    assert sorted((j.agent_id, j.end_index) for j in claimed) == [(1, 10), (2, 10), (3, 10)]
