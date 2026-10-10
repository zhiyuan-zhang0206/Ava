"""Native ownership contract consumers split by their existing behavior."""

from typing import Any
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.tests.impersonation.test_impersonation import gate_ctx as gate_ctx
from agent.tests.impersonation.test_impersonation import incarnation as incarnation
from agent.tests.impersonation.test_impersonation import relays as relays
from base.agents.incarnation.resources import ResourceProcess
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from tests.impersonation_support import attested_caller, recorded_tree


async def test_successor_admission_aligns_active_lease_binding_before_release(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    exited_host: ResourceProcess,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """Successor admission aligns accepted_* before release; native_status
    cannot restore the dead incarnation even before a held wake."""
    from agent.ownership.hosted import admit_hosted_runtime
    from base.agents import impersonation as leases
    from base.agents.messages.caller_identity import CallerIdentity
    from base.cluster.machine import machine_name
    from tests.fixtures.units import spawn_agent

    agent_id = spawn_agent(catalog=model_catalog, authority=config_authority)
    first = await admit_hosted_runtime(
        aops_pool,
        agent_id,
        machine_name(),
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
    )
    leases.accept(database, event_bus, lease["id"], agent_id, first, "Handoff brief")
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
        machine_name(),
        uuid4(),
        expected_from="running",
        db=database,
    )
    assert successor is not None
    assert successor.generation != first.generation
    assert db_conn.execute(
        "SELECT accepted_generation,accepted_owner FROM agent_impersonations WHERE id=%s",
        (lease["id"],),
    ).fetchone() == (successor.generation, successor.owner)
    # Release before any native_status: the restore trigger fires, but the
    # binding already matches the live incarnation — agents_meta is untouched.
    leases.release(
        database, event_bus, lease["id"], attested_caller(lease), "Done before the first held wake"
    )
    db_conn.commit()
    assert db_conn.execute(
        "SELECT runtime_generation,runtime_owner FROM agents_meta WHERE id=%s",
        (agent_id,),
    ).fetchone() == (successor.generation, successor.owner)
