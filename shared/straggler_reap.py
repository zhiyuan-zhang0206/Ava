"""Settle update straggler-reap marks at a successor boundary.

The drain (`ops.agent_pause`) CAS-marks an un-landed cohort agent
``status='restarting'`` and lets the durable reap signal interrupt its
in-flight turn (``agent.db.has_pending_interrupt``); nothing at drain time
releases the row, because a hosted turn is not a process and may still be
unwinding. This module is the one settle face, called from the two boundaries
where the successor world is authoritative for this machine:

- the agent-host boot (a stop/start wave moved the unit onto new code), and
- the local unpause (the compensating resume of an aborted wave, or any later
  bring-up on a unit that stayed up).

Settling restores the row to the shape a normal landing leaves -- ``idling``,
ownership and lease released, no lifecycle pointer -- and closes the
never-applied maintenance restart command with an honest ``lifecycle_result``
outcome (``reaped``): no ``applied_at``/``observed_at`` is ever fabricated for
a command that never reached its target. The caller then wakes each settled
agent once, so its first admission runs the existing inbound reconcile
(uncommitted claimed ordinary rows return to ``pending`` and are re-delivered)
and the dangling-tool repair -- the reaped agent's work re-runs on the new
code. A run that finds nothing settled is the normal idempotent no-op.
"""

from __future__ import annotations

import psycopg
from psycopg_pool import AsyncConnectionPool

from shared.db_transaction import async_write_transaction
from shared.log import logger

# The honest terminal outcome for a command the reap ended before application.
# Never 'applied' and never a flush: the drain released the member as `reaped`.
REAP_LIFECYCLE_OUTCOME = "reaped"
REAP_LIFECYCLE_REASON = "update_straggler_reap"

# Rows the reap stranded: marked 'restarting' with an unresolved maintenance
# restart still linked. Only the reap writes 'restarting' on a hosted row, so
# this shape names exactly a reap mark; legacy cold normalizations match
# neither kind nor payload.
_MARKED_ROWS = (
    "SELECT m.id FROM agents_meta m "
    "WHERE m.machine = %s AND m.status = 'restarting' AND m.runtime_kind = 'hosted' "
    "AND EXISTS (SELECT 1 FROM inbound_messages i "
    "  WHERE i.agent_id = m.id AND i.kind = 'restart' AND i.applied_at IS NULL "
    "  AND i.observed_at IS NULL AND i.status IN ('pending','claimed') "
    "  AND i.payload ? 'maintenance') "
    "ORDER BY m.id FOR UPDATE"
)

_CLOSE_COMMANDS = (
    "UPDATE inbound_messages SET status = 'done', "
    "payload = COALESCE(payload, '{}'::jsonb) || jsonb_build_object("
    "'lifecycle_result', jsonb_build_object('outcome', %s::text, 'reason', %s::text)) "
    "WHERE agent_id = %s AND kind = 'restart' AND applied_at IS NULL "
    "AND observed_at IS NULL AND status IN ('pending','claimed') "
    "AND payload ? 'maintenance' RETURNING id"
)

_SETTLE_ROW = (
    "UPDATE agents_meta SET status = 'idling', lifecycle_command_id = NULL, "
    "runtime_generation = NULL, runtime_owner = NULL, runtime_kind = NULL, "
    "lease_expires_at = NULL, runtime_protocol_version = 0 "
    "WHERE id = %s AND status = 'restarting' RETURNING id"
)


def _settle_one(conn: psycopg.Connection, agent_id: int) -> bool:
    """Close the agent's unresolved maintenance restarts, then release its row.

    The caller holds the transaction (and the row lock taken by
    `_MARKED_ROWS`). Closing first means a crash between the two writes cannot
    leave a runnable row pointing at a command that would replay a reap.
    """
    conn.execute(_CLOSE_COMMANDS, (REAP_LIFECYCLE_OUTCOME, REAP_LIFECYCLE_REASON, agent_id))
    row = conn.execute(_SETTLE_ROW, (agent_id,)).fetchone()
    return row is not None


def settle_stranded_reaps(conn: psycopg.Connection, machine: str) -> list[int]:
    """Settle this machine's stranded reap marks on the caller's connection.

    The sync transport for `ops.cluster_pause.unpause_local_cluster` (the
    resume/start boundary). The caller owns the transaction; rows are row-locked
    here, so concurrent settles serialize instead of racing the same row.
    """
    rows = conn.execute(_MARKED_ROWS, (machine,)).fetchall()
    # The caller announces after its transaction commits (announce_settled) —
    # a rolled-back settle must not report itself.
    return [row[0] for row in rows if _settle_one(conn, row[0])]


async def settle_stranded_reaps_async(pool: AsyncConnectionPool, machine: str) -> list[int]:
    """Settle this machine's stranded reap marks on the agent-host boot path."""
    async with async_write_transaction(pool) as conn:
        rows = await (await conn.execute(_MARKED_ROWS, (machine,))).fetchall()
        settled: list[int] = []
        for (agent_id,) in rows:
            await conn.execute(
                _CLOSE_COMMANDS,
                (REAP_LIFECYCLE_OUTCOME, REAP_LIFECYCLE_REASON, agent_id),
            )
            row = await (await conn.execute(_SETTLE_ROW, (agent_id,))).fetchone()
            if row is not None:
                settled.append(agent_id)
    announce_settled(settled, site="boot")
    return settled


def announce_settled(settled: list[int], *, site: str) -> None:
    """One telemetry row + log line per non-empty settle; never raises.

    Call only after the settle transaction committed (the async transport
    announces internally; the sync caller announces here)."""
    if not settled:
        return
    from shared import telemetry

    telemetry.emit(
        "telemetry",
        "update_straggler_reap_settled",
        attributes={"agents": settled, "site": site},
    )
    logger.info(
        "straggler-reap marks settled at {site}: {n} agent(s) restored to runnable",
        event="update_straggler_reap_settled",
        site=site,
        n=len(settled),
    )


def publish_settled_wakes(agent_ids: list[int]) -> None:
    """Best-effort wake per settled agent so its first admission reconciles."""
    from shared.db import publish_inbound_wake

    for agent_id in agent_ids:
        publish_inbound_wake(agent_id, "maintenance")
