"""Actual serialized pump cannot certify a force over a live pause writer."""

import asyncio
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from agent.ownership.hosted import admit_hosted_runtime
from base.agents.inbox.native_cancel import accept_native_cancel
from base.agents.incarnation.native_work_models import NativeWorkUncertainError
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.native_process.turn_identity import HostedTurnResources
from base.packages.composer_commands import CommandCatalog
from ops.agents.resurrection_retry import ResurrectSettlementDeferredError
from ops.agents.wake import resurrect_agent
from ops.lifecycle.termination import force_terminate_transaction
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.invocation.native_work import (
    recover_native_cancel,
    settle_native_invocation,
)
from services.agent_runner.agent_host.tests.history.test_hosted_compact_failure import (
    prepare_graph,
)
from services.agent_runner.agent_host.tests.host_policy import configured_machine, configured_policy
from services.agent_runner.agent_host.tests.managed_work import managed_work


async def test_force_observation_waits_for_actual_projection_continuation(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    *,
    database: Database,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    pool: ConnectionPool
    incarnation, target = await managed_work(db_conn, aops_pool, database_gate=database_gate)
    graph, saver, config, _history = await prepare_graph(aops_pool, target.agent_id, 100, [])
    with ConnectionPool[psycopg.Connection](db_conn.info.dsn) as pool:
        await asyncio.to_thread(accept_native_cancel, pool, "pump-barrier", target.agent_id, target)
    entered, release = asyncio.Event(), asyncio.Event()
    actual_update = graph.aupdate_state

    async def pause(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return await actual_update(*args, **kwargs)

    host = AgentHost(
        policy=configured_policy(),
        pool=aops_pool,
        checkpointer=saver,
        graph=graph,
        machine=configured_machine(name="claim-test"),
        bus=EventBus.from_settings(),
        db=database,
        catalog=model_catalog,
        commands=CommandCatalog(skills=list),
    )
    host._owner = incarnation.owner

    async def continuation(agent: int, *, resources: HostedTurnResources | None) -> None:
        assert agent == target.agent_id
        await settle_native_invocation(
            aops_pool, saver, graph, incarnation, target, config, resources=resources
        )

    monkeypatch.setattr(graph, "aupdate_state", pause)
    monkeypatch.setattr(host, "_run_turn", continuation)
    running = asyncio.create_task(host.run_turn(target.agent_id))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        with ConnectionPool[psycopg.Connection](db_conn.info.dsn) as pool:
            _status, _pid, _pages, force, _shell_cutoff = await asyncio.to_thread(
                force_terminate_transaction, target.agent_id, pool, source="user"
            )
        running.cancel()
        await asyncio.sleep(0)
        assert not running.done()
        assert db_conn.execute(
            "SELECT observed_at FROM inbound_messages WHERE id=%s", (force,)
        ).fetchone() == (None,)
        with pytest.raises(ResurrectSettlementDeferredError):
            await asyncio.to_thread(
                resurrect_agent,
                database,
                EventBus.from_settings(),
                target.agent_id,
                resurrected_by="user",
                local_machine=machine_identity.name(),
            )
        release.set()
        # The original owner cannot ACK after force changed its authority, but
        # the pump only records actual force observation after its writer drains.
        with pytest.raises(NativeWorkUncertainError):
            await asyncio.wait_for(running, 5)
        observed = db_conn.execute(
            "SELECT observed_at FROM inbound_messages WHERE id=%s", (force,)
        ).fetchone()
        assert observed is not None and observed[0] is not None
        await asyncio.to_thread(
            resurrect_agent,
            database,
            EventBus.from_settings(),
            target.agent_id,
            resurrected_by="user",
            local_machine=machine_identity.name(),
        )
        successor = await admit_hosted_runtime(
            aops_pool,
            target.agent_id,
            "claim-test",
            uuid4(),
            db=database,
            expected_from="idling",
        )
        assert successor is not None
        assert not await recover_native_cancel(aops_pool, successor)
        assert db_conn.execute(
            "SELECT outcome FROM native_cancel_commands WHERE work_id=%s", (target.work_id,)
        ).fetchone() == ("uncertain",)
    finally:
        release.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)
