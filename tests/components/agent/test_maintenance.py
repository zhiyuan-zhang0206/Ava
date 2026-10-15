import asyncio
from typing import Any
from uuid import uuid4

import psycopg
from psycopg_pool import AsyncConnectionPool

from agent.ownership.hosted import admit_hosted_runtime, settle_hosted_runtime
from base.agents.birth.spawn import create_agent_row
from base.agents.inbox.insert import insert_inbound_message
from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.deploy.maintenance import cohort, pause_owner
from base.deploy.tests.maintenance_factory import WHEN, maintenance_agent
from base.deploy.tests.maintenance_factory import isolate as isolate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog


def test_maintenance_parks_a_never_admitted_birth_without_consuming_its_marker(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    machine_identity: MachineIdentity,
) -> None:
    agent, _, _, _ = create_agent_row(
        database,
        event_bus,
        spawner="user",
        machine=machine_identity.name(),
        authority=config_authority,
        catalog=model_catalog,
    )
    query = "SELECT incarnation_resources FROM agents_meta WHERE id=%s"
    marker = db_conn.execute(query, (agent,)).fetchone()
    assert marker is not None and marker[0]["state"] == "unadmitted"
    db_conn.commit()
    pause_owner.begin_maintenance("move", WHEN)
    hold = cohort.prepare(
        db_conn, machine=machine_identity.name(), host_owner=None, holder="move", acquired_at=WHEN
    )
    assert hold.parked == (agent,) and hold.commands == {}
    cohort.verify_drained(db_conn, hold)
    assert db_conn.execute(query, (agent,)).fetchone() == marker


async def test_admission_waiting_on_real_row_lock_cannot_escape_published_hold(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    machine_identity: MachineIdentity,
) -> None:
    agent = maintenance_agent(db_conn, machine_name=machine_identity.name())
    async with aops_pool.connection() as blocker, blocker.transaction():
        await blocker.execute("SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (agent,))
        attempt = asyncio.create_task(
            admit_hosted_runtime(
                aops_pool,
                agent,
                machine_identity.name(),
                uuid4(),
                expected_from="idling",
                db=database,
            )
        )
        try:
            async with asyncio.timeout(3):
                while True:
                    row = db_conn.execute(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                        "AND wait_event_type='Lock' AND query LIKE '%FROM agents_meta WHERE id=%'"
                    ).fetchone()
                    db_conn.commit()
                    if row and row[0]:
                        break
                    await asyncio.sleep(0.01)
            pause_owner.begin_maintenance("move", WHEN)
        except BaseException:
            attempt.cancel()
            raise
    assert await asyncio.wait_for(attempt, 3) is None
    row = db_conn.execute(
        "SELECT runtime_owner,runtime_generation,status FROM agents_meta WHERE id=%s", (agent,)
    ).fetchone()
    assert row == (None, None, "idling")


async def test_original_idle_cohort_preserves_pending_messages_and_rejects_successor(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    machine_identity: MachineIdentity,
) -> None:
    agent, owner = maintenance_agent(db_conn, machine_name=machine_identity.name()), uuid4()
    incarnation = await admit_hosted_runtime(
        aops_pool, agent, machine_identity.name(), owner, expected_from="idling", db=database
    )
    assert incarnation is not None
    assert await settle_hosted_runtime(aops_pool, incarnation, bus=event_bus, resources=None)
    message = insert_inbound_message(
        db_conn, agent, "pending work", "user", bus=event_bus, database=database
    )
    pause_owner.begin_maintenance("move", WHEN)
    hold = cohort.prepare(
        db_conn,
        machine=machine_identity.name(),
        host_owner=owner,
        holder="move",
        acquired_at=WHEN,
    )
    assert set(hold.commands) == {agent}
    assert (
        cohort.prepare(
            db_conn,
            machine=machine_identity.name(),
            host_owner=owner,
            holder="move",
            acquired_at=WHEN,
        )
        == hold
    )
    assert (
        await admit_hosted_runtime(
            aops_pool, agent, machine_identity.name(), uuid4(), expected_from="idling", db=database
        )
        is None
    )
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (message,)
    ).fetchone() == ("pending",)
