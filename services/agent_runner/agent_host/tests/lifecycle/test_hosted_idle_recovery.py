"""Hosted idle recovery and reaper successor marker lifecycle."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.state import BaseAgentState
from base.agents.incarnation.resources import (
    IncarnationResources,
    ResourceProcess,
)
from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.deploy.maintenance import pause_owner
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from services.agent_runner.agent_host.dispatcher import TurnScheduler
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.tests.host_policy import configured_machine, configured_policy
from services.agent_runner.agent_host.tests.lifecycle.recovery_admission import admit as _admit
from tests.components.base.poll_until import poll_until_async
from tests.fixtures.unit.identity import machine_identity as machine_identity


def _accept_model(**_kwargs: object) -> None:
    pass


def _unexpected_model(state: BaseAgentState) -> dict[str, object]:
    del state
    pytest.fail("quiet idle recovery reached a model/initialization node")


def _dead_birth_copy(host_process: ResourceProcess) -> ResourceProcess:
    """Same PID, different birth: the predecessor has exited, even if its PID
    was recycled. No real host or test worker is stopped for this evidence."""
    if host_process.starttime is not None:
        return host_process.model_copy(update={"starttime": host_process.starttime + 1})
    return host_process.model_copy(update={"birth": host_process.birth - 60})


def _mark_predecessor_idle(
    db_conn: psycopg.Connection,
    resources: IncarnationResources,
    agent: int,
    *,
    evidence: str,
    released: bool,
) -> None:
    """Idle the agent with the predecessor's incarnation evidence either absent
    (`legacy`) or present but dead (`managed`), and the lease either released
    or still (about to be) expired."""
    assert resources.host_process is not None
    dead = _dead_birth_copy(resources.host_process)
    incarnation_resources = (
        None
        if evidence == "legacy"
        else Jsonb(resources.model_copy(update={"host_process": dead}).model_dump(mode="json"))
    )
    db_conn.execute(
        "UPDATE agents_meta SET status='idling',incarnation_resources=%s,"
        "lease_expires_at=CASE WHEN %s THEN NULL ELSE now()-interval '1s' END WHERE id=%s",
        (incarnation_resources, released, agent),
    )
    db_conn.commit()


async def test_maintenance_hold_does_not_adopt_a_quiet_foreign_owner(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
    db_conn.execute(
        "UPDATE agents_meta SET status='idling',lease_expires_at=now()-interval '1s' WHERE id=%s",
        (agent,),
    )
    db_conn.commit()
    host = AgentHost(
        policy=configured_policy(),
        pool=aops_pool,
        graph=AsyncMock(),
        checkpointer=AsyncMock(),
        bus=EventBus.from_settings(),
        db=Database.from_settings(gate=database_gate),
        catalog=model_catalog,
        machine=configured_machine(),
    )
    assert await host.pending_inbound_wakes(60) == []
    before = db_conn.execute("SELECT * FROM agents_meta WHERE id=%s", (agent,)).fetchone()
    monkeypatch.setattr(pause_owner, "state_path", lambda: tmp_path / "pause.json")
    monkeypatch.setattr(pause_owner, "lock_path", lambda: tmp_path / "pause.lock")
    pause_owner.begin_maintenance("test-idle-recovery", datetime.now(UTC))
    assert await host.pending_inbound_wakes(60) == []
    assert db_conn.execute("SELECT * FROM agents_meta WHERE id=%s", (agent,)).fetchone() == before


class TestReapedSuccessorMarker:
    @pytest.mark.parametrize("outcome", ["return", "error", "cancel"])
    async def test_reaped_successor_marker_clears_when_task_finishes(self, outcome: str) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        failure = ValueError("turn failed")

        async def run_turn(_agent_id: int) -> None:
            entered.set()
            await release.wait()
            if outcome == "error":
                raise failure

        scheduler = TurnScheduler(run_turn)
        try:
            original = scheduler.wake(1)
            assert original is not None
            original.cancel()  # Reap before the first pump runs.
            await poll_until_async(lambda: scheduler.reaped_successor(1) is not None, timeout=3)
            successor = scheduler.reaped_successor(1)
            assert successor is not None
            await asyncio.wait_for(entered.wait(), 3)
            assert scheduler.reaped_successor(1) is successor
            if outcome == "cancel":
                successor.cancel()
            else:
                release.set()
            await poll_until_async(
                lambda: successor.done() and scheduler.reaped_successor(1) is None, timeout=3
            )
        finally:
            release.set()
            if outcome == "error":
                with pytest.raises(ValueError) as joined:
                    await scheduler.aclose()
                assert joined.value is failure
            else:
                await scheduler.aclose()
