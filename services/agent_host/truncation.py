"""Separate the update drain's deliberate truncation from a real ownership loss.

`ops.agent_pause._reap_agent` (task #4016) CAS-marks an un-landed cohort member
'restarting' while its maintenance restart is still un-applied — the durable
truncation signal `agent.db.has_pending_interrupt` turns into an abort for the
member's in-flight turn. The turn's next fail-closed guard read
(`agent.impersonation.protect_native_hooks` -> `shared.impersonation.native_status`)
then refuses it: the row's status has left (running, idling). That refusal is a
*deliberate* truncation, not an ownership loss — the drain already released the
member with the honest `reaped` receipt and a successor boundary settles the
mark (`shared.straggler_reap`). Classifying it here lets the host end the turn
as truncated instead of recording an unclassified crash; every other ownership
loss still raises (fail-closed, unchanged).

The mark is read through the same shape the in-flight interrupt reads
(`agent.db.has_pending_interrupt`'s reap branch), bound to the turn's own
incarnation: a 'restarting' row under a foreign generation/owner is a
replacement, never a truncation.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from psycopg_pool import AsyncConnectionPool

from services.agent_host.runtime import TurnOutcome
from shared.impersonation import ImpersonationError
from shared.log import logger
from shared.runtime_incarnation import RuntimeIncarnation, current_incarnation

__all__ = ["reap_truncation_outcome", "reap_truncation_stop"]

# The reap mark bound to this turn's own incarnation: the row CAS-marked
# 'restarting' — `ops.agent_pause._reap_agent` is its only writer — with its
# un-applied, non-self restart still pending/claimed.
_MARK_SQL = (
    "SELECT m.status = 'restarting' AND m.runtime_kind = 'hosted' "
    "AND m.runtime_generation = %s AND m.runtime_owner = %s "
    "AND EXISTS (SELECT 1 FROM inbound_messages i WHERE i.agent_id = m.id "
    "AND i.source <> 'self' AND i.kind = 'restart' AND i.applied_at IS NULL "
    "AND i.status IN ('pending', 'claimed')) "
    "FROM agents_meta m WHERE m.id = %s"
)


async def reap_truncation_outcome(
    exc: BaseException, pool: AsyncConnectionPool, agent_id: int
) -> TurnOutcome | None:
    """The truncated outcome when `exc` is this turn's own reap mark, else None."""
    incarnation = current_incarnation(agent_id)
    if incarnation is None or not await _is_truncation(exc, pool, incarnation):
        return None
    return TurnOutcome(exited=False, crashed=False, truncated=True)


@asynccontextmanager
async def reap_truncation_stop(
    pool: AsyncConnectionPool, incarnation: RuntimeIncarnation
) -> AsyncGenerator[None]:
    """End a held wake quietly when the update straggler reap marked the row.

    The held-controls wake probes the native session before applying any admin
    intent. Under the reap mark the probe refuses because ownership moved on —
    and the successor boundary owns both the row and its un-applied restart, so
    the held wake has nothing left to do: stop without the receipt/crash path.
    Every other refusal re-raises (fail-closed, unchanged). The incarnation is
    passed explicitly: the guarded body is the one that binds it.
    """
    try:
        yield
    except Exception as exc:
        if not await _is_truncation(exc, pool, incarnation):
            raise
        logger.info(
            "held wake stopped by the update straggler reap",
            event="host_held_wake_truncated",
            agent_id=incarnation.agent_id,
        )


async def _is_truncation(
    exc: BaseException, pool: AsyncConnectionPool, incarnation: RuntimeIncarnation
) -> bool:
    """Whether `exc` is this incarnation's own reap mark (never a foreign row)."""
    if not isinstance(exc, ImpersonationError):
        return False
    return await _owns_reap_mark(pool, incarnation)


async def _owns_reap_mark(pool: AsyncConnectionPool, incarnation: RuntimeIncarnation) -> bool:
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                _MARK_SQL,
                (incarnation.generation, incarnation.owner, incarnation.agent_id),
            )
        ).fetchone()
    return row is not None and row[0] is True
