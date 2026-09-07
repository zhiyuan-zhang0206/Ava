"""Real durable restart ownership, journal failure and parked-intent preservation."""

from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.db import claim_inbound_batch
from agent.hosted_ownership import admit_hosted_runtime, apply_hosted_lifecycle
from services.agent_host.host import AgentHost
from shared import maintenance, maintenance_cohort, pause_owner
from shared.machine import machine_name
from shared.turn_identity import bind_turn_identity
from tests.agent.test_maintenance import WHEN, _agent
from tests.agent.test_maintenance import isolate as isolate


async def test_successor_cannot_sign_original_host_final_cleanup(
    db_conn: psycopg.Connection[Any], aops_pool: AsyncConnectionPool[Any]
) -> None:
    agent = _agent(db_conn)
    old = AgentHost(
        pool=aops_pool, checkpointer=MagicMock(), graph=MagicMock(), machine=machine_name()
    )
    incarnation = await admit_hosted_runtime(
        aops_pool, agent, machine_name(), old._owner, expected_from="idling"
    )
    assert incarnation is not None
    pause_owner.begin_maintenance("owner", WHEN)
    hold = maintenance_cohort.prepare(
        db_conn, machine=machine_name(), host_owner=old._owner, holder="owner", acquired_at=WHEN
    )
    with bind_turn_identity(agent, incarnation=incarnation):
        batch = await claim_inbound_batch(aops_pool, agent, lifecycle_only=True)
        assert [item.id for item in batch] == [hold.commands[agent]]
        assert await apply_hosted_lifecycle(aops_pool, incarnation) == "restart"
    successor = AgentHost(
        pool=aops_pool, checkpointer=MagicMock(), graph=MagicMock(), machine=machine_name()
    )
    await successor.run_turn(agent)
    current = maintenance.snapshot()
    assert current is not None and current.maintenance is not None
    assert current.maintenance.drained == ()
    await old.run_turn(agent)
    current = maintenance.snapshot()
    assert current is not None and current.maintenance is not None
    assert current.maintenance.drained == (agent,)


@pytest.mark.parametrize("failure_write", [1, 2])
async def test_journal_write_failure_before_or_after_commit_keeps_same_restart(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    failure_write: int,
) -> None:
    agent, owner = _agent(db_conn), uuid4()
    assert (
        await admit_hosted_runtime(aops_pool, agent, machine_name(), owner, expected_from="idling")
        is not None
    )
    pause_owner.begin_maintenance("retry", WHEN)
    real = pause_owner.change_maintenance
    calls = 0

    def fail_once(*args: Any, **kwargs: Any) -> pause_owner.PauseOwnerSnapshot:
        nonlocal calls
        calls += 1
        if calls == failure_write:
            raise OSError("isolated journal failure")
        return real(*args, **kwargs)

    monkeypatch.setattr(pause_owner, "change_maintenance", fail_once)
    with pytest.raises(OSError, match="journal failure"):
        maintenance_cohort.prepare(
            db_conn, machine=machine_name(), host_owner=owner, holder="retry", acquired_at=WHEN
        )
    committed = db_conn.execute(
        "SELECT id FROM inbound_messages WHERE agent_id=%s AND kind='restart'", (agent,)
    ).fetchall()
    db_conn.commit()
    assert len(committed) == (1 if failure_write == 2 else 0)
    hold = maintenance_cohort.prepare(
        db_conn, machine=machine_name(), host_owner=owner, holder="retry", acquired_at=WHEN
    )
    rows = db_conn.execute(
        "SELECT id,target_owner,target_generation FROM inbound_messages "
        "WHERE agent_id=%s AND kind='restart'",
        (agent,),
    ).fetchall()
    assert rows == [(hold.commands[agent], None, None)]
    if committed:
        assert committed[0][0] == hold.commands[agent]


def test_unowned_idle_intent_is_preserved_without_restart_or_termination(
    db_conn: psycopg.Connection[Any],
) -> None:
    agent = _agent(db_conn)
    before = db_conn.execute("SELECT * FROM agents_meta WHERE id=%s", (agent,)).fetchone()
    db_conn.commit()
    pause_owner.begin_maintenance("parked", WHEN)
    hold = maintenance_cohort.prepare(
        db_conn, machine=machine_name(), host_owner=uuid4(), holder="parked", acquired_at=WHEN
    )
    assert hold.parked == (agent,)
    assert hold.commands == {}
    assert db_conn.execute("SELECT * FROM agents_meta WHERE id=%s", (agent,)).fetchone() == before
    assert (
        db_conn.execute("SELECT id FROM inbound_messages WHERE agent_id=%s", (agent,)).fetchall()
        == []
    )
    maintenance_cohort.verify_drained(db_conn, hold)


async def test_cold_idle_resume_uses_pointer_without_an_extra_model_call(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    from langchain_core.messages import HumanMessage
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.graph import START, StateGraph

    from agent import state as states
    from agent.graph._claim import claim_node
    from shared.context import AvaContext

    agent = _agent(db_conn)
    saver = AsyncPostgresSaver(aops_pool)
    await saver.setup()
    builder: Any = StateGraph(states.AgentState, context_schema=AvaContext)
    builder.add_node("claim", claim_node, destinations=("before_llm", "__end__", "claim"))
    model = AsyncMock(side_effect=AssertionError("idle restart must not call a model"))
    builder.add_node("before_llm", model)
    builder.add_edge(START, "claim")
    graph = builder.compile(checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": str(agent)}}
    await graph.aupdate_state(
        config, {"messages": [HumanMessage(content="Already finished")], "halted": True}
    )
    original = AgentHost(pool=aops_pool, checkpointer=saver, graph=graph, machine=machine_name())
    assert (
        await admit_hosted_runtime(
            aops_pool, agent, machine_name(), original._owner, expected_from="idling"
        )
        is not None
    )
    pause_owner.begin_maintenance("idle", WHEN)
    hold = maintenance_cohort.prepare(
        db_conn, machine=machine_name(), host_owner=original._owner, holder="idle", acquired_at=WHEN
    )
    await original.run_turn(agent)
    current = maintenance.require_operation("idle", WHEN)
    assert current.maintenance is not None and current.maintenance.drained == (agent,)
    pause_owner.change_maintenance(
        "idle", WHEN, current.maintenance, current.maintenance, resumed=True
    )
    successor = AgentHost(
        pool=aops_pool,
        checkpointer=AsyncPostgresSaver(aops_pool),
        graph=builder.compile(checkpointer=AsyncPostgresSaver(aops_pool)),
        machine=machine_name(),
    )
    ctx = AvaContext(ops_pool=aops_pool, event_publisher=MagicMock(), llm=MagicMock())
    monkeypatch.setattr(successor, "_runtime_for", AsyncMock(return_value=object()))
    monkeypatch.setattr("services.agent_host.host.validate_model_config", MagicMock())

    async def drive(_agent: int, _runtime: Any) -> bool:
        return await successor._invoke_until_done(_agent, ctx)

    monkeypatch.setattr(successor, "_drive_turns", drive)
    await successor.run_turn(agent)
    model.assert_not_awaited()
    assert db_conn.execute(
        "SELECT status,observed_at IS NOT NULL FROM inbound_messages WHERE id=%s",
        (hold.commands[agent],),
    ).fetchone() == ("done", True)
    cold = await AsyncPostgresSaver(aops_pool).aget_tuple(config)
    assert cold is not None and cold.checkpoint["channel_values"]["halted"] is True


async def test_prepare_retry_preserves_restart_applied_before_final_journal_write(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _agent(db_conn)
    host = AgentHost(
        pool=aops_pool, checkpointer=MagicMock(), graph=MagicMock(), machine=machine_name()
    )
    incarnation = await admit_hosted_runtime(
        aops_pool, agent, machine_name(), host._owner, expected_from="idling"
    )
    assert incarnation is not None
    pause_owner.begin_maintenance("commit-gap", WHEN)
    original = pause_owner.change_maintenance

    def fail_final(*args: Any, **kwargs: Any) -> pause_owner.PauseOwnerSnapshot:
        if args[3].phase == "draining":
            raise OSError("final journal unavailable")
        return original(*args, **kwargs)

    monkeypatch.setattr(pause_owner, "change_maintenance", fail_final)
    with pytest.raises(OSError):
        maintenance_cohort.prepare(
            db_conn,
            machine=machine_name(),
            host_owner=host._owner,
            holder="commit-gap",
            acquired_at=WHEN,
        )
    with bind_turn_identity(agent, incarnation=incarnation):
        batch = await claim_inbound_batch(aops_pool, agent, lifecycle_only=True)
        assert len(batch) == 1
        assert await apply_hosted_lifecycle(aops_pool, incarnation) == "restart"
    monkeypatch.setattr(pause_owner, "change_maintenance", original)
    hold = maintenance_cohort.prepare(
        db_conn,
        machine=machine_name(),
        host_owner=host._owner,
        holder="commit-gap",
        acquired_at=WHEN,
    )
    assert hold.commands == {agent: batch[0].id}
    assert hold.drained == ()
    await host.run_turn(agent)
    current = maintenance.require_operation("commit-gap", WHEN)
    assert current.maintenance is not None and current.maintenance.drained == (agent,)
    maintenance_cohort.verify_drained(db_conn, current.maintenance)
