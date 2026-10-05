"""Bounded host-side delivery of ended-lease notices, independent of the relay.

The existing machine host owns one parallel delivery activity at a time. Host acceptance
is the only success this transport proves; a timeout can follow acceptance,
so retries are at least once. Native restoration never depends on this pass.
"""

import asyncio
from typing import Any
from uuid import uuid4

from psycopg.rows import dict_row

from base.agents.impersonation import host_transport
from base.db import Database
from base.log import logger

_NOTICE_RETRY_SECONDS = 30


def notice_text(snapshot: dict[str, Any]) -> str:
    """Describe exactly the ended historical lease, never a replacement's authority."""
    native = (
        "The agent was terminated; no active native runtime is implied. "
        if snapshot["reason"] == "terminated: agent was terminated"
        else "Native authority is restored; its durable handoff may still be restoring. "
    )
    return (
        f"Ava impersonation lease {snapshot['session_id']} ({snapshot['lease_id']}) "
        f"for agent {snapshot['agent_id']} ended: {snapshot['status']}. "
        f"Reason: {snapshot['reason'] or snapshot['status']}. Ended at: {snapshot['ended_at']}. "
        "That lease no longer grants external control. " + native + "This notice concerns only "
        "the named lease and does not end or cancel any newer takeover or its work. "
        f"Notice ID: impersonation-ended:{snapshot['lease_id']}."
    )


def deliver_pending_notice(db: Database, machine: str) -> bool:
    """Attempt one machine-owned ended lease, including historical or terminated agents.

    Each RPC is bounded; transient failures retain pending state and retry with
    exponential backoff capped at fifteen minutes. Unsupported destinations
    retain an explicit durable error without claiming acceptance. The minimum
    claim interval exceeds the transport deadline and fences concurrent scans.
    """
    attempt = uuid4()
    with db.write_transaction() as conn, conn.cursor(row_factory=dict_row) as cur:
        conn.execute("SET LOCAL statement_timeout='3s'")
        cur.execute(
            "SELECT id,terminal_notice_snapshot FROM agent_impersonations "
            "WHERE machine=%s AND terminal_notice_pending_at IS NOT NULL "
            "AND terminal_notice_accepted_at IS NULL AND terminal_notice_unsupported_at IS NULL "
            "AND (terminal_notice_attempt_at IS NULL OR terminal_notice_attempt_at < "
            "clock_timestamp() - LEAST(900, %s * power(2, LEAST(5, terminal_notice_attempts))) * interval '1 second') "
            "ORDER BY terminal_notice_pending_at,id LIMIT 1 FOR UPDATE SKIP LOCKED",
            (machine, _NOTICE_RETRY_SECONDS),
        )
        lease = cur.fetchone()
        if lease is None:
            return False
        cur.execute(
            "UPDATE agent_impersonations SET terminal_notice_attempt_id=%s,"
            "terminal_notice_attempt_at=clock_timestamp(),terminal_notice_attempts=terminal_notice_attempts+1 "
            "WHERE id=%s",
            (attempt, lease["id"]),
        )
    snapshot = lease["terminal_notice_snapshot"]
    unsupported = (
        snapshot["provider"] != "codex"
        or not snapshot["endpoint"]
        or not snapshot["thread_id"]
        or snapshot["endpoint"] == "unix://"
        or not snapshot["endpoint"].startswith(("unix://", "ws://", "wss://"))
    )
    if unsupported:
        error = "Independent ended-lease delivery unsupported for this recorded destination"
    else:
        error = host_transport.live_submit(
            snapshot["thread_id"], notice_text(snapshot), endpoint=snapshot["endpoint"]
        )
    with db.write_transaction() as conn:
        conn.execute("SET LOCAL statement_timeout='3s'")
        conn.execute(
            "UPDATE agent_impersonations SET terminal_notice_error=%s,"
            "terminal_notice_accepted_at=CASE WHEN %s::text IS NULL THEN clock_timestamp() "
            "ELSE terminal_notice_accepted_at END,terminal_notice_unsupported_at=CASE "
            "WHEN %s THEN clock_timestamp() ELSE terminal_notice_unsupported_at END "
            "WHERE id=%s AND terminal_notice_attempt_id=%s",
            (error, error, unsupported, lease["id"], attempt),
        )
    return True


async def run_notice_delivery(db: Database, machine: str, *, interval: float = 30.0) -> None:
    """One owned activity beside dispatch; cancellation joins its bounded worker.

    Host submission has its five-second deadline; queries have a local three-
    second ceiling. Connections retain the database owner's existing dial and
    transport ceilings. A shutdown does not detach an in-flight receipt thread.
    """
    while True:
        try:
            async with asyncio.TaskGroup() as attempts:
                worker = attempts.create_task(
                    asyncio.to_thread(deliver_pending_notice, db, machine)
                )
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    await worker
                    raise
        except Exception:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise asyncio.CancelledError from None
            logger.exception("ended impersonation notice failed; durable pending attempt retained")
        await asyncio.sleep(interval)
