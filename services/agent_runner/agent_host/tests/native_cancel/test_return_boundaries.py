"""Native cancellation settles the original returned failure/lifecycle work."""

import asyncio
from dataclasses import replace
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import START, StateGraph
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from agent.graph.claim.node import claim_node
from agent.graph.llm_errors import FatalProviderError
from agent.ownership.lifecycle_intent import accept_lifecycle_intent
from agent.ownership.tests.test_lifecycle_intent import _command
from agent.startup import wrap_saver_writes_with_nstep_interval
from agent.state import AgentState
from agent.tests.claim.test_inbound_ownership import _insert
from base.agents.context import AvaContext
from base.agents.history.delta_read_compat import wrap_saver_reads_with_delta_reconstruction
from base.agents.messages.native_cancel import accept_native_cancel, observe_native_work
from base.db import Database
from base.db.transaction import async_write_transaction
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.native_process.turn_identity import HostedTurnResources
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.tests.host_policy import configured_policy
from services.agent_runner.agent_host.tests.native_cancel.helpers import managed_work
from services.agent_runner.agent_host.tests.native_cancel.test_continuation import _install_faults


@pytest.mark.parametrize("ending", ["provider_failure", "restart", "terminate"])
async def test_accepted_cancel_precedes_original_failure_or_lifecycle_settlement(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    ending: str,
    model_catalog: ModelCatalog,
) -> None:
    pool: ConnectionPool
    incarnation, initial = await managed_work(db_conn, aops_pool)
    _insert(db_conn, initial.agent_id)
    entered, release = asyncio.Event(), asyncio.Event()

    async def model(_state: AgentState) -> Command[Any]:
        entered.set()
        await release.wait()
        if ending == "provider_failure":
            raise FatalProviderError(
                "isolated provider rejection", error_class="permanent", status=401
            )
        return Command(
            update={
                "halted": True,
                "turn_idle": True,
                "restart_requested": ending == "restart",
                "exit_requested": ending == "terminate",
            },
            goto="__end__",
        )

    graph, _saver, host, ctx = await _blocked_host(aops_pool, model, model_catalog=model_catalog)
    faults = _install_faults(monkeypatch, initial.agent_id, "after_ack")
    running = asyncio.create_task(
        host._invoke_until_done(
            initial.agent_id,
            replace(ctx, original_incarnation=incarnation, hosted_resources=HostedTurnResources()),
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        command = None
        if ending != "provider_failure":
            command = _command(db_conn, initial.agent_id, ending)
            async with async_write_transaction(aops_pool) as conn:
                assert (
                    await accept_lifecycle_intent(conn, initial.agent_id, incarnation=incarnation)
                    is not None
                )
        with ConnectionPool[psycopg.Connection](db_conn.info.dsn) as pool:
            target = await asyncio.to_thread(observe_native_work, pool, initial.agent_id)
            assert target is not None
            accepted = await asyncio.to_thread(
                accept_native_cancel, pool, "return-boundary", initial.agent_id, target
            )
        queued = _insert(db_conn, initial.agent_id)
        release.set()
        outcome = await asyncio.wait_for(running, 10)
    finally:
        release.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)
    assert faults.injected and faults.invocations == 1
    assert not outcome.native_held and outcome.exited == (ending == "terminate")
    assert db_conn.execute(
        "SELECT outcome FROM native_cancel_commands WHERE id=%s", (accepted.command_id,)
    ).fetchone() == ("applied",)
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (queued,)
    ).fetchone() == ("pending",)
    if command is not None:
        record = db_conn.execute(
            "SELECT applied_at,observed_at FROM inbound_messages WHERE id=%s", (command,)
        ).fetchone()
        assert record is not None and record[0] is not None
        assert (record[1] is not None) == (ending == "terminate")
    else:
        assert outcome.crashed and outcome.aborted
    snapshot = await graph.aget_state({"configurable": {"thread_id": str(initial.agent_id)}})
    assert snapshot.values["halted"] is True


async def _blocked_host(
    pool: AsyncConnectionPool[Any], model: Any, model_catalog: ModelCatalog
) -> tuple[Any, Any, AgentHost, AvaContext]:
    saver = AsyncPostgresSaver(pool)
    await saver.setup()
    wrap_saver_writes_with_nstep_interval(saver, 100)
    wrap_saver_reads_with_delta_reconstruction(saver)
    builder: Any = StateGraph(AgentState, context_schema=AvaContext)
    builder.add_node("claim", claim_node, destinations=("before_llm", "claim", "__end__"))
    builder.add_node("before_llm", model, destinations=("__end__",))
    builder.add_edge(START, "claim")
    graph = builder.compile(checkpointer=saver)
    host = AgentHost(
        policy=configured_policy(),
        pool=pool,
        checkpointer=saver,
        graph=graph,
        machine="claim-test",
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
        catalog=model_catalog,
    )
    ctx = AvaContext(
        ops_pool=pool,
        event_publisher=MagicMock(),
        agent=AgentSlices.resolve(),
        db=Database.from_settings(),
        bus=EventBus.from_settings(),
        catalog=model_catalog,
    )
    return graph, saver, host, ctx
