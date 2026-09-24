"""Database loss preserves the original continuation and its ownership fence."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, LiteralString
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import psycopg
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import START, StateGraph
from psycopg import sql
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from agent import state as states
from agent.db import claim_inbound_batch
from agent.hosted_ownership import admit_hosted_runtime
from agent.inbound_ownership import RuntimeOwnershipLostError
from agent.startup import _wrap_saver_writes_with_nstep_interval
from ops.agent_spawn import create_agent_row
from services.agent_host import db_recovery
from services.agent_host.host import AgentHost
from shared import maintenance, maintenance_cohort, pause_owner
from shared.agents.history.delta_read_compat import wrap_saver_reads_with_delta_reconstruction
from shared.config import settings
from shared.context import AvaContext
from shared.db import insert_inbound_message
from shared.hosted_db_wait import database_wait_snapshot
from shared.hosted_force import install_hosted_force
from shared.incarnation_resources import ResourceBirth
from shared.machine import machine_name
from shared.maintenance_state import MaintenanceHold
from shared.runtime_incarnation import RuntimeIncarnation
from shared.turn_identity import bind_turn_identity


@pytest.fixture(autouse=True)
def isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pause_owner, "state_path", lambda: tmp_path / "pause.json")
    monkeypatch.setattr(pause_owner, "lock_path", lambda: tmp_path / "pause.lock")
    monkeypatch.setattr(db_recovery, "_PROBE_TIMEOUT_SECONDS", 0.03)
    monkeypatch.setattr(db_recovery, "_INITIAL_BACKOFF_SECONDS", 0.01)
    monkeypatch.setattr(db_recovery, "_MAX_BACKOFF_SECONDS", 0.02)
    monkeypatch.setattr(settings.daemon, "host_db_recovery_prolonged_attempts", 6)
    monkeypatch.setattr(settings.daemon, "host_db_recovery_prolonged_seconds", 300.0)
    monkeypatch.setattr(settings.daemon, "host_db_recovery_budget_seconds", 3600.0)


async def _admit(pool: AsyncConnectionPool) -> RuntimeIncarnation:
    agent, _, _prompt_id, _attempt_id = create_agent_row(spawner="user", machine=machine_name())
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
    # Delta write model (#3180): fold delta-written messages on read (daemon parity).
    wrap_saver_reads_with_delta_reconstruction(saver)
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
            assert not (await asyncio.wait_for(original, 5)).exited
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
    monkeypatch.setattr(db_recovery, "_DATABASE_PHASE_TIMEOUT_SECONDS", 0.05)
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


async def _seed_stalled_repair_scenario(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool[Any],
    incarnation: RuntimeIncarnation,
) -> tuple[AsyncPostgresSaver, Any, RunnableConfig, int, MaintenanceHold, list[int], datetime]:
    """The issue #1972 repro state: a claimed inbound whose turn left a
    dangling private tool call in the retained checkpoint, an unacked
    maintenance hold, and a graph that must never run during recovery.

    The state is seeded through one real graph superstep (a pass-through
    node), because the repair stage's unattributed `aupdate_state` needs the
    checkpoint's `versions_seen` to name a node — a pure update_state seed
    leaves it empty and the update is ambiguous on current langgraph.
    """
    aid = incarnation.agent_id
    graph_calls: list[int] = []

    async def work(_state: states.AgentState) -> dict[str, Any]:
        graph_calls.append(1)
        return {
            "messages": [
                AIMessage(
                    id="unfinished-tool",
                    content="",
                    tool_calls=[
                        {"id": "private-tool", "name": "execute_code", "args": {"code": "pass"}}
                    ],
                )
            ]
        }

    saver = AsyncPostgresSaver(aops_pool)
    await saver.setup()
    _wrap_saver_writes_with_nstep_interval(saver, 100)
    # Delta write model (#3180): fold delta-written messages on read (daemon parity).
    wrap_saver_reads_with_delta_reconstruction(saver)
    builder: Any = StateGraph(states.AgentState, context_schema=AvaContext)
    builder.add_node("work", work)
    builder.add_edge(START, "work")
    builder.add_edge("work", "__end__")
    graph = builder.compile(checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": str(aid)}}
    inbound = insert_inbound_message(db_conn, aid, "Original private request", "user")
    db_conn.commit()
    with bind_turn_identity(aid, incarnation=incarnation):
        await claim_inbound_batch(aops_pool, aid)
        await graph.ainvoke(
            {
                "messages": [
                    HumanMessage(
                        content="Original private request",
                        additional_kwargs={"ava_inbound_id": inbound},
                    )
                ]
            },
            config,
        )
        # The superstep checkpoint (with the dangling tool call) is durable
        # at once — delta threads retire the nstep buffer (#3180), so the
        # crash shape is the dangling call persisted, not a buffered tail.
    at = datetime.now(UTC)
    pause_owner.begin_maintenance("private-slow-recovery", at)
    hold = maintenance_cohort.prepare(
        db_conn,
        machine=machine_name(),
        host_owner=incarnation.owner,
        holder="private-slow-recovery",
        acquired_at=at,
    )
    db_conn.commit()
    graph_calls.clear()
    return saver, graph, config, inbound, hold, graph_calls, at


async def test_healthy_stages_each_get_their_own_deadline(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #1972: stages that each fit the old 5s aggregate but not together
    must still complete — the chain is bounded per stage, not per attempt."""
    import time

    incarnation = await _admit(aops_pool)
    saver, graph, config, inbound, hold, graph_calls, at = await _seed_stalled_repair_scenario(
        db_conn, aops_pool, incarnation
    )
    reader = AsyncPostgresSaver(aops_pool)  # type: ignore[arg-type]
    wrap_saver_reads_with_delta_reconstruction(reader)
    cold = await reader.aget(config)
    assert cold is not None
    # Delta write model (#3180): delta threads retire the nstep throttle, so
    # the superstep's writes are durable at once — the crash shape here is the
    # dangling call persisted (and not yet repaired), not a buffered tail.
    assert any(
        getattr(msg, "id", None) == "unfinished-tool" for msg in cold["channel_values"]["messages"]
    )
    events: list[tuple[str, str, float]] = []
    reconcile = db_recovery._reconcile_claimed_inbounds_at_startup
    repair = db_recovery._repair_dangling_tool_use_at_startup

    async def delay(stage: str) -> None:
        started = time.monotonic()
        async with aops_pool.connection() as conn:
            await conn.execute("SELECT pg_sleep(3.0)")
        events.append((stage, "query_complete", round(time.monotonic() - started, 3)))

    async def slow_reconcile(
        pool: AsyncConnectionPool, checkpointer: AsyncPostgresSaver, agent: int
    ) -> None:
        await delay("reconcile")
        await reconcile(pool, checkpointer, agent)
        events.append(("reconcile", "done", 0))

    async def slow_repair(compiled: Any, agent: int) -> None:
        await delay("repair")
        await repair(compiled, agent)
        events.append(("repair", "done", 0))

    monkeypatch.setattr(db_recovery, "_reconcile_claimed_inbounds_at_startup", slow_reconcile)
    monkeypatch.setattr(db_recovery, "_repair_dangling_tool_use_at_startup", slow_repair)
    try:
        with bind_turn_identity(incarnation.agent_id, incarnation=incarnation):
            await asyncio.wait_for(
                db_recovery.recover_database(
                    pool=aops_pool,
                    checkpointer=saver,
                    graph=graph,
                    incarnation=incarnation,
                ),
                15,
            )
    finally:
        reader = AsyncPostgresSaver(aops_pool)  # type: ignore[arg-type]
        wrap_saver_reads_with_delta_reconstruction(reader)
        cold = await reader.aget(config)
        assert cold is not None
        msgs = cold["channel_values"]["messages"]
        assert any(getattr(msg, "id", None) == "unfinished-tool" for msg in msgs)
        assert db_conn.execute(
            "SELECT status FROM inbound_messages WHERE id=%s", (inbound,)
        ).fetchone() == ("done",)
        assert db_conn.execute(
            "SELECT applied_at FROM inbound_messages WHERE id=%s",
            (hold.commands[incarnation.agent_id],),
        ).fetchone() == (None,)
        current = maintenance.require_operation("private-slow-recovery", at)
        assert current.maintenance is not None and not current.maintenance.drained
    assert sum(event[:2] == ("reconcile", "done") for event in events) == 1
    assert sum(event[:2] == ("repair", "done") for event in events) == 1
    assert all(elapsed < 5 for _, kind, elapsed in events if kind == "query_complete")
    assert any(isinstance(msg, ToolMessage) and msg.tool_call_id == "private-tool" for msg in msgs)
    assert not graph_calls


@pytest.fixture
def recovery_observation(monkeypatch: pytest.MonkeyPatch) -> tuple[list[float], Mock, AsyncMock]:
    clock = [1000.0]
    # Replace only this module's clock; real pool and asyncio deadlines still run.
    monkeypatch.setattr(db_recovery, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    log = Mock()
    monkeypatch.setattr(db_recovery, "logger", log)

    async def advance_backoff(_delay: float) -> None:
        clock[0] += 10.0

    backoff = AsyncMock(side_effect=advance_backoff)
    monkeypatch.setattr(db_recovery.RecoveryInterrupt, "wait_backoff", backoff)
    return clock, log, backoff


@pytest.mark.parametrize(
    ("error", "stage_seconds", "expected_error_type"),
    [
        (PoolTimeout("unavailable"), 290.0, "PoolTimeout"),
        (psycopg.errors.AdminShutdown(), 300.0, "AdminShutdown"),
        (TimeoutError("unavailable"), 290.0, "PoolTimeout"),
    ],
)
async def test_recovery_budget_abandons_at_attempt_boundary(
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    recovery_observation: tuple[list[float], Mock, AsyncMock],
    error: Exception,
    stage_seconds: float,
    expected_error_type: str,
) -> None:
    clock, log, backoff = recovery_observation
    monkeypatch.setattr(settings.daemon, "host_db_recovery_budget_seconds", 600.0)
    incarnation = await _admit(aops_pool)
    graph, saver = await _graph(aops_pool, incarnation.agent_id, AsyncMock())
    attempts = 0

    async def failed_repair(_graph: Any, _agent: int) -> None:
        nonlocal attempts
        attempts += 1
        assert attempts <= 2, "a spent ladder must not start another repair attempt"
        clock[0] += stage_seconds
        raise error

    monkeypatch.setattr(db_recovery, "_repair_dangling_tool_use_at_startup", failed_repair)
    with (
        bind_turn_identity(incarnation.agent_id, incarnation=incarnation),
        pytest.raises(db_recovery.DatabaseRecoveryBudgetExceededError, match="after 2 attempts"),
    ):
        await db_recovery.recover_database(
            pool=aops_pool, graph=graph, checkpointer=saver, incarnation=incarnation
        )
    assert attempts == backoff.await_count == 2
    assert database_wait_snapshot(incarnation.agent_id) is None
    log.error.assert_called_once_with(
        "host checkpoint recovery abandoned",
        agent_id=incarnation.agent_id,
        attempts=2,
        total_elapsed_seconds=2 * (stage_seconds + 10.0),
        final_phase="tool_state_repair",
        last_error_type=expected_error_type,
        last_sqlstate=error.sqlstate if isinstance(error, psycopg.Error) else None,
    )
    assert (
        sum(c.args[0] == "host checkpoint recovery retry" for c in log.warning.call_args_list) == 2
    )
    assert all(c.args[0] != "host turn checkpoint recovered" for c in log.info.call_args_list)
    assert any(
        c.args[0]
        == (
            "host checkpoint recovery stage failed"
            if isinstance(error, psycopg.OperationalError) and not isinstance(error, PoolTimeout)
            else "host checkpoint recovery stage timed out"
        )
        and c.kwargs["phase"] == "tool_state_repair"
        and c.kwargs["duration_ms"] >= 0
        for c in log.warning.call_args_list
    )


@pytest.mark.parametrize(("attempt_limit", "seconds_limit"), [(2, 300.0), (99, 50.0), (2, 50.0)])
async def test_recovery_prolonged_warns_once_at_first_threshold_crossing(
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    recovery_observation: tuple[list[float], Mock, AsyncMock],
    attempt_limit: int,
    seconds_limit: float,
) -> None:
    clock, log, backoff = recovery_observation
    monkeypatch.setattr(settings.daemon, "host_db_recovery_prolonged_attempts", attempt_limit)
    monkeypatch.setattr(settings.daemon, "host_db_recovery_prolonged_seconds", seconds_limit)
    incarnation = await _admit(aops_pool)
    graph, saver = await _graph(aops_pool, incarnation.agent_id, AsyncMock())
    flush = db_recovery.flush_checkpoint
    attempts = 0

    async def flaky_flush(checkpointer: AsyncPostgresSaver, agent: int) -> None:
        nonlocal attempts
        attempts += 1
        clock[0] += 20.0
        if attempts <= 3:
            raise PoolTimeout("checkpoint unavailable")
        await flush(checkpointer, agent)

    monkeypatch.setattr(db_recovery, "flush_checkpoint", flaky_flush)
    with bind_turn_identity(incarnation.agent_id, incarnation=incarnation):
        await db_recovery.recover_database(
            pool=aops_pool, graph=graph, checkpointer=saver, incarnation=incarnation
        )
    warnings = [
        c for c in log.warning.call_args_list if c.args[0] == "host checkpoint recovery prolonged"
    ]
    assert len(warnings) == 1
    assert warnings[0].kwargs == {
        "agent_id": incarnation.agent_id,
        "attempts": 2,
        "total_elapsed_seconds": 50.0,
        "phase": "checkpoint_flush",
        "error_type": "PoolTimeout",
        "sqlstate": None,
    }
    assert attempts == 4
    assert backoff.await_count == 3
    log.error.assert_not_called()
    assert sum(c.args[0] == "host turn checkpoint recovered" for c in log.info.call_args_list) == 1


@pytest.mark.parametrize("failures", [0, 2])
async def test_recovery_summary_counts_all_attempts_and_backoff_time(
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    recovery_observation: tuple[list[float], Mock, AsyncMock],
    failures: int,
) -> None:
    clock, log, backoff = recovery_observation
    incarnation = await _admit(aops_pool)
    graph, saver = await _graph(aops_pool, incarnation.agent_id, AsyncMock())
    refresh = db_recovery._refresh_owner
    failed_probes = 0

    async def flaky_probe(pool: AsyncConnectionPool, original: RuntimeIncarnation) -> None:
        nonlocal failed_probes
        clock[0] += 1.0
        if failed_probes < failures:
            failed_probes += 1
            raise PoolTimeout("owner unavailable")
        await refresh(pool, original)

    monkeypatch.setattr(db_recovery, "_refresh_owner", flaky_probe)
    with bind_turn_identity(incarnation.agent_id, incarnation=incarnation):
        await db_recovery.recover_database(
            pool=aops_pool, graph=graph, checkpointer=saver, incarnation=incarnation
        )
    assert backoff.await_count == failures
    assert database_wait_snapshot(incarnation.agent_id) is not None
    recovered = [
        c for c in log.info.call_args_list if c.args[0] == "host turn checkpoint recovered"
    ]
    assert len(recovered) == 1
    assert recovered[0].kwargs == {
        "agent_id": incarnation.agent_id,
        "attempt": failures + 1,
        "elapsed_seconds": 3.0,
        "total_attempts": failures + 1,
        "total_elapsed_seconds": failures * 11.0 + 3.0,
    }
    stages = [
        c for c in log.info.call_args_list if c.args[0] == "host checkpoint recovery stage complete"
    ]
    assert len(stages) == 6
    assert {c.kwargs["phase"] for c in stages} == {
        "owner_probe",
        "checkpoint_flush",
        "inbound_reconciliation",
        "owner_revalidation",
        "tool_state_repair",
        "repaired_owner_validation",
    }
    assert all(c.kwargs["outcome"] == "success" and c.kwargs["duration_ms"] >= 0 for c in stages)
    assert all(
        c.args[0] != "host checkpoint recovery prolonged" for c in log.warning.call_args_list
    )
    log.error.assert_not_called()


@pytest.mark.parametrize("write_before_retry", [False, True])
async def test_recovery_reuses_unchanged_checkpoint_across_retry(
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    write_before_retry: bool,
) -> None:
    incarnation = await _admit(aops_pool)

    async def never(_state: states.AgentState) -> dict[str, Any]:
        raise AssertionError("recovery cannot invoke agent work")

    graph, saver = await _graph(aops_pool, incarnation.agent_id, never)
    config: RunnableConfig = {"configurable": {"thread_id": str(incarnation.agent_id)}}
    await graph.aupdate_state(
        config, {"messages": [HumanMessage(content="Another message")]}, as_node="work"
    )
    raw = await AsyncPostgresSaver.aget_tuple(saver, config)
    assert raw is not None
    assert "messages" not in raw.checkpoint["channel_values"]

    history = saver.aget_delta_channel_history
    walks = 0
    flushes = 0
    repairs = 0

    async def counted_history(*, config: RunnableConfig, channels: Any) -> Any:
        nonlocal walks
        walks += 1
        return await history(config=config, channels=channels)

    async def counted_flush(_saver: AsyncPostgresSaver, _agent: int) -> None:
        nonlocal flushes
        flushes += 1

    async def flaky_repair(_graph: Any, _agent: int) -> None:
        nonlocal repairs
        repairs += 1
        await graph.aget_state(config)
        if repairs == 1:
            if write_before_retry:
                await graph.aupdate_state(
                    config,
                    {"messages": [HumanMessage(content="State changed in repair")]},
                    as_node="work",
                )
            raise PoolTimeout("retry after a completed read")

    monkeypatch.setattr(saver, "aget_delta_channel_history", counted_history)
    monkeypatch.setattr(db_recovery, "flush_checkpoint", counted_flush)
    monkeypatch.setattr(db_recovery, "_repair_dangling_tool_use_at_startup", flaky_repair)
    with bind_turn_identity(incarnation.agent_id, incarnation=incarnation):
        await db_recovery.recover_database(
            pool=aops_pool, graph=graph, checkpointer=saver, incarnation=incarnation
        )
    assert repairs == 2
    assert flushes == (2 if write_before_retry else 1)
    assert walks == (2 if write_before_retry else 1)
    await saver.aget_tuple(config)
    assert walks == (3 if write_before_retry else 2)
