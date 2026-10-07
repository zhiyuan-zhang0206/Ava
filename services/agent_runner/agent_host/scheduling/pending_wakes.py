"""SQL for the hosted runner's pending wake scan and lane classification."""

from __future__ import annotations

from uuid import UUID

import psycopg
from psycopg_pool import AsyncConnectionPool

_WORK_EXISTS_SQL = (
    "EXISTS (SELECT 1 FROM inbound_messages work "
    "WHERE work.agent_id=m.id AND work.status='pending' "
    "AND work.kind NOT IN ('restart','terminate')) "
    "OR EXISTS (SELECT 1 FROM agent_impersonations work_lease "
    "WHERE work_lease.agent_id=m.id "
    "AND (work_lease.status IN ('requested','accepted','active') "
    "OR work_lease.delta_version>work_lease.applied_version "
    "OR (work_lease.automatic AND work_lease.handoff_applied_at IS NULL)))"
)


async def scan_rows(
    pool: AsyncConnectionPool[psycopg.AsyncConnection],
    owner: UUID,
    machine: str,
    stale_after_s: float,
) -> list[tuple[int, bool, bool]]:
    async with pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT m.id, "  # noqa: S608 -- static predicate
                "  (m.last_active_at IS NULL "
                "   OR m.last_active_at < now() - make_interval(secs => %s)) "
                "  AND EXISTS ("
                "    SELECT 1 FROM inbound_messages stale "
                "    WHERE stale.agent_id = m.id AND stale.status = 'pending' "
                "      AND stale.created_at < now() - make_interval(secs => %s)"
                "  ), "
                "  NOT (" + _WORK_EXISTS_SQL + ") "
                "FROM agents_meta m "
                "WHERE ((("
                "    m.status = 'idling' "
                "    OR (m.status='running' AND m.runtime_owner IS DISTINCT FROM %s "
                "        AND EXISTS (SELECT 1 FROM agent_impersonations takeover "
                "          WHERE takeover.agent_id=m.id AND takeover.status IN "
                "          ('requested','accepted','active'))) "
                "    OR (m.status='terminated' AND m.runtime_kind='hosted' "
                "        AND m.runtime_owner=%s AND EXISTS ("
                "          SELECT 1 FROM inbound_messages force "
                "          WHERE force.id=m.lifecycle_command_id AND force.agent_id=m.id "
                "          AND force.target_generation=m.runtime_generation "
                "          AND force.target_owner=m.runtime_owner AND force.kind='terminate' "
                "          AND force.status='claimed' AND force.applied_at IS NOT NULL "
                "          AND force.observed_at IS NULL)) "
                "    OR (m.status = 'running' "
                "        AND (m.last_active_at IS NULL "
                "             OR m.last_active_at < now() - make_interval(secs => %s)) "
                "        AND EXISTS ("
                "          SELECT 1 FROM inbound_messages stale2 "
                "          WHERE stale2.agent_id = m.id AND stale2.status = 'pending' "
                "            AND stale2.created_at < now() - make_interval(secs => %s)"
                "        )"
                "    )"
                "  ) "
                "  AND (m.lifecycle_command_id IS NOT NULL OR EXISTS ("
                "    SELECT 1 FROM inbound_messages pending "
                "    WHERE pending.agent_id = m.id AND pending.status = 'pending'"
                "  ) OR EXISTS (SELECT 1 FROM agent_impersonations lease "
                "    WHERE lease.agent_id=m.id AND (lease.status IN "
                "    ('requested','accepted','active') OR lease.delta_version>lease.applied_version "
                "    OR (lease.automatic AND lease.handoff_applied_at IS NULL))))) "
                "  OR (m.status IN ('running','idling') AND m.runtime_kind='hosted' "
                "      AND m.runtime_owner IS NOT NULL AND m.last_turn_fatal_at IS NULL "
                "      AND m.runtime_owner IS DISTINCT FROM %s "
                "      AND (m.lease_expires_at IS NULL OR m.lease_expires_at<=now()))) "
                "  AND m.machine = %s ",
                (
                    stale_after_s,
                    stale_after_s,
                    owner,
                    owner,
                    stale_after_s,
                    stale_after_s,
                    owner,
                    machine,
                ),
            )
        ).fetchall()
    return [(row[0], row[1], row[2]) for row in rows]
