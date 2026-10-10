"""A lost result/commit never starts another native invocation or claims chat."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import START, StateGraph
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from agent.graph.claim.node import claim_node
from agent.startup import wrap_saver_writes_with_nstep_interval
from agent.state import AgentState, BaseAgentState
from agent.tests.claim.test_inbound_ownership import _insert
from base.agents.context import AvaContext
from base.agents.history.delta_read_compat import wrap_saver_reads_with_delta_reconstruction
from base.agents.messages.native_cancel import accept_native_cancel, observe_native_work
from base.db import Database
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from services.agent_runner.agent_host import host as host_owner
from services.agent_runner.agent_host import invocation as invocation_owner
from services.agent_runner.agent_host.invocation import native_work as work_owner
from services.agent_runner.agent_host.settlement import close_hosted_turn
from services.agent_runner.agent_host.tests.host_policy import configured_policy
from services.agent_runner.agent_host.tests.native_cancel.helpers import managed_work
from services.agent_runner.agent_host.tests.runtime.hosted_resources import hosted_scope


@dataclass
class _Faults:
    invocations: int = 0
    injected: bool = False


def _install_faults(patch: pytest.MonkeyPatch, agent: int, site: str) -> _Faults:
    """Faults wrap actual PostgreSQL flush/transactions, never fake production storage."""
    faults = _Faults()
    actual_invoke, actual_flush, actual_tx = (
        host_owner.run_invocation_with_stall_guard,
        invocation_owner.flush_checkpoint,
        work_owner.async_write_transaction,
    )

    async def invoke(*args: Any, **kwargs: Any) -> Any:
        faults.invocations += 1
        result = await actual_invoke(*args, **kwargs)
        if site == "graph_result" and not faults.injected:
            faults.injected = True
            raise psycopg.OperationalError("test lost graph result")
        return result

    async def flush(checkpointer: object, target: int) -> None:
        if site == "before_flush" and not faults.injected:
            faults.injected = True
            raise psycopg.OperationalError("test before checkpoint flush")
        await actual_flush(checkpointer, target)
        if site == "after_flush" and not faults.injected:
            faults.injected = True
            raise psycopg.OperationalError("test lost flush response")

    @asynccontextmanager
    async def transaction(*args: Any, **kwargs: Any):
        finishing = False
        async with actual_tx(*args, **kwargs) as conn:
            yield conn
            row = await (
                await conn.execute(
                    "SELECT outcome FROM native_cancel_commands WHERE agent_id=%s", (agent,)
                )
            ).fetchone()
            finishing = row is not None and row[0] == "applied"
            if finishing and site == "before_ack" and not faults.injected:
                faults.injected = True
                raise psycopg.OperationalError("test before ACK commit")
        if finishing and site == "after_ack" and not faults.injected:
            faults.injected = True
            raise psycopg.OperationalError("test lost ACK commit response")

    patch.setattr(host_owner, "run_invocation_with_stall_guard", invoke)
    patch.setattr(invocation_owner, "flush_checkpoint", flush)
    patch.setattr(work_owner, "async_write_transaction", transaction)
    return faults


@pytest.mark.parametrize(
    "site", ["graph_result", "before_flush", "after_flush", "before_ack", "after_ack"]
)
async def test_original_invocation_settles_once_after_database_fault(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    site: str,
    database: Database,
    model_catalog: ModelCatalog,
) -> None:
    async with hosted_scope() as resources:
        pool: ConnectionPool
        incarnation, initial = await managed_work(db_conn, aops_pool)
        agent = initial.agent_id
        first = _insert(db_conn, agent)
        entered, release = asyncio.Event(), asyncio.Event()

        async def model(state: BaseAgentState) -> Command[Any]:
            assert not state.halted
            entered.set()
            await release.wait()
            return Command(update={"halted": True}, goto="claim")

        saver = AsyncPostgresSaver(aops_pool)
        await saver.setup()
        wrap_saver_writes_with_nstep_interval(saver, 100)
        wrap_saver_reads_with_delta_reconstruction(saver)
        builder: Any = StateGraph(AgentState, context_schema=AvaContext)
        builder.add_node("claim", claim_node, destinations=("before_llm", "claim", "__end__"))
        builder.add_node("before_llm", model, destinations=("claim",))
        builder.add_edge(START, "claim")
        graph = builder.compile(checkpointer=saver)
        host = host_owner.AgentHost(
            policy=configured_policy(),
            pool=aops_pool,
            checkpointer=saver,
            graph=graph,
            machine="claim-test",
            bus=EventBus.from_settings(),
            db=database,
            catalog=model_catalog,
        )
        ctx = AvaContext(
            ops_pool=aops_pool,
            event_publisher=MagicMock(),
            agent=AgentSlices.resolve(default_reader=configured_policy().default_reader),
            db=database,
            bus=EventBus.from_settings(),
            catalog=model_catalog,
            clock_factory=configured_policy().clock_factory,
        )
        faults = _install_faults(monkeypatch, agent, site)
        running = asyncio.create_task(
            host._invoke_until_done(
                agent,
                replace(ctx, original_incarnation=incarnation, hosted_resources=resources),
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 10)
            with ConnectionPool[psycopg.Connection](db_conn.info.dsn) as pool:
                target = await asyncio.to_thread(observe_native_work, pool, agent)
                assert target is not None and target.work_id != initial.work_id
                accepted = await asyncio.to_thread(
                    accept_native_cancel, pool, "original-work", agent, target
                )
                repeated = await asyncio.to_thread(
                    accept_native_cancel, pool, "original-work", agent, target
                )
                assert repeated == accepted
            queued = _insert(db_conn, agent)
            release.set()
            outcome = await asyncio.wait_for(running, 15)
        finally:
            release.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        await close_hosted_turn(
            aops_pool,
            aops_pool,
            ctx.require_db(),
            ctx.require_bus(),
            saver,
            incarnation,
            outcome,
            resources=None,
            wake_enabled=configured_policy().recovery_wake_enabled,
            prompt_reap_enabled=configured_policy().recrash_reap_enabled,
            reconcile_inputs=configured_policy().reconcile_inputs,
        )
        assert faults.injected
        assert faults.invocations == 1
        assert not outcome.crashed and not outcome.native_held
        assert db_conn.execute(
            "SELECT outcome FROM native_cancel_commands WHERE id=%s", (accepted.command_id,)
        ).fetchone() == ("applied",)
        assert db_conn.execute(
            "SELECT native_work_id FROM agents_meta WHERE id=%s", (agent,)
        ).fetchone() == (target.work_id,)
        assert db_conn.execute(
            "SELECT status FROM inbound_messages WHERE id=%s", (queued,)
        ).fetchone() == ("pending",)
        assert db_conn.execute(
            "SELECT status FROM inbound_messages WHERE id=%s", (first,)
        ).fetchone() == ("done",)
