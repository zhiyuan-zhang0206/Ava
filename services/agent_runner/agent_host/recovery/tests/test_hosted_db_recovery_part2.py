"""Database loss preserves the original continuation and its ownership fence."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import psycopg
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import START, StateGraph
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from agent import state as states
from agent.db import claim_inbound_batch
from agent.ownership.hosted import admit_hosted_runtime
from agent.startup import wrap_saver_writes_with_nstep_interval
from base.agents.context import AvaContext
from base.agents.history.delta_read_compat import wrap_saver_reads_with_delta_reconstruction
from base.agents.incarnation.resources import ResourceBirth
from base.agents.observation import db_wait
from base.agents.observation.db_wait import DatabaseWaits
from base.cluster.machine import machine_name
from base.config import settings
from base.db import Database, insert_inbound_message
from base.deploy.maintenance import cohort, pause_owner
from base.deploy.maintenance.state import MaintenanceHold
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
from ops.agents.spawn import create_agent_row
from services.agent_runner.agent_host import db_recovery


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
    agent, _, _prompt_id, _attempt_id = create_agent_row(
        Database.from_settings(), EventBus.from_settings(), spawner="user", machine=machine_name()
    )
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE agents_meta SET incarnation_resources=%s WHERE id=%s",
            (Jsonb(ResourceBirth(birth=uuid4()).model_dump(mode="json")), agent),
        )
    incarnation = await admit_hosted_runtime(
        pool, agent, machine_name(), uuid4(), expected_from="idling", db=Database.from_settings()
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
    wrap_saver_writes_with_nstep_interval(saver, 100)
    # Delta write model (#3180): fold delta-written messages on read (daemon parity).
    wrap_saver_reads_with_delta_reconstruction(saver)
    builder: Any = StateGraph(states.AgentState, context_schema=AvaContext)
    builder.add_node("work", work)
    builder.add_edge(START, "work")
    builder.add_edge("work", "__end__")
    graph = builder.compile(checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": str(aid)}}
    inbound = insert_inbound_message(
        db_conn,
        aid,
        "Original private request",
        "user",
        bus=EventBus.from_settings(),
        database=Database.from_settings(),
    )
    db_conn.commit()
    await claim_inbound_batch(aops_pool, aid, incarnation=incarnation, work=None)
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
    at = datetime.now(UTC)
    pause_owner.begin_maintenance("private-slow-recovery", at)
    hold = cohort.prepare(
        db_conn,
        machine=machine_name(),
        host_owner=incarnation.owner,
        holder="private-slow-recovery",
        acquired_at=at,
    )
    db_conn.commit()
    graph_calls.clear()
    return saver, graph, config, inbound, hold, graph_calls, at


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
    monkeypatch.setattr(db_recovery, "repair_dangling_tool_use_at_startup", flaky_repair)
    await db_recovery.recover_database(
        pool=aops_pool,
        graph=graph,
        checkpointer=saver,
        incarnation=incarnation,
        database_waits=DatabaseWaits(),
        peek_lock=asyncio.Lock(),
        work=None,
    )
    assert repairs == 2
    assert flushes == (2 if write_before_retry else 1)
    assert walks == (2 if write_before_retry else 1)
    await saver.aget_tuple(config)
    assert walks == (3 if write_before_retry else 2)


def test_database_phase_bound_fits_checkpoint_recovery_band() -> None:
    """INC-927 (task #4781): observed checkpoint reads ran 25-45s under load.

    The bound must fit a full settle pass (read + write + flush + receipt) and
    every recovery stage, while staying finite as the one-stage fence (#1972).
    """
    assert db_recovery._DATABASE_PHASE_TIMEOUT_SECONDS == 120.0


def test_db_wait_proof_ttl_covers_widened_recovery_stages() -> None:
    """The wait proof must outlive two bounded stages plus the documented slack."""
    slack_seconds = 10 + 3 + 15 + 12  # heartbeat, publication, sleep, scheduling
    assert (
        2 * db_recovery._DATABASE_PHASE_TIMEOUT_SECONDS + slack_seconds
    ) <= db_wait.DB_WAIT_PROOF_TTL_SECONDS
