"""Settle an ended hosted turn and consume its claimed inputs without redelivery. Crash reaping marks termination and publishes diagnostics; it never queues a recovery wake."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from agent.ownership.corpse_reap import reap_recrashed_corpse
from agent.ownership.hosted import TurnSettlement, settle_and_stamp_turn
from agent.ownership.inbound import RuntimeOwnershipLostError
from agent.startup import reconcile_claimed_inbounds_at_startup
from base.agents.history.inbound_sideload import ReconcileReadInputs
from base.config import settings
from base.deploy.stop_timing import CANCEL_UNWIND_TIMEOUT_S
from base.events.live.bus import EventBus
from base.log import logger
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import (
    HostedTurnResources,
    hosted_resources_settled,
)
from services.agent_runner.agent_host.db_recovery import database_phase
from services.agent_runner.agent_host.runtime import TurnOutcome
from services.agent_runner.agent_host.stall_guard import HostRestartRequiredError

__all__ = [
    "close_hosted_turn",
    "prompt_reap_after_recrash",
    "reconcile_inbounds_after_abort",
    "reconcile_inbounds_after_turn",
]


async def close_hosted_turn(
    pool: AsyncConnectionPool,
    control_pool: AsyncConnectionPool,
    bus: EventBus,
    checkpointer: AsyncPostgresSaver,
    incarnation: RuntimeIncarnation,
    outcome: TurnOutcome,
    *,
    resources: HostedTurnResources | None,
    prompt_reap_enabled: Callable[[], bool],
    reconcile_inputs: ReconcileReadInputs,
) -> None:
    """Settle the finished turn, dispose its claimed rows (the abort pass, or
    a finished turn's own pass), then prompt-reap a corpse that re-crashed
    under its own mark.

    Order matters: both reconcile passes need the turn's own live incarnation
    (its lease fence), and the reap terminates that incarnation — so the reap
    runs last, after them."""
    settlement = await settle_and_stamp_turn(
        control_pool,
        incarnation,
        bus=bus,
        exited=outcome.exited,
        crashed=outcome.crashed,
        resources=resources,
    )
    if outcome.failure_report is not None:
        if settlement.stamp is not None and settlement.stamp.applied and settlement.settled:
            report, outcome.failure_report = outcome.failure_report, None
            await report()
        else:
            logger.error(
                "turn Error withheld: its fatal cutoff or owner settlement is unproven",
                agent_id=incarnation.agent_id,
            )
    if outcome.aborted:
        await reconcile_inbounds_after_abort(
            pool, checkpointer, incarnation, resources=resources, inputs=reconcile_inputs
        )
    elif not outcome.crashed and not outcome.truncated and not outcome.native_held:
        # A truncated turn (an applied force terminate of its incarnation)
        # skips the pass too: the successor boundary that observes the force
        # owns its claimed rows.
        await reconcile_inbounds_after_turn(
            pool, checkpointer, incarnation, resources=resources, inputs=reconcile_inputs
        )
    if outcome.crashed:
        await prompt_reap_after_recrash(
            control_pool, bus, incarnation, settlement, enabled=prompt_reap_enabled
        )


async def prompt_reap_after_recrash(
    pool: AsyncConnectionPool,
    bus: EventBus,
    incarnation: RuntimeIncarnation,
    settlement: TurnSettlement,
    *,
    enabled: Callable[[], bool],
) -> None:
    """Terminate an exactly matched corpse after a repeated crash.

    The existing crash stamp, configured switch and settled incarnation fence
    must all permit reaping. Publish termination diagnostics without enqueueing
    new work or restarting the agent.
    """
    stamp = settlement.stamp
    if stamp is None or not stamp.recrash:
        return
    agent_id = incarnation.agent_id
    if not enabled():
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
    reaped = await reap_recrashed_corpse(pool, incarnation, bus=bus)
    if not reaped:
        logger.info(
            "recrash prompt reap skipped: the row moved on since the crash",
            event="host_recrash_reap_skipped",
            agent_id=agent_id,
            reason="row_moved_on",
        )
        return


async def reconcile_inbounds_after_abort(
    pool: AsyncConnectionPool,
    checkpointer: AsyncPostgresSaver,
    incarnation: RuntimeIncarnation,
    *,
    resources: HostedTurnResources | None,
    inputs: ReconcileReadInputs,
) -> None:
    """Consume an aborted turn's claims under its original owner lock.

    Rows confirmed in history are committed; other claimed chats are consumed as
    interrupted with unknown results. Neither group is returned to pending. A
    missing lease, unresolved resources or read/write failure leaves the claims
    for a later admission to consume, without executing the old work.
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
    if not hosted_resources_settled(resources):
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
            await reconcile_claimed_inbounds_at_startup(
                pool, checkpointer, agent_id, incarnation=incarnation, inputs=inputs
            )
    except RuntimeOwnershipLostError:
        logger.warning(
            "host abort reconcile skipped: runtime ownership already replaced — "
            "the replacement disposes the claimed rows",
            event="host_abort_reconcile_skipped",
            agent_id=agent_id,
            reason="ownership_lost",
        )
    except Exception:
        logger.opt(exception=True).warning(
            "host abort reconcile failed — the next cold admission retries it",
            event="host_abort_reconcile_failed",
            agent_id=agent_id,
        )


async def reconcile_inbounds_after_turn(
    pool: AsyncConnectionPool,
    checkpointer: AsyncPostgresSaver,
    incarnation: RuntimeIncarnation,
    *,
    resources: HostedTurnResources | None,
    inputs: ReconcileReadInputs,
) -> None:
    """Dispose the finished turn's claimed inbounds at its settlement point.

    A confirmed claim is consumed normally. An unconfirmed interrupted claim
    is also consumed, with an unknown result, so it cannot automatically replay
    execution. Pending new inputs stay available. The original incarnation and
    settled resource fence serialize this pass against another claim.
    """
    agent_id = incarnation.agent_id
    if not settings.daemon.host_turn_reconcile_enabled:
        logger.info(
            "turn inbound reconcile disabled — claimed rows wait for the next cold admission",
            event="host_turn_reconcile_skipped",
            agent_id=agent_id,
            reason="disabled",
        )
        return
    if not hosted_resources_settled(resources):
        # Expected fail-closed path (the event declares tier="noise"): the
        # claimed rows wait for the next cold admission, so this is INFO, not
        # a WARNING-level anomaly (2026-10-03 triage).
        logger.info(
            "turn inbound reconcile skipped: turn resources unresolved — "
            "the next cold admission disposes the claimed rows",
            event="host_turn_reconcile_skipped",
            agent_id=agent_id,
            reason="resources_unsettled",
        )
        return
    try:
        async with database_phase():
            await reconcile_claimed_inbounds_at_startup(
                pool, checkpointer, agent_id, incarnation=incarnation, inputs=inputs
            )
    except RuntimeOwnershipLostError:
        # Expected fail-closed path (the event declares tier="noise"): the
        # replacement runtime disposes the rows, so this is INFO, not a
        # WARNING-level anomaly (2026-10-03 triage).
        logger.info(
            "turn inbound reconcile skipped: runtime ownership already replaced — "
            "the replacement disposes the claimed rows",
            event="host_turn_reconcile_skipped",
            agent_id=agent_id,
            reason="ownership_lost",
        )
    except Exception:
        logger.opt(exception=True).warning(
            "turn inbound reconcile failed — the next cold admission retries it",
            event="host_turn_reconcile_failed",
            agent_id=agent_id,
        )


async def wait_shielded_task[T](task: asyncio.Task[T]) -> bool:
    """Observe one cleanup task within a fixed bound, including repeated cancellation."""
    cancelled = False
    deadline = asyncio.get_running_loop().time() + CANCEL_UNWIND_TIMEOUT_S
    while not task.done():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise HostRestartRequiredError("hosted cleanup exceeded its finite deadline")
        try:
            await asyncio.wait({task}, timeout=remaining)
        except asyncio.CancelledError:
            cancelled = True
    return cancelled


async def wait_retained_resources(resources: HostedTurnResources) -> bool:
    """Wait finitely for known resources; uncertainty never authorizes a new turn."""
    cancelled = False
    deadline = asyncio.get_running_loop().time() + CANCEL_UNWIND_TIMEOUT_S
    while resources.unresolved:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise HostRestartRequiredError("hosted resources did not close before the deadline")
        resources.changed.clear()
        try:
            async with asyncio.timeout(remaining):
                await resources.changed.wait()
        except asyncio.CancelledError:
            cancelled = True
        except TimeoutError as exc:
            raise HostRestartRequiredError(
                "hosted resources did not close before the deadline"
            ) from exc
    return cancelled
