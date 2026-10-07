"""Repeatable wake reconciliation for a transactionally accepted launch retry."""

import asyncio

from psycopg_pool import ConnectionPool

from base.cluster.machine import machine_name
from base.db import Database, publish_inbound_wake
from base.db.transaction import write_transaction
from base.events.live.bus import EventBus
from ops.rpc_schemas.launch_retry import LaunchReconciled, LaunchReconcileRequest


async def reconcile_launch_op(
    db: Database, bus: EventBus, body: LaunchReconcileRequest, pool: ConnectionPool
) -> LaunchReconciled:
    """Repeat a wake hint without inserting a prompt, rotating or forcing a host."""
    return await asyncio.to_thread(_reconcile, db, bus, body, pool)


def _reconcile(
    db: Database, bus: EventBus, body: LaunchReconcileRequest, pool: ConnectionPool
) -> LaunchReconciled:
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT agent_id, machine FROM agent_launch_retry_receipts WHERE launch_attempt_id=%s",
            (body.launch_attempt_id,),
        )
        receipt = cur.fetchone()
        if receipt is None or receipt[1] != machine_name():
            raise ValueError("launch retry receipt is missing or belongs to another machine")
        agent_id, target = receipt
        cur.execute(
            "SELECT machine, last_launch_attempt_id, status, last_admission_at "
            "FROM agents_meta WHERE id=%s FOR UPDATE",
            (agent_id,),
        )
        row = cur.fetchone()
        if row != (target, body.launch_attempt_id, "idling", None):
            return LaunchReconciled(wake_published=False)
        # Hold the metadata fence through publication. Concurrent admission,
        # termination, placement and later retry cannot pass between check and
        # wake. A hint already published can be consumed later: the native host
        # still owns admission and claims only existing durable inbound work.
        published = publish_inbound_wake(db, bus, agent_id, "0")
        return LaunchReconciled(wake_published=published)
