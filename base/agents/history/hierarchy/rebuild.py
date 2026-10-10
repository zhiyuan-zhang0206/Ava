"""Upper-level rebuild — the queue (`understanding_rebuilds`) and the replay that grows the tree again.

A manual build (`build.py`) describes sessions' level 1 out of order and in bulk, so the levels above
cannot be grown the way live leaves grow them. A build therefore enqueues one `rebuild` job for its
agent; the consumer loop claims it only when the agent has no chunk job pending or running, drops
every node above level 1 together with the grouping cursor (`understanding_group_state`), and
replays the leaves in message order, running the grouping checks after each — the tree grows exactly
as it does when leaves land live (`group_consumer.run_group_checks`, with the leaf's end as the replay
horizon `upto`). Interrupted at any point, the next claim starts over from the lifted state, so a
rebuild is idempotent.

Builds of one agent merge: a build finds the agent's pending rebuild (under an advisory lock that
serializes builds per agent) and reuses it; the pending row stays locked until the build commits, so
the claimer cannot take it between the build's chunk jobs and its rebuild. A rebuild already running
is not reused (it may have lifted before the new leaves landed): the build adds a pending one, which
the claim holds back until the running one ends. While a rebuild runs, the agent's chunk jobs are
not claimed (`chunks.py`), so nothing writes leaves under it.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Executor
from dataclasses import dataclass

from psycopg_pool import AsyncConnectionPool

from base.agents.history.hierarchy.chunks import CLAIM_LEASE_SECONDS, RETRY_SPACING_SECONDS
from base.agents.history.hierarchy.group_consumer import (
    GroupingModels,
    UnderstandingReadInputs,
    run_group_checks,
)
from base.db import Database
from base.db.transaction import async_write_transaction

# A rebuild is retried after a crash or an error this often before it is given up on.
MAX_REBUILD_ATTEMPTS = 3
# A grouping check of an earlier leaf may still be finishing when the last chunk job ends; the
# rebuild waits for it this long (polling) before it lifts the tree.
IDLE_WAIT_SECONDS = 600.0
_IDLE_POLL_SECONDS = 2.0

_CLAIM_SQL = """
UPDATE understanding_rebuilds SET status = 'running', attempts = attempts + 1, claimed_at = now()
WHERE id = (
    SELECT r.id FROM understanding_rebuilds r
    WHERE ((r.status = 'pending'
            AND (r.claimed_at IS NULL OR r.claimed_at < now() - make_interval(secs => %(spacing)s)))
        OR (r.status = 'running' AND r.claimed_at < now() - make_interval(secs => %(lease)s)))
      AND NOT EXISTS (
        SELECT 1 FROM understanding_chunk_jobs j
        WHERE j.agent_id = r.agent_id AND j.status IN ('pending', 'running'))
      AND NOT EXISTS (
        SELECT 1 FROM understanding_rebuilds o
        WHERE o.agent_id = r.agent_id AND o.id <> r.id AND o.status = 'running'
          AND o.claimed_at >= now() - make_interval(secs => %(lease)s))
    ORDER BY r.id
    FOR UPDATE OF r SKIP LOCKED
    LIMIT 1
)
RETURNING id, agent_id, attempts
"""


@dataclass(frozen=True)
class RebuildJob:
    """One claimed rebuild row."""

    id: int
    agent_id: int
    attempts: int


async def claim_rebuild(pool: AsyncConnectionPool) -> RebuildJob | None:
    """Claim the oldest rebuild whose agent has no live chunk job, or None when nothing is due."""
    async with async_write_transaction(pool) as conn, conn.cursor() as cur:
        await cur.execute(
            _CLAIM_SQL, {"spacing": RETRY_SPACING_SECONDS, "lease": CLAIM_LEASE_SECONDS}
        )
        row = await cur.fetchone()
    return None if row is None else RebuildJob(int(row[0]), int(row[1]), int(row[2]))


async def finish_rebuild(
    pool: AsyncConnectionPool,
    rebuild_id: int,
    *,
    status: str,
    leaves: int = 0,
    error: str | None = None,
) -> None:
    """Close a claimed rebuild as `done` (with the leaves it replayed) or `failed`."""
    if status not in ("done", "failed"):
        raise ValueError(f"unknown terminal status {status!r}")
    async with async_write_transaction(pool) as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE understanding_rebuilds SET status = %s, leaves = %s, error = %s,"
            " finished_at = now() WHERE id = %s",
            (status, leaves, error, rebuild_id),
        )


async def release_rebuild(
    pool: AsyncConnectionPool, rebuild_id: int, *, error: str, count_attempt: bool = True
) -> None:
    """Hand a claimed rebuild back to the queue; it is retried after the spacing."""
    async with async_write_transaction(pool) as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE understanding_rebuilds SET status = 'pending', error = %s, claimed_at = now(),"
            " attempts = CASE WHEN %s::boolean THEN attempts ELSE greatest(attempts - 1, 0) END"
            " WHERE id = %s",
            (error, count_attempt, rebuild_id),
        )


async def rebuild_pending(pool: AsyncConnectionPool, agent_id: int) -> bool:
    """Whether a rebuild of the agent is waiting: its leaves' grouping is then left to it."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT EXISTS (SELECT 1 FROM understanding_rebuilds"
            " WHERE agent_id = %s AND status = 'pending')",
            (agent_id,),
        )
        row = await cur.fetchone()
    assert row is not None, "an EXISTS query always returns one row"  # noqa: S101
    return bool(row[0])


async def _checks_idle(pool: AsyncConnectionPool, agent_id: int) -> bool:
    """No grouping check of the agent holds a live lease."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM understanding_group_state WHERE agent_id = %s"
            " AND claimed_at IS NOT NULL"
            " AND claimed_at > now() - make_interval(secs => %s)",
            (agent_id, CLAIM_LEASE_SECONDS),
        )
        row = await cur.fetchone()
    assert row is not None, "an aggregate query always returns one row"  # noqa: S101
    return int(row[0]) == 0


async def _lift_upper(pool: AsyncConnectionPool, agent_id: int) -> list[int]:
    """Drop every node above level 1 and the grouping cursor, in one transaction.

    The level-1 nodes lose their parent; the result is the message-index end of every one that can
    take part in grouping (timed, of this pipeline), in message order.
    """
    async with async_write_transaction(pool) as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE understanding_nodes SET parent_id = NULL"
            " WHERE agent_id = %s AND depth = 1 AND parent_id IS NOT NULL",
            (agent_id,),
        )
        await cur.execute(
            "DELETE FROM understanding_nodes WHERE agent_id = %s AND depth > 1", (agent_id,)
        )
        await cur.execute("DELETE FROM understanding_group_state WHERE agent_id = %s", (agent_id,))
        await cur.execute(
            "SELECT span_end FROM understanding_nodes WHERE agent_id = %s AND depth = 1"
            " AND engine_version LIKE 'chunk-%%' AND start_ts IS NOT NULL AND end_ts IS NOT NULL"
            " ORDER BY span_start",
            (agent_id,),
        )
        return [int(r[0]) for r in await cur.fetchall()]


async def run_rebuild(
    pool: AsyncConnectionPool,
    db: Database,
    models: GroupingModels,
    agent_id: int,
    *,
    inputs: UnderstandingReadInputs,
    executor: Executor | None = None,
) -> int:
    """Rebuild the agent's upper levels from its level-1 nodes; the number of leaves replayed.

    Raises:
        TimeoutError: an earlier grouping check still holds its lease after `IDLE_WAIT_SECONDS`.
    """
    waited = 0.0
    while not await _checks_idle(pool, agent_id):
        if waited >= IDLE_WAIT_SECONDS:
            raise TimeoutError("a grouping check of the agent is still running")
        await asyncio.sleep(_IDLE_POLL_SECONDS)
        waited += _IDLE_POLL_SECONDS
    leaf_ends = await _lift_upper(pool, agent_id)
    for end in leaf_ends:
        await run_group_checks(
            pool, db, models, agent_id, inputs=inputs, executor=executor, upto=end
        )
    return len(leaf_ends)
