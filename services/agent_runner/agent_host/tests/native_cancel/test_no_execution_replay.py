"""Fresh wakes abandon interrupted work even when its execution checkpoint is lost."""

import asyncio
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool

from agent.state import AgentState, BaseAgentState
from base.agents.context import AvaContext
from base.agents.observation.relay_supervision import RelaySupervision
from base.db import Database
from base.events.live.bus import EventBus
from services.agent_runner.agent_host.invocation.native_work import (
    NativeWorkContinuation,
    invoke_prepared_graph,
)
from services.agent_runner.agent_host.tests.host_policy import configured_policy
from services.agent_runner.agent_host.tests.native_cancel.helpers import managed_work
from services.agent_runner.agent_host.tests.runtime.hosted_resources import hosted_scope


async def test_checkpoint_failure_after_subprocess_does_not_replay_on_fresh_wake(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    incarnation, _ = await managed_work(db_conn, aops_pool)
    effect = tmp_path / "effects.txt"
    fresh_input = True
    save_failure = False
    fail_once = True

    async def claim(state: BaseAgentState) -> Command[Any]:
        nonlocal fresh_input
        if fresh_input:
            fresh_input = False
            return Command(update={"halted": False}, goto="exec")
        return Command(update={"turn_idle": True}, goto=END)

    async def execute(state: BaseAgentState) -> dict[str, object]:
        nonlocal save_failure
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; "
            "p=Path(sys.argv[1]); p.write_text(p.read_text()+'effect\\n' if p.exists() else 'effect\\n')",
            str(effect),
        )
        assert await child.wait() == 0
        save_failure = True
        return {"halted": True, "turn_idle": True}

    saver = AsyncPostgresSaver(aops_pool)
    await saver.setup()
    original_put = saver.aput

    async def faulted_put(*args: Any, **kwargs: Any) -> Any:
        nonlocal fail_once
        if save_failure and fail_once:
            fail_once = False
            raise psycopg.OperationalError("lost execution checkpoint commit")
        return await original_put(*args, **kwargs)

    monkeypatch.setattr(saver, "aput", faulted_put)
    builder = cast(Any, StateGraph(AgentState, context_schema=AvaContext))
    builder.add_node("claim", claim, destinations=("exec", END))
    builder.add_node("exec", execute)
    builder.add_edge(START, "claim")
    builder.add_edge("exec", END)
    graph = builder.compile(checkpointer=saver)
    policy = configured_policy()
    config: RunnableConfig = {"configurable": {"thread_id": str(incarnation.agent_id)}}
    bus = EventBus.from_settings()
    async with hosted_scope() as resources:
        ctx = AvaContext(
            original_incarnation=incarnation,
            hosted_resources=resources,
            ops_pool=aops_pool,
            db=database,
            bus=bus,
        )

        async def run(context: AvaContext, update: dict[str, object]) -> Any:
            return await graph.ainvoke(update, config, context=context)

        async def invoke() -> Any:
            return await invoke_prepared_graph(
                aops_pool,
                graph,
                incarnation.agent_id,
                ctx,
                NativeWorkContinuation(uuid4()),
                db=database,
                bus=bus,
                relays=RelaySupervision(),
                invoke=run,
                notes=policy.handoff_notes,
            )

        with pytest.raises(psycopg.OperationalError, match="execution checkpoint"):
            await invoke()
        assert effect.read_text().splitlines() == ["effect"]
        # Discard process-local graph/saver state as a restart would. The committed
        # predecessor still contains a scheduled exec, but a new input starts claim.
        saver = AsyncPostgresSaver(aops_pool)
        graph = builder.compile(checkpointer=saver)
        ctx = replace(ctx)
        await invoke()
        assert effect.read_text().splitlines() == ["effect"]
        # A genuinely new message can execute new work once.
        fresh_input = True
        await invoke()
        assert effect.read_text().splitlines() == ["effect", "effect"]
