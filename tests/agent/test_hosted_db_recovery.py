"""Database loss preserves the original continuation and its ownership fence."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, LiteralString
from uuid import uuid4

import psycopg
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import START, StateGraph
from psycopg import sql
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from agent import state as states
from agent.hosted_ownership import admit_hosted_runtime
from agent.inbound_ownership import RuntimeOwnershipLostError
from ops.agent_spawn import create_agent_row
from services.agent_host import db_recovery
from services.agent_host.host import AgentHost
from shared import maintenance, maintenance_cohort, pause_owner
from shared.config import settings
from shared.context import AvaContext
from shared.db import insert_inbound_message
from shared.hosted_force import install_hosted_force
from shared.incarnation_resources import ResourceBirth
from shared.machine import machine_name
from shared.runtime_incarnation import RuntimeIncarnation
from shared.turn_identity import bind_turn_identity


@pytest.fixture(autouse=True)
def isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pause_owner, "state_path", lambda: tmp_path / "pause.json")
    monkeypatch.setattr(pause_owner, "lock_path", lambda: tmp_path / "pause.lock")
    monkeypatch.setattr(db_recovery, "_PROBE_TIMEOUT_SECONDS", 0.03)
    monkeypatch.setattr(db_recovery, "_INITIAL_BACKOFF_SECONDS", 0.01)
    monkeypatch.setattr(db_recovery, "_MAX_BACKOFF_SECONDS", 0.02)


async def _admit(pool: AsyncConnectionPool) -> RuntimeIncarnation:
    agent, _ = create_agent_row(spawner="user", machine=machine_name())
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE agents_meta SET incarnation_resources=%s WHERE id=%s",
            (Jsonb(ResourceBirth(birth=uuid4()).model_dump(mode="json")), agent),
        )
    incarnation = await admit_hosted_runtime(
        pool, agent, machine_name(), uuid4(), expected_from="idling"
    )
    assert incarnation is not None
    return incarnation


async def _graph(
    pool: AsyncConnectionPool[Any], agent: int, node: Any
) -> tuple[Any, AsyncPostgresSaver]:
    saver = AsyncPostgresSaver(pool)
    await saver.setup()
    builder: Any = StateGraph(states.AgentState, context_schema=AvaContext)
    builder.add_node("work", node)
    builder.add_edge(START, "work")
    builder.add_edge("work", "__end__")
    graph = builder.compile(checkpointer=saver)
    await graph.aupdate_state(
        {"configurable": {"thread_id": str(agent)}},
        {"messages": [HumanMessage(content="Continue existing work")], "halted": False},
        as_node="work",
    )
    return graph, saver


async def test_original_host_task_resumes_autonomous_work_without_pending_inbound(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incarnation = await _admit(aops_pool)
    agent = incarnation.agent_id
    recovering = asyncio.Event()
    refresh = db_recovery._refresh_owner

    async def observe(pool: AsyncConnectionPool, original: RuntimeIncarnation) -> None:
        recovering.set()
        await refresh(pool, original)

    monkeypatch.setattr(db_recovery, "_refresh_owner", observe)
    # Exhaust a real PostgreSQL pool: both the interrupted graph and recovery
    # get real PoolTimeout until the held connection is returned.
    async with AsyncConnectionPool[psycopg.AsyncConnection](
        settings.data_plane.db_url, min_size=1, max_size=1, kwargs={"autocommit": True}
    ) as control:
        invocations: list[int] = []

        async def work(_state: states.AgentState) -> dict[str, Any]:
            invocations.append(id(asyncio.current_task()))
            async with control.connection(timeout=0.03) as conn:
                await conn.execute("SELECT 1")
            return {"halted": True, "turn_idle": True, "messages": [AIMessage(content="Resumed")]}

        graph, saver = await _graph(aops_pool, agent, work)
        host = AgentHost(pool=aops_pool, control_pool=control, checkpointer=saver, graph=graph)
        with bind_turn_identity(agent, incarnation=incarnation):
            async with control.connection():
                original = asyncio.create_task(host._invoke_until_done(agent, AvaContext()))
                try:
                    await asyncio.wait_for(recovering.wait(), 3)
                    assert not original.done()
                    assert db_conn.execute(
                        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
                    ).fetchone() == (0,)
                    # An outage may outlast the lease. The retained exact owner
                    # can renew; a different/released owner is tested below.
                    db_conn.execute(
                        "UPDATE agents_meta SET lease_expires_at=clock_timestamp()-interval '1s' "
                        "WHERE id=%s",
                        (agent,),
                    )
                    db_conn.commit()
                except BaseException:
                    original.cancel()
                    await asyncio.gather(original, return_exceptions=True)
                    raise
            assert await asyncio.wait_for(original, 5) is False
        assert len(invocations) == 2
        cold = await saver.aget({"configurable": {"thread_id": str(agent)}})
        assert cold is not None
        assert cold["channel_values"]["halted"] is True
        assert cold["channel_values"]["messages"][-1].content == "Resumed"
        assert db_conn.execute(
            "SELECT runtime_generation,runtime_owner,lease_expires_at>clock_timestamp() "
            "FROM agents_meta WHERE id=%s",
            (agent,),
        ).fetchone() == (incarnation.generation, incarnation.owner, True)


@pytest.mark.parametrize("lost", ["owner", "generation", "terminated", "released", "frozen"])
async def test_recovery_never_repairs_or_renews_a_lost_or_forced_incarnation(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, lost: str
) -> None:
    incarnation = await _admit(aops_pool)
    agent = incarnation.agent_id

    async def never(_state: states.AgentState) -> dict[str, Any]:
        raise AssertionError("recovery must not invoke agent work")

    graph, saver = await _graph(aops_pool, agent, never)
    changes: dict[str, LiteralString] = {
        "owner": "runtime_owner=gen_random_uuid()",
        "generation": "runtime_generation=gen_random_uuid()",
        "terminated": "status='terminated'",
        "released": "lease_expires_at=NULL",
        "frozen": "incarnation_resources=jsonb_set(incarnation_resources,'{frozen_by}','1')",
    }
    db_conn.execute(
        sql.SQL("UPDATE agents_meta SET {} WHERE id=%s").format(sql.SQL(changes[lost])),
        (agent,),
    )
    db_conn.commit()
    before = db_conn.execute("SELECT * FROM agents_meta WHERE id=%s", (agent,)).fetchone()
    config: RunnableConfig = {"configurable": {"thread_id": str(agent)}}
    checkpoint = await saver.aget_tuple(config)
    with (
        bind_turn_identity(agent, incarnation=incarnation),
        pytest.raises(RuntimeOwnershipLostError, match="lost authority"),
    ):
        await db_recovery.recover_database(
            pool=aops_pool, graph=graph, checkpointer=saver, incarnation=incarnation
        )
    assert db_conn.execute("SELECT * FROM agents_meta WHERE id=%s", (agent,)).fetchone() == before
    assert await saver.aget_tuple(config) == checkpoint


async def test_cancelling_database_wait_keeps_checkpoint_and_does_not_ack_pause(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    incarnation = await _admit(aops_pool)
    agent = incarnation.agent_id

    async def never(_state: states.AgentState) -> dict[str, Any]:
        raise AssertionError("waiting must not invoke agent work")

    graph, saver = await _graph(aops_pool, agent, never)
    acquired = datetime.now(UTC)
    pause_owner.begin_maintenance("outage", acquired)
    hold = maintenance_cohort.prepare(
        db_conn,
        machine=machine_name(),
        host_owner=incarnation.owner,
        holder="outage",
        acquired_at=acquired,
    )
    async with (
        AsyncConnectionPool[psycopg.AsyncConnection](
            settings.data_plane.db_url, min_size=1, max_size=1, kwargs={"autocommit": True}
        ) as control,
        control.connection(),
    ):
        with bind_turn_identity(agent, incarnation=incarnation):
            task = asyncio.create_task(
                db_recovery.recover_database(
                    pool=control, graph=graph, checkpointer=saver, incarnation=incarnation
                )
            )
        await asyncio.sleep(0.06)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
    current = maintenance.require_operation("outage", acquired)
    assert current.maintenance is not None and not current.maintenance.drained
    assert db_conn.execute(
        "SELECT status,applied_at FROM inbound_messages WHERE id=%s", (hold.commands[agent],)
    ).fetchone() == ("pending", None)
    cold = await saver.aget({"configurable": {"thread_id": str(agent)}})
    assert cold is not None and cold["channel_values"]["halted"] is False
    # Once DB returns, the same original owner can recover to the ordinary
    # claim boundary. Recovery itself still cannot certify a drained restart.
    with bind_turn_identity(agent, incarnation=incarnation):
        await db_recovery.recover_database(
            pool=aops_pool, graph=graph, checkpointer=saver, incarnation=incarnation
        )
    resumed = maintenance.require_operation("outage", acquired)
    assert resumed.maintenance is not None and not resumed.maintenance.drained
    assert db_conn.execute(
        "SELECT applied_at FROM inbound_messages WHERE id=%s", (hold.commands[agent],)
    ).fetchone() == (None,)


@pytest.mark.parametrize("action", ["replace_owner", "force_terminate"])
async def test_decision_committed_during_outage_prevents_old_continuation(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, action: str
) -> None:
    incarnation = await _admit(aops_pool)
    agent = incarnation.agent_id

    async def never(_state: states.AgentState) -> dict[str, Any]:
        raise AssertionError("recovery cannot resume old work after a new decision")

    graph, saver = await _graph(aops_pool, agent, never)
    config: RunnableConfig = {"configurable": {"thread_id": str(agent)}}
    before = await saver.aget_tuple(config)
    async with AsyncConnectionPool[psycopg.AsyncConnection](
        settings.data_plane.db_url, min_size=1, max_size=1, kwargs={"autocommit": True}
    ) as control:
        async with control.connection():
            with bind_turn_identity(agent, incarnation=incarnation):
                task = asyncio.create_task(
                    db_recovery.recover_database(
                        pool=control, graph=graph, checkpointer=saver, incarnation=incarnation
                    )
                )
            try:
                async with asyncio.timeout(2):
                    while control.get_stats().get("requests_waiting", 0) == 0:
                        await asyncio.sleep(0.001)
                if action == "force_terminate":
                    command = insert_inbound_message(db_conn, agent, "", "user", kind="terminate")
                    db_conn.execute(
                        "UPDATE agents_meta SET status='terminated' WHERE id=%s", (agent,)
                    )
                    install_hosted_force(db_conn, agent, command)
                else:
                    db_conn.execute(
                        "UPDATE agents_meta SET runtime_owner=%s WHERE id=%s", (uuid4(), agent)
                    )
                db_conn.commit()
            except BaseException:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
        with pytest.raises(RuntimeOwnershipLostError):
            await asyncio.wait_for(task, 2)
    assert await saver.aget_tuple(config) == before


async def test_repair_timeout_retries_and_remains_cancellable(
    aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    incarnation = await _admit(aops_pool)

    async def never(_state: states.AgentState) -> dict[str, Any]:
        raise AssertionError("repair never invokes agent work")

    graph, saver = await _graph(aops_pool, incarnation.agent_id, never)
    monkeypatch.setattr(db_recovery, "_RECOVERY_TIMEOUT_SECONDS", 0.05)
    cancelled, retried = asyncio.Event(), asyncio.Event()
    attempts = 0

    async def stuck_flush(_saver: object, _agent: int) -> None:
        nonlocal attempts
        attempts += 1
        if attempts > 1:
            retried.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(db_recovery, "flush_checkpoint", stuck_flush)
    with bind_turn_identity(incarnation.agent_id, incarnation=incarnation):
        task = asyncio.create_task(
            db_recovery.recover_database(
                pool=aops_pool, graph=graph, checkpointer=saver, incarnation=incarnation
            )
        )
    try:
        await asyncio.wait_for(cancelled.wait(), 1)
        await asyncio.wait_for(retried.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("cancel", [False, True])
async def test_database_only_phase_bounds_real_pool_wait_and_preserves_cancellation(
    monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    monkeypatch.setattr(db_recovery, "_DATABASE_PHASE_TIMEOUT_SECONDS", 0.05)
    async with (
        AsyncConnectionPool[psycopg.AsyncConnection](
            settings.data_plane.db_url, min_size=1, max_size=1, kwargs={"autocommit": True}
        ) as pool,
        pool.connection(),
    ):

        async def borrow() -> None:
            async with db_recovery.database_phase(), pool.connection(timeout=10):
                raise AssertionError("the real connection is still held")

        task = asyncio.create_task(borrow())
        try:
            async with asyncio.timeout(1):
                while pool.get_stats().get("requests_waiting", 0) == 0:
                    await asyncio.sleep(0.001)
            if cancel:
                task.cancel()
            expected = asyncio.CancelledError if cancel else PoolTimeout
            with pytest.raises(expected):
                await asyncio.wait_for(task, 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
