"""Separate an externally commanded force termination from a real ownership loss.

`ops.ops_lifecycle.terminate_agent_op(force=True)` installs an applied-but-
unobserved terminate command bound to the current hosted incarnation
(`shared.hosted_force.install_hosted_force`; the live pointer
`agents_meta.lifecycle_command_id`). The delivery watchdog's hosted-turn wedge
recovery is its loudest caller — it force-terminates a wedged incarnation and
queues the durable recovery chat — but a CLI/operator force and a machine
pause write the same durable shape. The turn's next fail-closed guard read
(`agent.impersonation.protect_native_hooks` -> `shared.agents.impersonation.native_status`)
then refuses it: the row has left (running, idling). That refusal is a
*deliberate* termination, not an ownership loss — this pump's own boundary
observes the command right after (`shared.hosted_force.original_host_force`).
Classifying it here lets the host end the turn quietly instead of recording an
unclassified crash; every other ownership loss still raises (fail-closed,
unchanged).

The command is bound to the turn's own incarnation through its stored
`target_generation`/`target_owner`, which survive the resurrection that NULLs
the row's generation — both states of the wedge-recovery window classify — and
the live pointer must still anchor the command: a detached command is never a
classification.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from psycopg_pool import AsyncConnectionPool

from services.agent_host.runtime import TurnOutcome
from shared.agents.impersonation import ImpersonationError
from shared.log import logger
from shared.runtime_incarnation import RuntimeIncarnation, current_incarnation

__all__ = ["force_termination_outcome", "force_termination_stop"]

# The applied-but-unobserved force command bound to this turn's own
# incarnation, still anchored by the live lifecycle pointer. `status='claimed'`
# mirrors `shared.hosted_force.original_host_force`'s exact conjunction — an
# observed command (done) or a tear (pointer detached) is never a
# classification.
_FORCE_SQL = (
    "SELECT 1 FROM agents_meta m JOIN inbound_messages i "
    "ON i.id = m.lifecycle_command_id AND i.agent_id = m.id "
    "WHERE m.id = %s AND i.kind = 'terminate' AND i.status = 'claimed' "
    "AND i.applied_at IS NOT NULL AND i.observed_at IS NULL "
    "AND i.target_generation = %s AND i.target_owner = %s"
)


async def force_termination_outcome(
    exc: BaseException, pool: AsyncConnectionPool, agent_id: int
) -> TurnOutcome | None:
    """The truncated outcome when `exc` is this turn's own applied force end, else None."""
    incarnation = current_incarnation(agent_id)
    if incarnation is None or not await _is_force_end(exc, pool, incarnation):
        return None
    logger.info(
        "hosted turn ended by its incarnation's applied force terminate",
        event="host_turn_force_terminated",
        agent_id=agent_id,
    )
    return TurnOutcome(exited=False, crashed=False, truncated=True)


@asynccontextmanager
async def force_termination_stop(
    pool: AsyncConnectionPool, incarnation: RuntimeIncarnation
) -> AsyncGenerator[None]:
    """End a held wake quietly when its own incarnation's force terminate landed.

    The held-controls wake probes the native session before applying any admin
    intent. Under the applied force the probe refuses because ownership moved
    on — the pump's boundary owns the command's observation, so the held wake
    has nothing left to do: stop without the receipt/crash path. Every other
    refusal re-raises (fail-closed, unchanged). The incarnation is passed
    explicitly: the guarded body is the one that binds it.
    """
    try:
        yield
    except Exception as exc:
        if not await _is_force_end(exc, pool, incarnation):
            raise
        logger.info(
            "held wake stopped by its incarnation's applied force terminate",
            event="host_held_wake_force_terminated",
            agent_id=incarnation.agent_id,
        )


async def _is_force_end(
    exc: BaseException, pool: AsyncConnectionPool, incarnation: RuntimeIncarnation
) -> bool:
    """Whether `exc` is this incarnation's own applied force end, never a foreign command."""
    if not isinstance(exc, ImpersonationError):
        return False
    return await _owns_force_command(pool, incarnation)


async def _owns_force_command(pool: AsyncConnectionPool, incarnation: RuntimeIncarnation) -> bool:
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                _FORCE_SQL,
                (incarnation.agent_id, incarnation.generation, incarnation.owner),
            )
        ).fetchone()
    return row is not None
