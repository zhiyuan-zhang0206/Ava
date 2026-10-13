"""A replacement host's admission ends the takeover of the host it replaced."""

from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent import impersonation_process
from agent.ownership.hosted import admit_hosted_runtime
from base.agents import impersonation as leases
from base.agents.impersonation.tests.impersonation_support import attested_caller, recorded_tree
from base.api_contracts.caller_identity import CallerIdentity
from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from services.agent_runner.agent_host.tests.host_policy import configured_machine, configured_policy
from tests.fixtures.unit.identity import machine_identity as machine_identity


async def test_replacement_host_admission_ends_the_takeover_without_respawning_its_relay(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    from services.agent_runner.agent_host.host import AgentHost

    agent_id = create_agent(db_conn)
    machine = machine_identity.name()
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine) VALUES(%s,'idling',%s)", (agent_id, machine)
    )
    db_conn.commit()
    owner = await admit_hosted_runtime(
        aops_pool, agent_id, machine, uuid4(), expected_from="idling", db=database
    )
    assert owner is not None
    lease = leases.request(
        database,
        event_bus,
        agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex"),
        process_metadata=recorded_tree(),
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
        authority=config_authority,
        local_machine=machine_identity.name(),
    )
    leases.accept(
        database,
        event_bus,
        lease["id"],
        agent_id,
        owner,
        "Handoff brief",
        local_machine=machine_identity.name(),
    )
    leases.activate(database, event_bus, lease["id"], owner)
    # A real codex relay process must never start inside the test environment.
    spawn = MagicMock(return_value=MagicMock(poll=MagicMock(return_value=None)))
    monkeypatch.setattr(impersonation_process, "spawn_codex_relay", spawn)
    graph = MagicMock()
    host = AgentHost(
        policy=configured_policy(),
        pool=aops_pool,
        checkpointer=MagicMock(),
        graph=graph,
        machine=configured_machine(name=machine),
        bus=EventBus.from_settings(),
        db=Database.from_settings(gate=database_gate),
        catalog=model_catalog,
    )
    # The original host has stopped renewing; admission uses its ordinary ownership fence.
    db_conn.execute(
        "UPDATE agents_meta SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
        (agent_id,),
    )
    db_conn.commit()
    await host.run_turn(agent_id)
    assert db_conn.execute(
        "SELECT runtime_owner FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (host._owner,)
    db_conn.commit()
    ended = leases.get(
        database,
        event_bus,
        lease["id"],
        attested_caller(lease),
        local_machine=machine_identity.name(),
    )
    assert ended["status"] == "expired"
    assert ended["rejection_reason"] == "aborted: native runtime restarted"
    # Nothing re-provisions the previous host's relay for the ended lease.
    spawn.assert_not_called()
