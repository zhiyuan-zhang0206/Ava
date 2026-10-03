"""The hosted-admission UPDATE: one statement that either claims the agent's runtime row or loses."""

from typing import Any
from uuid import UUID

import psycopg

from base.deploy.progress_timeout import AGENT_LEASE_TTL_S


async def claim_admission_row(
    conn: psycopg.AsyncConnection[Any],
    *,
    agent_id: int,
    machine: str,
    owner: UUID,
    generation: UUID,
    expected_from: str,
    takeover: bool,
) -> Any:
    """The admission UPDATE: the new `runtime_generation` row, or None when the claim lost."""
    return await (
        await conn.execute(
            "UPDATE agents_meta SET status = 'running', runtime_kind = 'hosted', "
            "runtime_generation = CASE WHEN runtime_owner = %s AND runtime_kind = 'hosted' "
            "AND runtime_generation IS NOT NULL "
            "THEN runtime_generation ELSE %s END, runtime_owner = %s, "
            "runtime_protocol_version = 0, "
            "last_admission_outcome = 'admitted', "
            "last_admission_at = clock_timestamp(), "
            "last_launch_failure_reason = NULL, last_launch_failure_at = NULL, "
            "lease_expires_at = now() + make_interval(secs => %s) "
            "WHERE id = %s AND machine = %s AND status = %s AND pid IS NULL "
            "AND status IN ('running','idling') "
            "AND NOT EXISTS (SELECT 1 FROM inbound_messages force "
            "WHERE force.id=agents_meta.lifecycle_command_id AND force.kind='terminate' "
            "AND force.status='claimed' AND force.applied_at IS NOT NULL "
            "AND force.observed_at IS NULL) "
            "AND (runtime_kind IS NULL OR runtime_kind = 'hosted') "
            "AND (runtime_owner IS NULL OR runtime_owner = %s "
            "OR lease_expires_at IS NULL OR lease_expires_at <= now() OR %s) "
            "RETURNING runtime_generation",
            (
                owner,
                generation,
                owner,
                AGENT_LEASE_TTL_S,
                agent_id,
                machine,
                expected_from,
                owner,
                takeover,
            ),
        )
    ).fetchone()
