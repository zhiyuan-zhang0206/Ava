"""The owned invocation context: event lifecycle, compact continuation, then ordinary work."""

import asyncio
from collections.abc import Awaitable, Callable

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from base.agents.context import AvaContext
from base.agents.history.delta_read_compat import RecoveryReconstructionScope
from base.agents.history.inbound_sideload import ReconcileReadInputs
from base.agents.observation.db_wait import DatabaseWaits
from services.agent_runner.agent_host.db_recovery import recover_database
from services.agent_runner.agent_host.invocation.compact.apply import CompactGraph
from services.agent_runner.agent_host.invocation.compact.execute import run_compact
from services.agent_runner.agent_host.invocation.compact.lifecycle import settle_original_restart
from services.agent_runner.agent_host.runtime import TurnOutcome


async def drive_context(
    pool: AsyncConnectionPool,
    saver: AsyncPostgresSaver,
    graph: CompactGraph,
    agent_id: int,
    ctx: AvaContext,
    database_waits: DatabaseWaits,
    peek_lock: asyncio.Lock,
    invoke: Callable[[int, AvaContext], Awaitable[TurnOutcome]],
    drop_agent: Callable[[int], None],
    *,
    reconcile_inputs: ReconcileReadInputs,
    reconstruction: RecoveryReconstructionScope | None = None,
) -> TurnOutcome:
    """The publisher and explicit context belong to this invocation, not a cached runtime."""
    publisher = ctx.event_publisher
    if publisher is None:
        raise RuntimeError("hosted invocation requires its event publisher")
    invocation_error: BaseException | None = None
    try:
        async with asyncio.TaskGroup() as tasks:
            await publisher.start(tasks)
            try:
                outcome = await _drive_work(
                    pool,
                    saver,
                    graph,
                    agent_id,
                    ctx,
                    database_waits,
                    peek_lock,
                    invoke,
                    drop_agent,
                    reconstruction=reconstruction,
                    reconcile_inputs=reconcile_inputs,
                )
            except BaseException as primary:
                invocation_error = primary
                try:
                    await publisher.aclose()
                except asyncio.CancelledError:
                    # TaskGroup may cancel this parent while a failed invocation is
                    # draining. Keep the original failure alongside its worker error.
                    raise primary from None
                except BaseException as cleanup:
                    raise BaseExceptionGroup(
                        "invocation and publisher cleanup failed", [primary, cleanup]
                    ) from None
                raise
            else:
                await publisher.aclose()
                return outcome
    except BaseExceptionGroup as failures:
        # TaskGroup wraps a body failure even when its worker exits cleanly.
        # Keep host lifecycle/stall classification on the original exception;
        # a worker failure or multiple failures still leave as the full group.
        if (
            invocation_error is not None
            and len(failures.exceptions) == 1
            and failures.exceptions[0] is invocation_error
        ):
            raise invocation_error from None
        raise


async def _drive_work(
    pool: AsyncConnectionPool,
    saver: AsyncPostgresSaver,
    graph: CompactGraph,
    agent_id: int,
    ctx: AvaContext,
    database_waits: DatabaseWaits,
    peek_lock: asyncio.Lock,
    invoke: Callable[[int, AvaContext], Awaitable[TurnOutcome]],
    drop_agent: Callable[[int], None],
    *,
    reconcile_inputs: ReconcileReadInputs,
    reconstruction: RecoveryReconstructionScope | None = None,
) -> TurnOutcome:
    """Run compact continuation and ordinary work in the admitted context."""
    incarnation = ctx.require_original_incarnation(agent_id)
    if not await run_compact(
        pool,
        saver,
        graph,
        incarnation,
        ctx,
        lambda work: recover_database(
            pool=pool,
            checkpointer=saver,
            graph=graph,
            incarnation=incarnation,
            database_waits=database_waits,
            peek_lock=peek_lock,
            work=work,
            reconstruction_parent=reconstruction,
            reconcile_inputs=reconcile_inputs,
        ),
    ):
        return TurnOutcome(exited=False, crashed=False, native_held=True)
    restarted = await settle_original_restart(
        pool,
        ctx.require_bus(),
        agent_id,
        incarnation.owner,
        drop_agent,
        lambda token: recover_database(
            pool=pool,
            checkpointer=saver,
            graph=graph,
            incarnation=token,
            database_waits=database_waits,
            peek_lock=peek_lock,
            work=ctx.native_work,
            reconstruction_parent=reconstruction,
            reconcile_inputs=reconcile_inputs,
        ),
        incarnation=incarnation,
        resources=ctx.hosted_resources,
    )
    if restarted:
        return TurnOutcome(exited=False, crashed=False)
    return await invoke(agent_id, ctx)
