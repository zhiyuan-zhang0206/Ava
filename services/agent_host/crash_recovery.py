"""The guarded auto-resurrect attempt that follows a corpse reap (task #4039).

`agent.corpse_reap` commits a durable recovery chat inside the terminating
transaction; this module is the attempt that consumes it right after. It is
the same shape the delivery watchdog runs for a wedged turn (task #1712):
queue the marked chat, then call the guarded `resurrect_if_terminated` — and
the same backstop: every attempt a refusal (closed agent, suppression
window, tripped recovery breaker), a race (hosted-force quiescence), or this
process's death drops is retried by the watchdog's terminated-owner
resurrection retry until the chat's stale age gate dead-letters it. The
attempt lives here rather than in the reaper because the agent layer does
not reach the ops layer, and revival orchestration is this layer's job.
"""

from __future__ import annotations

from collections.abc import Sequence

from agent.corpse_reap import ReapedCorpse
from shared.log import logger


async def recover_reaped_corpses(reaped: Sequence[ReapedCorpse]) -> None:
    """Best-effort guarded resurrection for each freshly reaped corpse.

    Never raises for an ordinary failure: a reaped corpse whose wake exists
    but whose attempt failed is still recovered by the delivery watchdog's
    retry (or by the next arriving work), so failing loudly here would only
    dress up a deferral as an incident."""
    for corpse in reaped:
        wake_id = corpse.recovery_wake_id
        if wake_id is None:
            continue
        # Deferred, and reached through ops_lifecycle's own module: ops is a
        # higher layer resolved at call time, and this is the stubbable name.
        from ops import ops_lifecycle

        try:
            status = await ops_lifecycle.resurrect_if_terminated(
                corpse.agent_id,
                trigger_inbound_id=wake_id,
                trigger_inbound_kind="chat",
            )
        except Exception:
            logger.exception(
                "crash recovery wake: resurrect attempt failed — "
                "the queued chat stays for the delivery watchdog",
                event="crash_recovery_wake_deferred",
                agent_id=corpse.agent_id,
                recovery_wake_id=wake_id,
            )
        else:
            logger.info(
                "crash recovery wake: resurrect attempt for agent {agent_id} returned {status}",
                event="crash_recovery_wake_attempted",
                agent_id=corpse.agent_id,
                recovery_wake_id=wake_id,
                status=status,
            )
