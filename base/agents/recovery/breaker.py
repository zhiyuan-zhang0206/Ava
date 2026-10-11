"""One permanent provider rejection stops background wakes. Fresh chat or explicit operator resurrection may try again after a fix; successful model progress clears the durable rejection. No billing batch recovery is provided."""

from __future__ import annotations

from typing import Final, LiteralString, cast

from psycopg_pool import AsyncConnectionPool

from base.db.transaction import async_write_transaction

HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS = 1

SUPPRESS_REASON_PERMANENT_REJECT = "permanent_provider_reject"

# The tripped window is "until human" in practice: long enough that no
# automatic path resumes on a mere timer, finite so the column stays
# representable everywhere (no `infinity` datetime edge cases).
HALT_SUPPRESSION_WINDOW_S = 3650 * 24 * 3600.0

# Stored reason for an HTTP 402 billing rejection, shared with the classifier.
PERMANENT_REJECT_REASON_BILLING: Final = "billing"

# Correlated against the unaliased agents_meta row (same shape as
# `base.agents.incarnation.lifecycle_acceptance.FAILED_RESTART_FOR_CURRENT_TARGET`). This is the
# SQL half of the breaker the automatic-recovery gates embed; keep the literal
# in sync with HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS (asserted by
# tests/components/base/test_recovery_breaker.py).
RECOVERY_BREAKER_CLEAR: LiteralString = "permanent_reject_streak < 1"


async def record_permanent_reject_turn(
    pool: AsyncConnectionPool, agent_id: int, reason: str
) -> int:
    """Count one permanent-class rejected turn; return the new streak.

    `reason` is the ``_circuit_reason`` class of THIS rejection; it is
    recorded beside the streak (``last_permanent_reject_reason``) so durable
    diagnostics can identify the cause of the halt. Callers keep this
    best-effort (log-and-continue on failure): the breaker must never mask
    the original provider rejection.
    """
    async with async_write_transaction(pool) as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE agents_meta SET permanent_reject_streak = permanent_reject_streak + 1, "
            "last_permanent_reject_reason = %s "
            "WHERE id = %s RETURNING permanent_reject_streak",
            (reason, agent_id),
        )
        row = await cur.fetchone()
    if row is None:
        raise RuntimeError(f"agent {agent_id} disappeared while recording a permanent rejection")
    return cast(int, row[0])


async def halt_automatic_recovery(pool: AsyncConnectionPool, agent_id: int) -> bool:
    """Trip the breaker: suppress automatic wakes for `agent_id`.

    Idempotent against an already-tripped row: while the permanent-reject
    suppression is active it is left untouched (the streak is the durable
    gate; this write is the operator-visible reason and the input to the
    suppression gates). Any other reason's window — or an expired one — is
    replaced, because this trip is the stronger, until-human state.

    Returns True when this call wrote a fresh suppression window.
    """
    async with async_write_transaction(pool) as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE agents_meta "
            "SET wake_suppressed_until = clock_timestamp() + make_interval(secs => %s), "
            "    wake_suppress_reason = %s "
            "WHERE id = %s "
            "  AND (wake_suppress_reason IS DISTINCT FROM %s "
            "       OR wake_suppressed_until IS NULL "
            "       OR wake_suppressed_until < now())",
            (
                HALT_SUPPRESSION_WINDOW_S,
                SUPPRESS_REASON_PERMANENT_REJECT,
                agent_id,
                SUPPRESS_REASON_PERMANENT_REJECT,
            ),
        )
        return cur.rowcount == 1


async def admit_pending_wake(pool: AsyncConnectionPool, agent_id: int) -> bool:
    """Keep background inputs halted; an explicit fresh peer/user chat can retry."""
    async with async_write_transaction(pool) as conn:
        row = await (
            await conn.execute(
                "SELECT permanent_reject_streak,lifecycle_command_id FROM agents_meta "
                "WHERE id=%s FOR UPDATE",
                (agent_id,),
            )
        ).fetchone()
        if row is None:
            return False
        if row[0] < HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS or row[1] is not None:
            return True
        fresh = await (
            await conn.execute(
                "SELECT 1 FROM inbound_messages i JOIN agents_meta m ON m.id=i.agent_id "
                "WHERE i.agent_id=%s AND i.status='pending' AND i.kind='chat' "
                "AND i.source<>'system' AND i.source NOT LIKE 'system:%%' "
                "AND i.created_at>COALESCE(m.last_turn_fatal_at,m.status_changed_at) LIMIT 1",
                (agent_id,),
            )
        ).fetchone()
        if fresh is None:
            return False
        await conn.execute(
            "UPDATE agents_meta SET permanent_reject_streak=0,last_permanent_reject_reason=NULL,"
            "wake_suppressed_until=NULL,wake_suppress_reason=NULL WHERE id=%s",
            (agent_id,),
        )
        return True
