"""Real durable restart ownership, journal failure and parked-intent preservation."""

import asyncio
from dataclasses import replace
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
from agent.db import claim_inbound_batch
from agent.graph.claim.node import claim_node
from agent.graph.exec.node import exec_node
from agent.impersonation import protect_native_hooks
from agent.ownership.hosted import admit_hosted_runtime, apply_hosted_lifecycle
from agent.startup import wrap_saver_writes_with_nstep_interval
from ava.sdk_surface.process_context import process_clients
from base.agents.context import AvaContext
from base.agents.context.identity import AgentIdentity
from base.agents.history.delta_read_compat import wrap_saver_reads_with_delta_reconstruction
from base.cluster.machine import machine_name
from base.db import Database
from base.deploy.maintenance import admission, cohort, pause_owner
from base.deploy.maintenance.state import MaintenancePhase
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import HostedTurnResources
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.runtime import TurnOutcome
from tests.factories.maintenance import WHEN, maintenance_agent, start_cluster_through_ready_gate
from tests.factories.maintenance import isolate as isolate
from tests.factories.maintenance import maintenance_agent as _agent


async def test_successor_cannot_sign_original_host_final_cleanup(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
) -> None:
    agent = _agent(db_conn)
    old = AgentHost(
        pool=aops_pool,
        checkpointer=MagicMock(),
        graph=MagicMock(),
        machine=machine_name(),
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
    )
    incarnation = await admit_hosted_runtime(
        aops_pool,
        agent,
        machine_name(),
        old._owner,
        expected_from="idling",
        db=database,
    )
    assert incarnation is not None
    pause_owner.begin_maintenance("owner", WHEN)
    hold = cohort.prepare(
        db_conn,
        machine=machine_name(),
        host_owner=old._owner,
        holder="owner",
        acquired_at=WHEN,
    )
    batch = await claim_inbound_batch(
        aops_pool, agent, lifecycle_only=True, incarnation=incarnation, work=None
    )
    assert [item.id for item in batch] == [hold.commands[agent]]
    assert (
        await apply_hosted_lifecycle(aops_pool, incarnation, bus=event_bus, resources=None)
        == "restart"
    )
    successor = AgentHost(
        pool=aops_pool,
        checkpointer=MagicMock(),
        graph=MagicMock(),
        machine=machine_name(),
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
    )
    await successor.run_turn(agent)
    current = admission.snapshot()
    assert current is not None and current.maintenance is not None
    assert current.maintenance.drained == ()
    await old.run_turn(agent)
    current = admission.snapshot()
    assert current is not None and current.maintenance is not None
    assert current.maintenance.drained == (agent,)


@pytest.mark.parametrize("failure_write", [1, 2])
async def test_journal_write_failure_before_or_after_commit_keeps_same_restart(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    failure_write: int,
    database: Database,
) -> None:
    agent, owner = _agent(db_conn), uuid4()
    assert (
        await admit_hosted_runtime(
            aops_pool, agent, machine_name(), owner, expected_from="idling", db=database
        )
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
        cohort.prepare(
            db_conn,
            machine=machine_name(),
            host_owner=owner,
            holder="retry",
            acquired_at=WHEN,
        )
    committed = db_conn.execute(
        "SELECT id FROM inbound_messages WHERE agent_id=%s AND kind='restart'", (agent,)
    ).fetchall()
    db_conn.commit()
    assert len(committed) == (1 if failure_write == 2 else 0)
    hold = cohort.prepare(
        db_conn,
        machine=machine_name(),
        host_owner=owner,
        holder="retry",
        acquired_at=WHEN,
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
    hold = cohort.prepare(
        db_conn,
        machine=machine_name(),
        host_owner=uuid4(),
        holder="parked",
        acquired_at=WHEN,
    )
    assert hold.parked == (agent,)
    assert hold.commands == {}
    assert db_conn.execute("SELECT * FROM agents_meta WHERE id=%s", (agent,)).fetchone() == before
    assert (
        db_conn.execute("SELECT id FROM inbound_messages WHERE agent_id=%s", (agent,)).fetchall()
        == []
    )
    cohort.verify_drained(db_conn, hold)


async def test_cold_idle_resume_uses_pointer_without_an_extra_model_call(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    from unittest.mock import AsyncMock

    from langchain_core.messages import HumanMessage
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.graph import START, StateGraph

    from agent import state as states
    from agent.graph.claim.node import claim_node
    from base.agents.context import AvaContext

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
    original = AgentHost(
        pool=aops_pool,
        checkpointer=saver,
        graph=graph,
        machine=machine_name(),
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
    )
    assert (
        await admit_hosted_runtime(
            aops_pool, agent, machine_name(), original._owner, expected_from="idling", db=database
        )
        is not None
    )
    pause_owner.begin_maintenance("idle", WHEN)
    hold = cohort.prepare(
        db_conn,
        machine=machine_name(),
        host_owner=original._owner,
        holder="idle",
        acquired_at=WHEN,
    )
    await original.run_turn(agent)
    current = admission.require_operation("idle", WHEN)
    assert current.maintenance is not None and current.maintenance.drained == (agent,)
    pause_owner.change_maintenance(
        "idle", WHEN, current.maintenance, current.maintenance, resumed=True
    )
    successor = AgentHost(
        pool=aops_pool,
        checkpointer=AsyncPostgresSaver(aops_pool),
        graph=builder.compile(checkpointer=AsyncPostgresSaver(aops_pool)),
        machine=machine_name(),
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
    )
    ctx = AvaContext(
        ops_pool=aops_pool,
        event_publisher=MagicMock(),
        llm=MagicMock(),
        agent=AgentSlices.resolve(),
        db=Database.from_settings(),
        bus=EventBus.from_settings(),
    )
    monkeypatch.setattr(successor, "_runtime_for", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        "services.agent_runner.agent_host.runtime.validate_model_config", MagicMock()
    )

    async def drive(
        _agent: int,
        _runtime: Any,
        _slices: object,
        *,
        incarnation: RuntimeIncarnation,
        resources: HostedTurnResources | None,
    ) -> TurnOutcome:
        return await successor._invoke_until_done(
            _agent, replace(ctx, original_incarnation=incarnation, hosted_resources=resources)
        )

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
    database: Database,
    event_bus: EventBus,
) -> None:
    agent = _agent(db_conn)
    host = AgentHost(
        pool=aops_pool,
        checkpointer=MagicMock(),
        graph=MagicMock(),
        machine=machine_name(),
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
    )
    incarnation = await admit_hosted_runtime(
        aops_pool,
        agent,
        machine_name(),
        host._owner,
        expected_from="idling",
        db=database,
    )
    assert incarnation is not None
    pause_owner.begin_maintenance("commit-gap", WHEN)
    original = pause_owner.change_maintenance

    def fail_final(*args: Any, **kwargs: Any) -> pause_owner.PauseOwnerSnapshot:
        if args[3].phase == MaintenancePhase.DRAINING:
            raise OSError("final journal unavailable")
        return original(*args, **kwargs)

    monkeypatch.setattr(pause_owner, "change_maintenance", fail_final)
    with pytest.raises(OSError):
        cohort.prepare(
            db_conn,
            machine=machine_name(),
            host_owner=host._owner,
            holder="commit-gap",
            acquired_at=WHEN,
        )
    batch = await claim_inbound_batch(
        aops_pool, agent, lifecycle_only=True, incarnation=incarnation, work=None
    )
    assert len(batch) == 1
    assert (
        await apply_hosted_lifecycle(aops_pool, incarnation, bus=event_bus, resources=None)
        == "restart"
    )
    monkeypatch.setattr(pause_owner, "change_maintenance", original)
    hold = cohort.prepare(
        db_conn,
        machine=machine_name(),
        host_owner=host._owner,
        holder="commit-gap",
        acquired_at=WHEN,
    )
    assert hold.commands == {agent: batch[0].id}
    assert hold.drained == ()
    await host.run_turn(agent)
    current = admission.require_operation("commit-gap", WHEN)
    assert current.maintenance is not None and current.maintenance.drained == (agent,)
    cohort.verify_drained(db_conn, current.maintenance)


def _model_that_runs_the_effect_tool_once(
    calls: list[str], entered: asyncio.Event, finish: asyncio.Event, effect: Path
) -> Any:
    """The llm node: park on `finish`, then ask `execute_code` to write `effect`; end on the next call."""

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

    return model


def _real_exec_graph_builder(model: Any, calls: list[str]) -> Any:
    """claim -> before_llm -> llm -> before_exec -> real exec -> after_exec -> claim."""

    async def route(_state: Any, _runtime: Any, _config: Any) -> Command[Any]:
        return Command(goto="llm")

    async def before_exec(_state: Any, _runtime: Any, _config: Any) -> Command[Any]:
        calls.append("before_exec")
        return Command(goto="exec")

    async def after_exec(_state: Any, _runtime: Any, _config: Any) -> Command[Any]:
        calls.append("after_exec")
        return Command(goto="claim")

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
    return builder


def _host_driving_invoke_until_done(
    monkeypatch: pytest.MonkeyPatch,
    pool: AsyncConnectionPool[Any],
    saver: AsyncPostgresSaver,
    graph: Any,
    ctx: AvaContext,
) -> AgentHost:
    host = AgentHost(
        pool=pool,
        checkpointer=saver,
        graph=graph,
        machine=machine_name(),
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
    )
    monkeypatch.setattr(host, "_runtime_for", AsyncMock(return_value=object()))

    async def drive(
        _agent: int,
        _runtime: Any,
        _slices: object,
        *,
        incarnation: RuntimeIncarnation,
        resources: HostedTurnResources | None,
    ) -> TurnOutcome:
        return await host._invoke_until_done(
            _agent, replace(ctx, original_incarnation=incarnation, hosted_resources=resources)
        )

    monkeypatch.setattr(host, "_drive_turns", drive)
    return host


async def _assert_cold_checkpoint_carries_the_exec_tool_result(
    aops_pool: AsyncConnectionPool[Any], config: RunnableConfig
) -> None:
    reader = AsyncPostgresSaver(aops_pool)
    wrap_saver_reads_with_delta_reconstruction(reader)
    cold = await reader.aget_tuple(config)
    assert cold is not None
    messages = cold.checkpoint["channel_values"]["messages"]
    assert any(
        isinstance(message, ToolMessage) and message.tool_call_id == "one" for message in messages
    )


async def test_admitted_model_finishes_real_exec_and_after_exec_before_drain_receipt(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent = maintenance_agent(db_conn)
    entered, finish = asyncio.Event(), asyncio.Event()
    calls: list[str] = []
    effect = tmp_path / "effect.txt"
    model = _model_that_runs_the_effect_tool_once(calls, entered, finish, effect)

    saver = AsyncPostgresSaver(aops_pool)
    await saver.setup()
    wrap_saver_writes_with_nstep_interval(saver, 100)
    builder = _real_exec_graph_builder(model, calls)
    graph = builder.compile(checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": str(agent)}}
    await graph.aupdate_state(
        config, {"messages": [HumanMessage(content="Do the action")], "halted": False}
    )
    ctx = AvaContext(
        ops_pool=aops_pool,
        event_publisher=MagicMock(),
        llm=MagicMock(),
        agent=AgentSlices.resolve(),
        db=Database.from_settings(),
        bus=EventBus.from_settings(),
        clients=process_clients(),
        identity=AgentIdentity(agent_id=agent, owns_loop=True),
    )
    monkeypatch.setattr(
        "services.agent_runner.agent_host.runtime.validate_model_config", MagicMock()
    )
    host = _host_driving_invoke_until_done(monkeypatch, aops_pool, saver, graph, ctx)
    work = asyncio.create_task(host.run_turn(agent))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        pause_owner.begin_maintenance("move", WHEN)
        hold = cohort.prepare(
            db_conn,
            machine=machine_name(),
            host_owner=host._owner,
            holder="move",
            acquired_at=WHEN,
        )
        assert not hold.drained
        finish.set()
        await asyncio.wait_for(work, 15)
        current = admission.require_operation("move", WHEN)
        assert current.maintenance is not None
        assert current.maintenance.drained == (agent,)
        cohort.verify_drained(db_conn, current.maintenance)
        await _assert_cold_checkpoint_carries_the_exec_tool_result(aops_pool, config)
        assert effect.read_text() == "once"
        assert calls == ["model", "before_exec", "after_exec"]
        await host.run_turn(agent)
        assert calls == ["model", "before_exec", "after_exec"]
        # Drop the old host's in-memory graph/cache: recovery consumes the
        # durable restart pointer and real cold checkpoint after explicit release.
        assert current.maintenance is not None
        start_cluster_through_ready_gate(monkeypatch)
        successor = _host_driving_invoke_until_done(
            monkeypatch,
            aops_pool,
            AsyncPostgresSaver(aops_pool),
            builder.compile(checkpointer=AsyncPostgresSaver(aops_pool)),
            ctx,
        )
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
