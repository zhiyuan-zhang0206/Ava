"""Real PostgreSQL ownership/claim and compiled graph drain boundaries."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import psycopg
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import START, StateGraph
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool

from agent import state as states
from agent.graph._claim import claim_node
from agent.graph._exec import exec_node
from agent.hosted_ownership import admit_hosted_runtime, settle_hosted_runtime
from agent.impersonation import protect_native_hooks
from agent.startup import _wrap_saver_writes_with_nstep_interval
from services.agent_host.host import AgentHost
from shared import maintenance, maintenance_cohort, pause_owner
from shared.context import AvaContext
from shared.db import create_agent, insert_inbound_message
from shared.machine import machine_name

WHEN = datetime(2026, 9, 6, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pause_owner, "state_path", lambda: tmp_path / "pause.json")
    monkeypatch.setattr(pause_owner, "lock_path", lambda: tmp_path / "pause.lock")


def _agent(conn: psycopg.Connection[Any]) -> int:
    agent_id = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta(id,status,machine) VALUES(%s,'idling',%s)",
        (agent_id, machine_name()),
    )
    conn.commit()
    return agent_id


async def test_admission_waiting_on_real_row_lock_cannot_escape_published_hold(
    db_conn: psycopg.Connection[Any], aops_pool: AsyncConnectionPool[Any]
) -> None:
    agent = _agent(db_conn)
    # This is a real PostgreSQL lock wait, not a mocked held() return.
    async with aops_pool.connection() as blocker, blocker.transaction():
        await blocker.execute("SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (agent,))
        attempt = asyncio.create_task(
            admit_hosted_runtime(aops_pool, agent, machine_name(), uuid4(), expected_from="idling")
        )
        try:
            async with asyncio.timeout(3):
                while True:
                    row = db_conn.execute(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                        "AND wait_event_type='Lock' AND query LIKE '%FROM agents_meta WHERE id=%'"
                    ).fetchone()
                    db_conn.commit()
                    if row and row[0]:
                        break
                    await asyncio.sleep(0.01)
            pause_owner.begin_maintenance("move", WHEN)
        except BaseException:
            attempt.cancel()
            raise
    assert await asyncio.wait_for(attempt, 3) is None
    row = db_conn.execute(
        "SELECT runtime_owner,runtime_generation,status FROM agents_meta WHERE id=%s", (agent,)
    ).fetchone()
    assert row == (None, None, "idling")


async def test_original_idle_cohort_preserves_pending_messages_and_rejects_successor(
    db_conn: psycopg.Connection[Any], aops_pool: AsyncConnectionPool[Any]
) -> None:
    agent, owner = _agent(db_conn), uuid4()
    incarnation = await admit_hosted_runtime(
        aops_pool, agent, machine_name(), owner, expected_from="idling"
    )
    assert incarnation is not None
    assert await settle_hosted_runtime(aops_pool, incarnation)
    message = insert_inbound_message(db_conn, agent, "pending work", "user")
    pause_owner.begin_maintenance("move", WHEN)
    hold = maintenance_cohort.prepare(
        db_conn, machine=machine_name(), host_owner=owner, holder="move", acquired_at=WHEN
    )
    assert set(hold.commands) == {agent}
    assert (
        maintenance_cohort.prepare(
            db_conn, machine=machine_name(), host_owner=owner, holder="move", acquired_at=WHEN
        )
        == hold
    )
    assert (
        await admit_hosted_runtime(
            aops_pool, agent, machine_name(), uuid4(), expected_from="idling"
        )
        is None
    )
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (message,)
    ).fetchone() == ("pending",)


async def test_admitted_model_finishes_real_exec_and_after_exec_before_drain_receipt(  # noqa: PLR0915 — one real graph/exec/DB boundary
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent = _agent(db_conn)
    entered, finish = asyncio.Event(), asyncio.Event()
    calls: list[str] = []
    effect = tmp_path / "effect.txt"

    async def model(_state: states.BaseAgentState) -> Command[Any]:
        calls.append("model")
        if calls.count("model") == 2:
            return Command(
                update={"halted": True, "messages": [AIMessage(content="Finished")]}, goto="__end__"
            )
        entered.set()
        await finish.wait()
        return Command(
            update={
                "messages": [
                    AIMessage(
                        content="Finish the admitted action",
                        tool_calls=[
                            {
                                "id": "one",
                                "name": "execute_code",
                                "args": {
                                    "code": f"from pathlib import Path\nPath({str(effect)!r}).write_text('once')"
                                },
                            }
                        ],
                    )
                ]
            },
            goto="before_exec",
        )

    async def route(_state: Any, _runtime: Any, _config: Any) -> Command[Any]:
        return Command(goto="llm")

    async def before_exec(_state: Any, _runtime: Any, _config: Any) -> Command[Any]:
        calls.append("before_exec")
        return Command(goto="exec")

    async def after_exec(_state: Any, _runtime: Any, _config: Any) -> Command[Any]:
        calls.append("after_exec")
        return Command(goto="claim")

    saver = AsyncPostgresSaver(aops_pool)
    await saver.setup()
    _wrap_saver_writes_with_nstep_interval(saver, 100)
    builder: Any = StateGraph(states.AgentState, context_schema=AvaContext)
    builder.add_node("claim", claim_node, destinations=("before_llm", "__end__", "claim"))
    builder.add_node(
        "before_llm",
        protect_native_hooks(route),
        destinations=("llm", "claim", "__end__"),
    )
    builder.add_node("llm", model, destinations=("before_exec", "__end__"))
    builder.add_node(
        "before_exec", protect_native_hooks(before_exec), destinations=("exec", "__end__")
    )
    builder.add_node(
        "exec", protect_native_hooks(cast(Any, exec_node)), destinations=("after_exec", "__end__")
    )
    builder.add_node(
        "after_exec", protect_native_hooks(after_exec), destinations=("claim", "__end__")
    )
    builder.add_edge(START, "claim")
    graph = builder.compile(checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": str(agent)}}
    await graph.aupdate_state(
        config, {"messages": [HumanMessage(content="Do the action")], "halted": False}
    )
    ctx = AvaContext(ops_pool=aops_pool, hosted=True, event_publisher=MagicMock(), llm=MagicMock())
    host = AgentHost(pool=aops_pool, checkpointer=saver, graph=graph, machine=machine_name())
    monkeypatch.setattr(host, "_runtime_for", AsyncMock(return_value=object()))
    monkeypatch.setattr("services.agent_host.host.validate_model_config", MagicMock())

    async def drive(_agent: int, _runtime: Any) -> bool:
        return await host._invoke_until_done(_agent, ctx)

    monkeypatch.setattr(host, "_drive_turns", drive)
    work = asyncio.create_task(host.run_turn(agent))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        pause_owner.begin_maintenance("move", WHEN)
        hold = maintenance_cohort.prepare(
            db_conn, machine=machine_name(), host_owner=host._owner, holder="move", acquired_at=WHEN
        )
        assert not hold.drained
        finish.set()
        await asyncio.wait_for(work, 15)
        current = maintenance.require_operation("move", WHEN)
        assert current.maintenance is not None
        assert current.maintenance.drained == (agent,)
        maintenance_cohort.verify_drained(db_conn, current.maintenance)
        cold = await AsyncPostgresSaver(aops_pool).aget_tuple(config)
        assert cold is not None
        messages = cold.checkpoint["channel_values"]["messages"]
        assert any(
            isinstance(message, ToolMessage) and message.tool_call_id == "one"
            for message in messages
        )
        assert effect.read_text() == "once"
        assert calls == ["model", "before_exec", "after_exec"]
        await host.run_turn(agent)
        assert calls == ["model", "before_exec", "after_exec"]
        # Drop the old host's in-memory graph/cache: recovery consumes the
        # durable restart pointer and real cold checkpoint after explicit release.
        assert current.maintenance is not None
        pause_owner.change_maintenance(
            "move", WHEN, current.maintenance, current.maintenance, resumed=True
        )
        successor = AgentHost(
            pool=aops_pool,
            checkpointer=AsyncPostgresSaver(aops_pool),
            graph=builder.compile(checkpointer=AsyncPostgresSaver(aops_pool)),
            machine=machine_name(),
        )
        monkeypatch.setattr(successor, "_runtime_for", AsyncMock(return_value=object()))

        async def resume_drive(_agent: int, _runtime: Any) -> bool:
            return await successor._invoke_until_done(_agent, ctx)

        monkeypatch.setattr(successor, "_drive_turns", resume_drive)
        wakes = await successor.pending_inbound_wakes(stale_after_s=300)
        assert agent in [wake.agent_id for wake in wakes]
        await successor.run_turn(agent)
        assert calls == ["model", "before_exec", "after_exec", "model"]
        assert effect.read_text() == "once"
        assert db_conn.execute(
            "SELECT status,observed_at IS NOT NULL FROM inbound_messages WHERE id=%s",
            (hold.commands[agent],),
        ).fetchone() == ("done", True)
        after = await AsyncPostgresSaver(aops_pool).aget_tuple(config)
        assert after is not None and after.checkpoint["channel_values"]["halted"] is True
    finally:
        finish.set()
        if not work.done():
            await asyncio.wait_for(work, 15)
