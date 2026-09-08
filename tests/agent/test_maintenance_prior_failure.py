"""A prior ordinary crash cannot poison a later durable maintenance generation."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import psycopg
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from psycopg_pool import AsyncConnectionPool

from agent import state as states
from agent.impersonation import flush_checkpoint
from agent.startup import _wrap_saver_writes_with_nstep_interval
from services.agent_host import host as host_module
from services.agent_host.runtime import TurnOutcome
from shared import maintenance, maintenance_cohort, pause_owner
from shared.config import settings
from shared.context import AvaContext
from shared.db import insert_inbound_message
from shared.machine import machine_name
from tests.agent.test_maintenance import WHEN, _agent
from tests.agent.test_maintenance import isolate as isolate


async def _failed_turn(
    pool: AsyncConnectionPool[Any], agent: int, monkeypatch: pytest.MonkeyPatch
) -> tuple[host_module.AgentHost, AsyncPostgresSaver, RunnableConfig, AIMessage, list[str]]:
    calls: list[str] = []
    tail = AIMessage(id="saved-before-error", content="Preserve this completed work")

    async def save(_state: states.BaseAgentState) -> dict[str, object]:
        calls.append("save")
        return {"messages": [tail]}

    async def fail(_state: states.BaseAgentState) -> dict[str, object]:
        calls.append("fail")
        raise RuntimeError("isolated ordinary node failure")

    saver = AsyncPostgresSaver(pool)
    await saver.setup()
    _wrap_saver_writes_with_nstep_interval(saver, 100)
    builder: Any = StateGraph(states.AgentState, context_schema=AvaContext)
    builder.add_node("save", save)
    builder.add_node("fail", fail)
    builder.add_edge(START, "save")
    builder.add_edge("save", "fail")
    builder.add_edge("fail", END)
    graph = builder.compile(checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": str(agent)}}
    await graph.aupdate_state(
        config, {"messages": [HumanMessage(content="Original request")]}, as_node="fail"
    )
    await flush_checkpoint(saver, agent)
    host = host_module.AgentHost(pool=pool, checkpointer=saver, graph=graph, machine=machine_name())
    monkeypatch.setattr(host, "_runtime_for", AsyncMock(return_value=object()))
    monkeypatch.setattr(host_module, "validate_model_config", MagicMock())
    ctx = AvaContext(ops_pool=pool, event_publisher=MagicMock())

    async def drive(target: int, _runtime: object) -> TurnOutcome:
        return await host._invoke_until_done(target, ctx)

    monkeypatch.setattr(host, "_drive_turns", drive)
    with pytest.raises(RuntimeError, match="ordinary node failure"):
        await host.run_turn(agent)
    assert calls == ["save", "fail"]
    cold = await AsyncPostgresSaver(pool).aget_tuple(config)
    assert cold is not None
    assert tail not in cold.checkpoint["channel_values"]["messages"]
    return host, saver, config, tail, calls


async def test_prior_ordinary_failure_can_drain_without_replaying_work(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _agent(db_conn)
    host, _saver, config, tail, calls = await _failed_turn(aops_pool, agent, monkeypatch)
    chat = insert_inbound_message(db_conn, agent, "Leave queued work untouched", "user")
    original = db_conn.execute(
        "SELECT runtime_owner,runtime_generation FROM agents_meta WHERE id=%s", (agent,)
    ).fetchone()
    assert original is not None
    db_conn.commit()
    pause_owner.begin_maintenance("after-crash", WHEN)
    hold = maintenance_cohort.prepare(
        db_conn,
        machine=machine_name(),
        host_owner=host._owner,
        holder="after-crash",
        acquired_at=WHEN,
    )
    assert [wake.agent_id for wake in await host.pending_inbound_wakes(300)] == [agent]
    await host.run_turn(agent)
    current = maintenance.require_operation("after-crash", WHEN)
    assert current.maintenance is not None
    assert current.maintenance.drained == (agent,)
    maintenance_cohort.verify_drained(db_conn, current.maintenance)
    assert calls == ["save", "fail"]
    cold = await AsyncPostgresSaver(aops_pool).aget_tuple(config)
    assert cold is not None and tail in cold.checkpoint["channel_values"]["messages"]
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (chat,)
    ).fetchone() == ("pending",)
    assert db_conn.execute(
        "SELECT status,applied_at IS NOT NULL,observed_at,target_owner,target_generation "
        "FROM inbound_messages WHERE id=%s",
        (hold.commands[agent],),
    ).fetchone() == ("claimed", True, None, *original)
    assert db_conn.execute(
        "SELECT runtime_owner,runtime_generation,incarnation_resources FROM agents_meta WHERE id=%s",
        (agent,),
    ).fetchone() == (None, None, None)


async def test_prior_tail_flush_outage_defers_receipt_until_reflushed(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _agent(db_conn)
    host, _saver, config, tail, calls = await _failed_turn(aops_pool, agent, monkeypatch)
    pause_owner.begin_maintenance("failed-flush", WHEN)
    hold = maintenance_cohort.prepare(
        db_conn,
        machine=machine_name(),
        host_owner=host._owner,
        holder="failed-flush",
        acquired_at=WHEN,
    )
    broken = await psycopg.AsyncConnection.connect(settings.data_plane.db_url)
    await broken.close()

    async def fail_flush(_saver: object, _agent: int) -> None:
        await broken.execute("SELECT 1")

    with monkeypatch.context() as patch:
        patch.setattr(host_module, "flush_checkpoint", fail_flush)
        with pytest.raises(psycopg.OperationalError):
            await host.run_turn(agent)
    # A database-outage flush failure is crash-equivalent: recorded as an
    # undelivered receipt, never latched as a blocking failure. The buffered
    # tail is NOT silently cleared or dropped — it stays unflushed, and the
    # drain must not certify before a real re-flush succeeds.
    current = maintenance.require_operation("failed-flush", WHEN)
    assert current.maintenance is not None
    assert current.maintenance.failures == {}
    assert current.maintenance.undelivered == {agent: "OperationalError"}
    assert current.maintenance.drained == ()
    assert db_conn.execute(
        "SELECT status,claimed_at,applied_at FROM inbound_messages WHERE id=%s",
        (hold.commands[agent],),
    ).fetchone() == ("pending", None, None)
    cold = await AsyncPostgresSaver(aops_pool).aget_tuple(config)
    assert cold is not None and tail not in cold.checkpoint["channel_values"]["messages"]
    # Restoring the channel re-drives the receipt through the held-control
    # path: the wake scan keeps the agent woken (no failure fence), the held
    # controls re-flush the buffered tail BEFORE claiming the restart, and
    # only the resulting applied command certifies the drain.
    assert [wake.agent_id for wake in await host.pending_inbound_wakes(300)] == [agent]
    await host.run_turn(agent)
    current = maintenance.require_operation("failed-flush", WHEN)
    assert current.maintenance is not None
    assert current.maintenance.undelivered == {agent: "OperationalError"}
    assert current.maintenance.drained == (agent,)
    maintenance_cohort.verify_drained(db_conn, current.maintenance)
    assert await host.pending_inbound_wakes(300) == []
    assert db_conn.execute(
        "SELECT status,applied_at IS NOT NULL,observed_at FROM inbound_messages WHERE id=%s",
        (hold.commands[agent],),
    ).fetchone() == ("claimed", True, None)
    assert db_conn.execute(
        "SELECT runtime_owner,runtime_generation,incarnation_resources FROM agents_meta WHERE id=%s",
        (agent,),
    ).fetchone() == (None, None, None)
    cold = await AsyncPostgresSaver(aops_pool).aget_tuple(config)
    assert cold is not None and tail in cold.checkpoint["channel_values"]["messages"]
    assert calls == ["save", "fail"]
