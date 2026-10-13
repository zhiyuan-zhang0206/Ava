"""Native ownership contract consumers split by their existing behavior."""

from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.tests.impersonation.test_impersonation import gate_ctx as gate_ctx
from agent.tests.impersonation.test_impersonation import incarnation as incarnation
from agent.tests.impersonation.test_impersonation import relays as relays
from base.agents.impersonation.tests.impersonation_support import attested_caller, recorded_tree
from base.agents.incarnation.resources import ResourceProcess
from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from tests.fixtures.unit.identity import machine_identity as machine_identity


async def test_successor_admission_ends_an_active_lease_without_restoring_the_dead_owner(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    exited_host: ResourceProcess,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """An active takeover does not follow the native runtime into its successor: the
    admission closes it, and the close does not restore the dead incarnation."""
    from agent.ownership.hosted import admit_hosted_runtime
    from base.agents import impersonation as leases
    from base.api_contracts.caller_identity import CallerIdentity
    from tests.fixtures.units import spawn_agent

    agent_id = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    first = await admit_hosted_runtime(
        aops_pool,
        agent_id,
        machine_identity.name(),
        uuid4(),
        expected_from="idling",
        db=database,
    )
    assert first is not None
    lease = leases.request(
        database,
        event_bus,
        agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex", instance="test"),
        ttl_seconds=3600,
        reason="Handle the next message",
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
        first,
        "Handoff brief",
        local_machine=machine_identity.name(),
    )
    leases.activate(database, event_bus, lease["id"], first)
    db_conn.execute(
        "UPDATE agents_meta SET lease_expires_at = clock_timestamp() - interval '1 second', "
        "incarnation_resources=jsonb_set(incarnation_resources,'{host_process}',%s) "
        "WHERE id=%s",
        (Jsonb(exited_host.model_dump(mode="json")), agent_id),
    )
    db_conn.commit()
    successor = await admit_hosted_runtime(
        aops_pool,
        agent_id,
        machine_identity.name(),
        uuid4(),
        expected_from="running",
        db=database,
    )
    assert successor is not None
    assert successor.generation != first.generation
    assert db_conn.execute(
        "SELECT status,rejection_reason,summary_inbound_id IS NOT NULL "
        "FROM agent_impersonations WHERE id=%s",
        (lease["id"],),
    ).fetchone() == ("expired", "aborted: native runtime restarted", True)
    # The end note reaches the replacement's inbox for its return handoff.
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND source='system:impersonation' "
        "AND content LIKE %s",
        (agent_id, "%native runtime restarted%"),
    ).fetchone() == (1,)
    # The controller's late release finds nothing to release and cannot touch the row.
    with pytest.raises(leases.ImpersonationError):
        leases.release(
            database,
            event_bus,
            lease["id"],
            attested_caller(lease),
            "Done after the restart",
            local_machine=machine_identity.name(),
        )
    db_conn.commit()
    assert db_conn.execute(
        "SELECT runtime_generation,runtime_owner FROM agents_meta WHERE id=%s",
        (agent_id,),
    ).fetchone() == (successor.generation, successor.owner)
