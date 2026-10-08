"""The owned invocation context: event lifecycle, compact continuation, then ordinary work."""

import asyncio
from collections.abc import Awaitable, Callable

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from ava.sdk_surface import process_context
from base.agents.context import AvaContext
from base.agents.observation.db_wait import DatabaseWaits
from base.native_process.runtime_incarnation import current_incarnation
from services.agent_runner.agent_host.db_recovery import recover_database
from services.agent_runner.agent_host.invocation.compact.apply import CompactGraph
from services.agent_runner.agent_host.invocation.compact.execute import run_compact
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
) -> TurnOutcome:
    """Publisher and process context belong to this invocation, not a cached runtime."""
    publisher = ctx.event_publisher
    if publisher is None:
        raise RuntimeError("hosted invocation requires its event publisher")
    await publisher.start()
    try:
        with process_context.scoped(ctx):
            incarnation = current_incarnation(agent_id)
            if incarnation is None:
                raise RuntimeError("compact host continuation lacks admitted identity")
            if not await run_compact(
                pool,
                saver,
                graph,
                incarnation,
                ctx,
                lambda: recover_database(
                    pool=pool,
                    checkpointer=saver,
                    graph=graph,
                    incarnation=incarnation,
                    database_waits=database_waits,
                    peek_lock=peek_lock,
                ),
            ):
                return TurnOutcome(exited=False, crashed=False, native_held=True)
            return await invoke(agent_id, ctx)
    finally:
        await publisher.aclose()
