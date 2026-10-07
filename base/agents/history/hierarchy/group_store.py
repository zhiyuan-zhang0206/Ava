"""Storage of the upper-level grouping — the check cursor, the open nodes, the groups, the call record.

- `understanding_group_state` — per `(agent, level)`: the open-node count at the
  last check (`last_checked_open`) and a lease (`claimed_at`), so a count is
  checked once and one runner checks a level at a time;
- open nodes — a level's rows with no parent, oldest first;
- `write_groups` — one parent row per closed group at the next level, its
  children pointed at it, and the cursor moved to what stays open, in one transaction;
- `understanding_group_calls` — the raw record of every provider call of a check.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from psycopg_pool import AsyncConnectionPool

from base import telemetry
from base.agents.history.hierarchy.group import (
    GROUP_ENGINE_VERSION,
    GROUP_PROMPT_VERSION,
    Group,
    GroupCall,
    OpenNode,
)
from base.agents.history.hierarchy.store import SCHEMA_VERSION
from base.db.transaction import async_write_transaction
from base.log import logger

# A claim older than this is a crashed runner's and is taken over.
CLAIM_LEASE_SECONDS = 3600.0


async def load_last_checked(pool: AsyncConnectionPool, agent_id: int, level: int) -> int:
    """The open count at the level's last check (0 before the first)."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT last_checked_open FROM understanding_group_state"
            " WHERE agent_id = %s AND level = %s",
            (agent_id, level),
        )
        row = await cur.fetchone()
    return 0 if row is None else int(row[0])


async def load_open_nodes(pool: AsyncConnectionPool, agent_id: int, level: int) -> list[OpenNode]:
    """The level's timed nodes that have no parent yet, oldest first."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT id, span_start, span_end, start_ts, end_ts, text FROM understanding_nodes"
            " WHERE agent_id = %s AND depth = %s AND parent_id IS NULL"
            " AND start_ts IS NOT NULL AND end_ts IS NOT NULL ORDER BY span_start",
            (agent_id, level),
        )
        rows = await cur.fetchall()
    return [OpenNode(int(r[0]), int(r[1]), int(r[2]), r[3], r[4], str(r[5])) for r in rows]


async def claim_check(pool: AsyncConnectionPool, agent_id: int, level: int) -> bool:
    """Take the level's lease; False when another runner holds a live one."""
    async with async_write_transaction(pool) as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO understanding_group_state (agent_id, level, claimed_at)"
            " VALUES (%(agent)s, %(level)s, now())"
            " ON CONFLICT (agent_id, level) DO UPDATE SET claimed_at = now()"
            " WHERE understanding_group_state.claimed_at IS NULL"
            " OR understanding_group_state.claimed_at < now() - make_interval(secs => %(lease)s)"
            " RETURNING agent_id",
            {"agent": agent_id, "level": level, "lease": CLAIM_LEASE_SECONDS},
        )
        return await cur.fetchone() is not None


_RELEASE_SQL = (
    "UPDATE understanding_group_state SET claimed_at = NULL, updated_at = now(),"
    " last_checked_open = COALESCE(%s, last_checked_open) WHERE agent_id = %s AND level = %s"
)


async def release_check(
    pool: AsyncConnectionPool, agent_id: int, level: int, *, last_checked_open: int | None
) -> None:
    """Free the lease; with a count, record it as the open count checked (no group closed)."""
    async with async_write_transaction(pool) as conn, conn.cursor() as cur:
        await cur.execute(_RELEASE_SQL, (last_checked_open, agent_id, level))


_UPSERT_GROUP_SQL = """
INSERT INTO understanding_nodes (
    agent_id, depth, span_start, span_end, start_ts, end_ts, segment_key, text,
    text_hash, input_hash, children_count, model, engine_version, prompt_version,
    schema_version, check_key
) VALUES (%s, %s, %s, %s, %s, %s, 'group', %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (agent_id, depth, span_start, span_end) DO UPDATE SET
    start_ts = EXCLUDED.start_ts,
    end_ts = EXCLUDED.end_ts,
    text = EXCLUDED.text,
    text_hash = EXCLUDED.text_hash,
    input_hash = EXCLUDED.input_hash,
    children_count = EXCLUDED.children_count,
    model = EXCLUDED.model,
    engine_version = EXCLUDED.engine_version,
    prompt_version = EXCLUDED.prompt_version,
    check_key = EXCLUDED.check_key,
    updated_at = now()
RETURNING id
"""


async def write_groups(
    pool: AsyncConnectionPool,
    agent_id: int,
    level: int,
    nodes: Sequence[OpenNode],
    groups: Sequence[Group],
    *,
    model: str,
    check_key: str,
) -> int:
    """Store the closed `groups` over the level's open `nodes`; the new open count.

    One parent row per group at `level + 1` (it records `check_key`, the check that wrote it) (span = first child's start to last
    child's end, times = the children's extremes), the children's `parent_id` set,
    and the level's cursor moved to the open count that remains (read in the transaction) — all in one
    transaction, the lease released with it.
    """
    index = {n.id: i for i, n in enumerate(nodes)}
    async with async_write_transaction(pool) as conn, conn.cursor() as cur:
        for group in groups:
            children = nodes[index[group.first] : index[group.last] + 1]
            span = (children[0].span_start, children[-1].span_end)
            input_hash = hashlib.sha256(
                f"{GROUP_ENGINE_VERSION}|{GROUP_PROMPT_VERSION}|{agent_id}|{level}|"
                f"{[c.id for c in children]}".encode()
            ).hexdigest()
            await cur.execute(
                _UPSERT_GROUP_SQL,
                (
                    agent_id,
                    level + 1,
                    span[0],
                    span[1],
                    min(c.start for c in children),
                    max(c.end for c in children),
                    group.summary,
                    hashlib.sha256(group.summary.encode()).hexdigest(),
                    input_hash,
                    len(children),
                    model,
                    GROUP_ENGINE_VERSION,
                    GROUP_PROMPT_VERSION,
                    SCHEMA_VERSION,
                    check_key,
                ),
            )
            row = await cur.fetchone()
            assert row is not None, "an upsert with RETURNING always returns a row"  # noqa: S101
            await cur.execute(
                "UPDATE understanding_nodes SET parent_id = %s WHERE id = ANY(%s)",
                (int(row[0]), [c.id for c in children]),
            )
        # Counted, not derived from the snapshot: a leaf of a later job of the same agent may
        # have landed while the call ran, and the baseline must include it.
        await cur.execute(
            "SELECT count(*) FROM understanding_nodes WHERE agent_id = %s AND depth = %s"
            " AND parent_id IS NULL AND start_ts IS NOT NULL AND end_ts IS NOT NULL",
            (agent_id, level),
        )
        row = await cur.fetchone()
        assert row is not None, "an aggregate query always returns one row"  # noqa: S101
        remaining = int(row[0])
        await cur.execute(_RELEASE_SQL, (remaining, agent_id, level))
    return remaining


_INSERT_CALL_SQL = """
INSERT INTO understanding_group_calls (
    agent_id, level, check_key, round, model, open_ids, request, content,
    additional_kwargs, usage_metadata, response_metadata, duration_ms, problem, error
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s)
"""


def _json(value: object) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, default=str)


async def write_group_calls(
    pool: AsyncConnectionPool,
    agent_id: int,
    level: int,
    check_key: str,
    open_ids: Sequence[int],
    calls: Sequence[GroupCall],
) -> None:
    """Persist the raw record of one check's provider calls, one row per call.

    Best-effort: a failure is a warning plus the `understanding_call_record_failed`
    event and never touches the check's outcome.
    """
    if not calls:
        return
    rows = [
        (
            agent_id,
            level,
            check_key,
            call.round,
            call.model,
            list(open_ids),
            call.request,
            _json(call.response.content if call.response else None),
            _json(call.response.additional_kwargs if call.response else None),
            _json(getattr(call.response, "usage_metadata", None)),
            _json(call.response.response_metadata if call.response else None),
            call.duration_ms,
            call.problem,
            call.error,
        )
        for call in calls
    ]
    try:
        async with async_write_transaction(pool) as conn, conn.cursor() as cur:
            await cur.executemany(_INSERT_CALL_SQL, rows)
    except Exception as exc:
        logger.opt(exception=True).warning(
            "understanding group call record failed for agent {agent} level {level}",
            agent=agent_id,
            level=level,
        )
        try:
            telemetry.emit(
                "telemetry",
                "understanding_call_record_failed",
                attributes={
                    "agent_id": agent_id,
                    "job_id": 0,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
        except Exception:
            logger.opt(exception=True).warning("understanding call-record event not emitted")
