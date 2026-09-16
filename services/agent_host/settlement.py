"""Close out one hosted turn: settle its row, then dispose its claimed inbounds.

The settlement boundary runs from `AgentHost._run_turn`'s finally for every
turn — the corpse marker + idling settle — and, for a settled abort,
additionally reconciles the inbounds that turn claimed. That reconcile is the
same pass a cold admission would run, moved to the point where the abort
became durable, so the rows are not left for a cold admission or boot that may
never come (task #3615). A turn that dies again under its own corpse mark is
prompt-reaped here too — the same termination the beat reaper performs, moved
up to the retry's failure (task #3616). Split into its own module to keep
`host.py` inside the file-size ceiling.
"""

from __future__ import annotations

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from agent.corpse_reap import reap_recrashed_corpse
from agent.hosted_ownership import TurnSettlement, settle_and_stamp_turn
from agent.inbound_ownership import RuntimeOwnershipLostError
from agent.startup import _reconcile_claimed_inbounds_at_startup
from services.agent_host.db_recovery import database_phase
from services.agent_host.runtime import TurnOutcome
from shared.config import settings
from shared.log import logger
from shared.runtime_incarnation import RuntimeIncarnation
from shared.turn_identity import bind_turn_identity, hosted_resources_settled

__all__ = ["close_hosted_turn", "prompt_reap_after_recrash", "reconcile_inbounds_after_abort"]


async def close_hosted_turn(
    pool: AsyncConnectionPool,
    control_pool: AsyncConnectionPool,
    checkpointer: AsyncPostgresSaver,
    incarnation: RuntimeIncarnation,
    outcome: TurnOutcome,
) -> None:
    """Settle the finished turn, dispose a settled abort's claimed rows, then
    prompt-reap a corpse that re-crashed under its own mark.

    Order matters: the abort reconcile needs the abort's own live incarnation
    (its lease fence), and the reap terminates that incarnation — so the reap
    runs last, after both."""
    settlement = await settle_and_stamp_turn(
        control_pool, incarnation, exited=outcome.exited, crashed=outcome.crashed
    )
    if outcome.aborted:
        await reconcile_inbounds_after_abort(pool, checkpointer, incarnation)
    if outcome.crashed:
        await prompt_reap_after_recrash(control_pool, incarnation, settlement)


async def prompt_reap_after_recrash(
    pool: AsyncConnectionPool,
    incarnation: RuntimeIncarnation,
    settlement: TurnSettlement,
) -> None:
    """Terminate a corpse whose second crash settled right now (task #3616).

    The mark's first stamp bought the grace window — the one chance to
    self-heal. A crash under an existing mark is the retry failing, so the
    corpse reaper's termination (`agent.corpse_reap.reap_recrashed_corpse`,
    same events) runs at once instead of letting a zombie spend the rest of
    the window claiming and re-dying (the #3602 window). A turn that died
    FIRST under its mark is not touched: the first grace stays whole.

    Fail-closed: every gap — the gray switch still off, a settle that could
    not reach idling, a row that moved on since — skips the reap with its
    reason logged, and the grace-window reap stays the backstop.
    """
    stamp = settlement.stamp
    if stamp is None or not stamp.recrash:
        return
    agent_id = incarnation.agent_id
    if not settings.daemon.hosted_recrash_prompt_reap_enabled:
        logger.info(
            "recrash prompt reap disabled — the corpse keeps its remaining grace",
            event="host_recrash_reap_skipped",
            agent_id=agent_id,
            reason="disabled",
        )
        return
    if not settlement.settled:
        logger.warning(
            "recrash prompt reap skipped: the turn never settled to idling — "
            "the grace-window reap stays the backstop",
            event="host_recrash_reap_skipped",
            agent_id=agent_id,
            reason="settle_incomplete",
        )
        return
    if not await reap_recrashed_corpse(pool, incarnation):
        logger.info(
            "recrash prompt reap skipped: the row moved on since the crash",
            event="host_recrash_reap_skipped",
            agent_id=agent_id,
            reason="row_moved_on",
        )


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
