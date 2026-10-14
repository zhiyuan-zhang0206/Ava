"""The pending scan never kills a running turn or hides an expired owner."""

import asyncio
import subprocess
import sys
import time
from collections.abc import Callable
from unittest.mock import AsyncMock
from uuid import uuid4

import psutil
import psycopg
import pytest
from langchain_core.messages import AIMessage
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.state import AgentState
from base.agents.incarnation import resources as resource_codec
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.packages.composer_commands import CommandCatalog
from base.sessions.tests.poll_until import poll_until_async
from services.agent_runner.agent_host.dispatcher import InboundWakeDispatcher, TurnScheduler
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.tests.host_policy import configured_machine, configured_policy
from services.agent_runner.agent_host.tests.lifecycle.recovery_admission import admit as _admit
from services.agent_runner.agent_host.tests.lifecycle.recovery_admission import (
    checkpoint_graph as _graph,
)


async def test_pending_scan_classifies_lifecycle_work_and_lease(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    insert_inbound: Callable[..., int],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    incarnation = await _admit(
        aops_pool,
        model_catalog=model_catalog,
        config_authority=config_authority,
        database_gate=database_gate,
        local_machine=machine_identity,
    )
    agent = incarnation.agent_id
    db_conn.execute("UPDATE agents_meta SET status='idling' WHERE id=%s", (agent,))
    lifecycle = insert_inbound(db_conn, agent, "", "system:test", kind="restart")
    db_conn.commit()
    host = AgentHost(
        policy=configured_policy(),
        pool=aops_pool,
        checkpointer=AsyncMock(),
        graph=AsyncMock(),
        bus=EventBus.from_settings(),
        db=Database.from_settings(gate=database_gate),
        catalog=model_catalog,
        machine=configured_machine(),
        commands=CommandCatalog(skills=list),
    )

    assert await host.pending_inbound_wakes() == [agent]

    ordinary = insert_inbound(db_conn, agent, "Need a reply", "user")
    db_conn.commit()
    assert await host.pending_inbound_wakes() == [agent]

    db_conn.execute(
        "UPDATE inbound_messages SET status='done' WHERE id IN (%s,%s)", (lifecycle, ordinary)
    )
    db_conn.execute(
        "INSERT INTO agent_impersonations(id,agent_id,session_id,source,machine,status,"
        "ttl_seconds,expires_at) VALUES(%s,%s,1,'cli',%s,'requested',300,"
        "clock_timestamp()+interval '5 minutes')",
        (uuid4(), agent, machine_identity.name()),
    )
    db_conn.commit()
    assert await host.pending_inbound_wakes() == [agent]


@pytest.mark.parametrize("silent_turn", [False, True])
async def test_old_pending_scan_never_cancels_a_running_turn(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    silent_turn: bool,
    insert_inbound: Callable[..., int],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    incarnation = await _admit(
        aops_pool,
        model_catalog=model_catalog,
        config_authority=config_authority,
        database_gate=database_gate,
        local_machine=machine_identity,
    )
    agent = incarnation.agent_id
    inbound = insert_inbound(db_conn, agent, "Queued before host recovery", "user")
    db_conn.execute(
        "UPDATE inbound_messages SET created_at=now()-interval '1 day' WHERE id=%s", (inbound,)
    )
    db_conn.execute(
        "UPDATE agents_meta SET last_active_at=now()-interval '1 day' WHERE id=%s", (agent,)
    )
    db_conn.commit()
    entered, release = asyncio.Event(), asyncio.Event()
    invocations: list[int] = []

    async def work(_state: AgentState) -> dict[str, object]:
        invocations.append(agent)
        host.turn_progress.mark(agent)
        entered.set()
        await release.wait()
        return {"turn_idle": True, "halted": True, "messages": [AIMessage(content="Completed")]}

    graph, saver = await _graph(aops_pool, agent, work)
    host = AgentHost(
        policy=configured_policy(),
        pool=aops_pool,
        graph=graph,
        checkpointer=saver,
        bus=EventBus.from_settings(),
        db=Database.from_settings(gate=database_gate),
        catalog=model_catalog,
        machine=configured_machine(),
        commands=CommandCatalog(skills=list),
    )
    host._owner = incarnation.owner
    scheduler = TurnScheduler(host.run_turn)
    dispatcher = InboundWakeDispatcher(
        EventBus.from_settings(),
        scheduler,
        pending_scan=host.pending_inbound_wakes,
    )
    try:
        scheduler.wake(agent)
        await asyncio.wait_for(entered.wait(), 3)
        assert await host.pending_inbound_wakes() == [agent]
        if silent_turn:
            # A progress clock silent for a day is the stall guard's to judge;
            # the scan has no cancellation path left.
            host.turn_progress._marks[agent] = [time.monotonic() - 86400]
        await dispatcher.scan_once()
        assert agent in scheduler.active_agents
        assert invocations == [agent]
        assert not scheduler.restart_required
    finally:
        release.set()
        await poll_until_async(lambda: not scheduler.active_agents, timeout=3)
        await scheduler.aclose()
        await host.aclose()
    state = await graph.aget_state({"configurable": {"thread_id": str(agent)}})
    assert state.values["messages"][-1].content == "Completed"


@pytest.mark.parametrize("status", ["running", "idling"])
@pytest.mark.parametrize(
    "boundary", ["fresh", "same_owner", "foreign", "unowned", "fatal", "terminated"]
)
async def test_owner_recovery_scan_excludes_unrelated_rows(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    status: str,
    boundary: str,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    incarnation = await _admit(
        aops_pool,
        model_catalog=model_catalog,
        config_authority=config_authority,
        database_gate=database_gate,
        local_machine=machine_identity,
    )
    agent = incarnation.agent_id
    host = AgentHost(
        policy=configured_policy(),
        pool=aops_pool,
        checkpointer=AsyncMock(),
        graph=AsyncMock(),
        bus=EventBus.from_settings(),
        db=Database.from_settings(gate=database_gate),
        catalog=model_catalog,
        machine=configured_machine(),
        commands=CommandCatalog(skills=list),
    )
    if boundary == "same_owner":
        host._owner = incarnation.owner
    db_conn.execute(
        "UPDATE agents_meta SET status=%s,machine=%s,lease_expires_at=now()+make_interval(secs=>%s) "
        "WHERE id=%s",
        (
            "terminated" if boundary == "terminated" else status,
            "another-machine" if boundary == "foreign" else machine_identity.name(),
            60 if boundary == "fresh" else -60,
            agent,
        ),
    )
    if boundary == "unowned":
        db_conn.execute(
            "UPDATE agents_meta SET runtime_owner=NULL,runtime_generation=NULL WHERE id=%s",
            (agent,),
        )
    if boundary == "fatal":
        db_conn.execute("UPDATE agents_meta SET last_turn_fatal_at=now() WHERE id=%s", (agent,))
    db_conn.commit()
    assert await host.pending_inbound_wakes() == []


@pytest.mark.parametrize("status", ["running", "idling"])
async def test_expired_scan_wake_cannot_steal_a_live_predecessor(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    status: str,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    incarnation = await _admit(
        aops_pool,
        model_catalog=model_catalog,
        config_authority=config_authority,
        database_gate=database_gate,
        local_machine=machine_identity,
    )
    agent = incarnation.agent_id
    host = AgentHost(
        policy=configured_policy(),
        pool=aops_pool,
        checkpointer=AsyncMock(),
        graph=AsyncMock(),
        bus=EventBus.from_settings(),
        db=Database.from_settings(gate=database_gate),
        catalog=model_catalog,
        machine=configured_machine(),
        commands=CommandCatalog(skills=list),
    )
    with subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
    ) as predecessor:
        assert predecessor.stdin is not None
        try:
            row = db_conn.execute(
                "SELECT incarnation_resources FROM agents_meta WHERE id=%s", (agent,)
            ).fetchone()
            assert row is not None
            resources = resource_codec.decode_resources(row[0])
            assert isinstance(resources, resource_codec.IncarnationResources)
            native = resource_codec.ResourceProcess.capture(psutil.Process(predecessor.pid))
            db_conn.execute(
                "UPDATE agents_meta SET status=%s,incarnation_resources=%s,"
                "lease_expires_at=now()-interval '1s' WHERE id=%s",
                (
                    status,
                    Jsonb(
                        resources.model_copy(update={"host_process": native}).model_dump(
                            mode="json"
                        )
                    ),
                    agent,
                ),
            )
            db_conn.commit()
            before = db_conn.execute(
                "SELECT status,runtime_generation,runtime_owner,incarnation_resources,"
                "lease_expires_at FROM agents_meta WHERE id=%s",
                (agent,),
            ).fetchone()
            assert await host.pending_inbound_wakes() == []
            await host.run_turn(agent)
            assert host.stats.cache_misses == 0
            assert (
                db_conn.execute(
                    "SELECT status,runtime_generation,runtime_owner,incarnation_resources,"
                    "lease_expires_at FROM agents_meta WHERE id=%s",
                    (agent,),
                ).fetchone()
                == before
            )
            observation = db_conn.execute(
                "SELECT last_admission_outcome,last_admission_at FROM agents_meta WHERE id=%s",
                (agent,),
            ).fetchone()
            assert observation is not None and observation[0] == "admission_guard_refused"
            assert observation[1] is not None
            assert predecessor.poll() is None
        finally:
            predecessor.stdin.close()
            predecessor.wait(timeout=3)
