"""SQL for the hosted runner's pending wake scan and lane classification."""

from __future__ import annotations

from uuid import UUID

import psycopg
from psycopg_pool import AsyncConnectionPool

from services.agent_runner.agent_host.dispatcher import PendingInboundWake
from services.agent_runner.agent_host.lifecycle import maintenance as maintenance_receipts


async def scan_rows(
    pool: AsyncConnectionPool[psycopg.AsyncConnection],
    owner: UUID,
    machine: str,
    stale_after_s: float,
) -> list[tuple[int, bool]]:
    async with pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT m.id, (m.last_active_at IS NULL "
                "OR m.last_active_at < now() - make_interval(secs => %s)) "
                "AND EXISTS (SELECT 1 FROM inbound_messages stale "
                "WHERE stale.agent_id=m.id AND stale.status='pending' "
                "AND stale.created_at < now() - make_interval(secs => %s)) "
                "FROM agents_meta m WHERE m.machine=%s AND ("
                "m.status IN ('idling','running') OR (m.status='terminated' "
                "AND m.runtime_owner=%s AND m.lifecycle_command_id IS NOT NULL)) "
                "AND (EXISTS (SELECT 1 FROM inbound_messages pending "
                "WHERE pending.agent_id=m.id AND pending.status='pending') "
                "OR m.lifecycle_command_id IS NOT NULL "
                "OR EXISTS (SELECT 1 FROM native_compact_commands compact "
                "WHERE compact.agent_id=m.id AND compact.released_at IS NULL "
                "AND compact.attempt_id IS NULL AND compact.outcome='accepted') "
                "OR EXISTS (SELECT 1 FROM agent_impersonations lease "
                "WHERE lease.agent_id=m.id AND lease.status IN ('requested','accepted','active')))",
                (stale_after_s, stale_after_s, machine, owner),
            )
        ).fetchall()
    return [(row[0], row[1]) for row in rows]


async def scan_candidates(
    pool: AsyncConnectionPool[psycopg.AsyncConnection],
    owner: UUID,
    machine: str,
    stale_after_s: float,
    failure_fences: maintenance_receipts.FailureFences,
) -> list[PendingInboundWake]:
    """Scan new inputs and explicit controls; never revive an expired turn.

    Database age identifies backlog; the dispatcher additionally requires its
    monotonic turn progress to be stale before cancellation. Held maintenance
    owns its restart cohort; otherwise retained commands share the work scan.
    """
    held = maintenance_receipts.pending_wakes(failure_fences)
    if held is not None:
        return held
    rows = await scan_rows(pool, owner, machine, stale_after_s)
    return [PendingInboundWake(agent_id=row[0], stale=row[1]) for row in rows]
