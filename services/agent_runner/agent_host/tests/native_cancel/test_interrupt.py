"""The unchanged watcher aborts real managed exec and discards model streams."""

import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import psycopg
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from agent.graph.claim.node import claim_node
from agent.graph.exec._result import _ExecCancelled
from agent.graph.exec._subprocess import _run_in_subprocess
from agent.graph.interrupt import subscribe_interrupt
from agent.graph.llm.node import llm_node
from agent.graph.llm_errors import LlmLedger
from agent.state import AgentState
from base.agents.incarnation.exec_owner_protocol import OwnerClosed
from base.agents.incarnation.native_work_models import NativeCancelMarker
from base.agents.messages.native_cancel import accept_native_cancel
from base.db import Database
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.native_process.turn_identity import (
    HostedTurnResources,
    bind_hosted_resources,
    bind_native_work,
    bind_turn_identity,
)
from services.agent_runner.agent_host.invocation.native_work import settle_native_invocation
from services.agent_runner.agent_host.tests.history.test_hosted_compact_failure import (
    _prepare_graph,
)
from services.agent_runner.agent_host.tests.native_cancel.helpers import managed_work
from tests.fixtures.pin_agent import exec_context


async def test_real_managed_exec_abort_closes_resources_before_original_ack(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    tmp_path: Path,
) -> None:
    pool: ConnectionPool
    incarnation, target = await managed_work(db_conn, aops_pool)
    graph, saver, config, _history = await _prepare_graph(aops_pool, target.agent_id, 100, [])
    scope = HostedTurnResources()
    with (
        bind_turn_identity(target.agent_id, incarnation=incarnation),
        bind_native_work(target.work_id),
        bind_hosted_resources(scope),
    ):
        async with subscribe_interrupt(aops_pool, target.agent_id) as interrupted:
            execution = asyncio.create_task(
                _run_in_subprocess(
                    Database.from_settings(),
                    "import time; print('strong-exec-started', flush=True); time.sleep(60)",
                    exec_context(target.agent_id),
                    interrupted,
                    30,
                    exec_dir=tmp_path,
                )
            )
            try:
                await _wait_attached_exec(db_conn, target.agent_id)
                with ConnectionPool[psycopg.Connection](db_conn.info.dsn) as pool:
                    accepted = await asyncio.to_thread(
                        accept_native_cancel, pool, "real-exec-stop", target.agent_id, target
                    )
                result, _payload = await asyncio.wait_for(execution, 10)
                assert isinstance(result, _ExecCancelled)
                assert interrupted.native_cancel == NativeCancelMarker(
                    command_id=accepted.command_id, target=target
                )
                assert not scope.unresolved
                assert db_conn.execute(
                    "SELECT incarnation_resources->'requests' FROM agents_meta WHERE id=%s",
                    (target.agent_id,),
                ).fetchone() == ({},)
                receipts = list(
                    (tmp_path / str(target.agent_id) / "domains").glob("*/owner.closed")
                )
                assert len(receipts) == 1
                assert (
                    OwnerClosed.model_validate_json(receipts[0].read_bytes()).allocation.request
                    is not None
                )
                assert await settle_native_invocation(
                    aops_pool, saver, graph, incarnation, target, config
                )
                assert db_conn.execute(
                    "SELECT outcome FROM native_cancel_commands WHERE work_id=%s", (target.work_id,)
                ).fetchone() == ("applied",)
            finally:
                if not execution.done():
                    execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)


async def test_real_model_watcher_discards_partial_then_claim_attributes_original(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
) -> None:
    pool: ConnectionPool
    incarnation, target = await managed_work(db_conn, aops_pool)
    entered, unwound = asyncio.Event(), asyncio.Event()

    async def stream():
        try:
            yield AIMessageChunk(content="partial generation")
            entered.set()
            await asyncio.Future()
        finally:
            unwound.set()

    ctx = exec_context(target.agent_id)
    model = MagicMock()
    model.bind_tools.return_value = model
    model.astream.return_value = stream()
    ctx = replace(
        ctx,
        llm=model,
        ops_pool=aops_pool,
        event_publisher=MagicMock(),
        agent=AgentSlices.resolve(),
        db=Database.from_settings(),
        bus=EventBus.from_settings(),
    )
    state = AgentState(
        messages=[HumanMessage(content="original")], halted=False, native_work=target
    )
    config: RunnableConfig = {"configurable": {"thread_id": str(target.agent_id)}}
    with (
        bind_turn_identity(target.agent_id, incarnation=incarnation),
        bind_native_work(target.work_id),
    ):
        running = asyncio.create_task(
            llm_node(state, Runtime(context=ctx), config, ledger=LlmLedger())
        )
        try:
            await asyncio.wait_for(entered.wait(), 5)
            with ConnectionPool[psycopg.Connection](db_conn.info.dsn) as pool:
                accepted = await asyncio.to_thread(
                    accept_native_cancel, pool, "real-model-stop", target.agent_id, target
                )
            result = await asyncio.wait_for(running, 5)
            assert result.update is not None and result.update.get("messages", []) == []
            assert result.update["halted"] is True and unwound.is_set()
            claimed = await claim_node(state, Runtime(context=ctx), config)
            assert claimed.update is not None
            assert claimed.update["native_cancel"] == NativeCancelMarker(
                command_id=accepted.command_id, target=target
            )
            assert claimed.goto == "__end__"
        finally:
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)


async def _wait_attached_exec(conn: psycopg.Connection, agent: int) -> None:
    async with asyncio.timeout(10):
        while True:
            row = conn.execute(
                "SELECT incarnation_resources FROM agents_meta WHERE id=%s", (agent,)
            ).fetchone()
            assert row is not None
            if (
                row[0]["requests"]
                and next(iter(row[0]["requests"].values()))["owner_process"] is not None
            ):
                return
            await asyncio.sleep(0.02)
