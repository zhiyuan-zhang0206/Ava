"""Close out one hosted turn: settle its row, then dispose its claimed inbounds.

The settlement boundary runs from `AgentHost._run_turn`'s finally for every
turn — the corpse marker + idling settle — and, for a settled abort,
additionally reconciles the inbounds that turn claimed. That reconcile is the
same pass a cold admission would run, moved to the point where the abort
became durable, so the rows are not left for a cold admission or boot that may
never come (task #3615). Split into its own module to keep `host.py` inside the
file-size ceiling.
"""

from __future__ import annotations

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from agent.hosted_ownership import settle_and_stamp_turn
from agent.inbound_ownership import RuntimeOwnershipLostError
from agent.startup import _reconcile_claimed_inbounds_at_startup
from services.agent_host.db_recovery import database_phase
from services.agent_host.runtime import TurnOutcome
from shared.config import settings
from shared.log import logger
from shared.runtime_incarnation import RuntimeIncarnation
from shared.turn_identity import bind_turn_identity, hosted_resources_settled

__all__ = ["close_hosted_turn", "reconcile_inbounds_after_abort"]


async def close_hosted_turn(
    pool: AsyncConnectionPool,
    control_pool: AsyncConnectionPool,
    checkpointer: AsyncPostgresSaver,
    incarnation: RuntimeIncarnation,
    outcome: TurnOutcome,
) -> None:
    """Settle the finished turn, then dispose a settled abort's claimed rows."""
    await settle_and_stamp_turn(
        control_pool, incarnation, exited=outcome.exited, crashed=outcome.crashed
    )
    if outcome.aborted:
        await reconcile_inbounds_after_abort(pool, checkpointer, incarnation)


async def reconcile_inbounds_after_abort(
    pool: AsyncConnectionPool,
    checkpointer: AsyncPostgresSaver,
    incarnation: RuntimeIncarnation,
) -> None:
    """Dispose the aborted turn's claimed inbounds at its settlement point.

    A fatal abort settles the turn (corpse marker stamped, row back to idling)
    but keeps the cached runtime, so nothing forces the cold admission and its
    startup reconcile — the rows claimed by the abort would wait for a boot
    that may never come (task #3615; the #3602 window). The same inbound
    reconcile the startup path uses runs here instead: rows whose message
    reached the flushed checkpoint go `done`, uncommitted rows carry no durable
    commitment and return to `pending` for the next claim (at-least-once
    delivery still permits a re-delivery), and rows past the stale threshold
    are dead-lettered.

    Fail-closed: any gap skips the pass and leaves the rows to the next cold
    admission. The pass needs the abort's own checkpoint settlement (written by
    `settle_turn_failure` before `aborted` was returned), a fully discharged
    turn (`hosted_resources_settled`), and a live lease for this exact
    incarnation — the inbound owner lock fences on it, so a replacement already
    in place makes the pass a no-op. The settle boundary runs outside the
    turn's bind window, so re-establish the same incarnation around the call.
    """
    agent_id = incarnation.agent_id
    if not settings.daemon.host_abort_reconcile_enabled:
        logger.info(
            "host abort reconcile disabled — claimed rows wait for the next cold admission",
            event="host_abort_reconcile_skipped",
            agent_id=agent_id,
            reason="disabled",
        )
        return
    if not hosted_resources_settled():
        logger.warning(
            "host abort reconcile skipped: turn resources unresolved — "
            "the next cold admission disposes the claimed rows",
            event="host_abort_reconcile_skipped",
            agent_id=agent_id,
            reason="resources_unsettled",
        )
        return
    try:
        async with database_phase():
            with bind_turn_identity(agent_id, incarnation=incarnation):
                await _reconcile_claimed_inbounds_at_startup(pool, checkpointer, agent_id)
    except RuntimeOwnershipLostError:
        logger.warning(
            "host abort reconcile skipped: runtime ownership already replaced — "
            "the replacement disposes the claimed rows",
            event="host_abort_reconcile_skipped",
            agent_id=agent_id,
            reason="ownership_lost",
        )
    except Exception:
        logger.warning(
            "host abort reconcile failed — the next cold admission retries it",
            event="host_abort_reconcile_failed",
            agent_id=agent_id,
            exc_info=True,
        )
