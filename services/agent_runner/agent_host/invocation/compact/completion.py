"""Close only the original terminal compact execution before releasing ordinary input."""

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from base.agents.compaction.completion import release_completed
from base.agents.compaction.execution import CompactCommand
from base.agents.compaction.models import CompactHeldError
from base.native_process.runtime_incarnation import RuntimeIncarnation
from services.agent_runner.agent_host.invocation.compact.apply import CompactGraph
from services.agent_runner.agent_host.invocation.native_work import (
    recover_native_cancel,
    settle_native_invocation,
)


async def close_terminal(
    pool: AsyncConnectionPool,
    saver: AsyncPostgresSaver,
    graph: CompactGraph,
    incarnation: RuntimeIncarnation,
    command: CompactCommand,
) -> bool:
    if command.execution is None:
        raise CompactHeldError("compact terminal continuation lacks original execution")
    if (command.execution.generation, command.execution.owner) != (
        incarnation.generation,
        incarnation.owner,
    ):
        await recover_native_cancel(pool, saver, graph, incarnation)
    await settle_native_invocation(
        pool,
        saver,
        graph,
        incarnation,
        command.execution,
        {"configurable": {"thread_id": str(incarnation.agent_id)}},
    )
    return await release_completed(pool, command, incarnation)
