"""Hosted ownership as the agent layer observes it: status events, restart release, the lifecycle advertisement and the owner beat."""

from unittest.mock import ANY, AsyncMock
from uuid import UUID, uuid4

import psutil
import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.db import claim_inbound_batch
from agent.impersonation import native_status
from agent.ownership.hosted import (
    admit_hosted_runtime,
    apply_hosted_lifecycle,
    renew_hosted_owner,
    settle_hosted_runtime,
)
from base.agents.impersonation import ImpersonationError
from base.agents.incarnation.resources import IncarnationResources, ResourceProcess
from base.db import Database, create_agent, insert_inbound_message
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import bind_turn_identity


def _agent(conn: psycopg.Connection) -> int:
    agent_id = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta (id, status, machine) VALUES (%s, 'idling', 'host-test') "
        "ON CONFLICT (id) DO UPDATE SET status = 'idling', machine = 'host-test'",
        (agent_id,),
    )
    conn.commit()
    return agent_id


def _version(conn: psycopg.Connection, agent_id: int) -> int:
    row = conn.execute(
        "SELECT runtime_protocol_version FROM agents_meta WHERE id = %s", (agent_id,)
    ).fetchone()
    assert row is not None
    return int(row[0])


def _seed_managed_row(conn: psycopg.Connection, agent_id: int, owner: UUID) -> RuntimeIncarnation:
    """A row with stored resource evidence, as a managed-writer birth leaves it."""
    generation = uuid4()
    native = psutil.Process()
    evidence = IncarnationResources(
        generation=generation,
        owner=owner,
        host_process=ResourceProcess.capture(native),
        requests={},
    )
    conn.execute(
        "UPDATE agents_meta SET status='idling', runtime_kind='hosted', runtime_generation=%s, "
        "runtime_owner=%s, incarnation_resources=%s, "
        "lease_expires_at=clock_timestamp()+interval '1 minute' WHERE id=%s",
        (generation, owner, Jsonb(evidence.model_dump(mode="json")), agent_id),
    )
    conn.commit()
    return RuntimeIncarnation(agent_id, generation, owner)


async def test_hosted_status_changes_publish_agent_updated(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
) -> None:
    publish = AsyncMock()
    monkeypatch.setattr("agent.ownership.hosted.publish_agent_updated", publish)
    agent_id, owner = _agent(db_conn), uuid4()

    incarnation = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", owner, expected_from="idling", db=database
    )
    assert incarnation is not None
    # #1687 moved admission's live announce into the bounded _run_turn
    # settlement boundary, so admit_hosted_runtime itself no longer publishes.
    publish.assert_not_awaited()

    publish.reset_mock()
    assert await settle_hosted_runtime(aops_pool, incarnation, bus=event_bus)
    publish.assert_awaited_once_with(ANY, agent_id)

    incarnation = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", owner, expected_from="idling", db=database
    )
    assert incarnation is not None
    insert_inbound_message(
        db_conn, agent_id, "", "user", "terminate", bus=event_bus, database=database
    )
    publish.reset_mock()
    with bind_turn_identity(agent_id, incarnation=incarnation):
        await claim_inbound_batch(aops_pool, agent_id)
        assert await apply_hosted_lifecycle(aops_pool, incarnation, bus=event_bus) == "terminate"
    publish.assert_awaited_once_with(ANY, agent_id)


async def test_hosted_restart_releases_before_new_incarnation(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
) -> None:
    agent_id, owner = _agent(db_conn), uuid4()
    first = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", owner, expected_from="idling", db=database
    )
    assert first is not None
    insert_inbound_message(
        db_conn, agent_id, "", "user", "restart", bus=event_bus, database=database
    )
    with bind_turn_identity(agent_id, incarnation=first):
        await claim_inbound_batch(aops_pool, agent_id)
        assert await apply_hosted_lifecycle(aops_pool, first, bus=event_bus) == "restart"
    second = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", owner, expected_from="idling", db=database
    )
    assert second is not None and second.generation != first.generation
    assert not await settle_hosted_runtime(aops_pool, first, bus=event_bus)


@pytest.mark.parametrize("command", ["restart", "terminate"])
async def test_lifecycle_apply_releases_the_advertisement(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    command: str,
    database: Database,
    event_bus: EventBus,
) -> None:
    """A durable lifecycle apply zeroes a granted advertisement; only settle retains it."""
    agent_id, owner = _agent(db_conn), uuid4()
    seeded = _seed_managed_row(db_conn, agent_id, owner)
    first = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", owner, expected_from="idling", db=database
    )
    assert first is not None and first == seeded
    db_conn.execute(
        "UPDATE agents_meta SET runtime_protocol_version = 1 WHERE id = %s", (agent_id,)
    )
    db_conn.commit()
    insert_inbound_message(db_conn, agent_id, "", "user", command, bus=event_bus, database=database)
    with bind_turn_identity(agent_id, incarnation=first):
        await claim_inbound_batch(aops_pool, agent_id)
        assert await apply_hosted_lifecycle(aops_pool, first, bus=event_bus) == command
    assert _version(db_conn, agent_id) == 0


async def test_owner_beat_renews_a_mid_turn_row_and_the_guard_stays_green(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
) -> None:
    """A turn's length never expires its lease: the beat renews, not the turn.

    A turn may run for hours with the row 'running' (agent 2697, 2026-09-20:
    ~375k tokens in flight); the host beat renews the lease the whole time, so
    the fail-closed guard read stays green however long the turn takes. A
    genuinely lapsed lease still refuses until a beat lands.
    """
    agent_id, owner = _agent(db_conn), uuid4()
    incarnation = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", owner, expected_from="idling", db=database
    )
    assert incarnation is not None
    # Mid-turn, with a stand-in for any renewal silence longer than the TTL.
    db_conn.execute(
        "UPDATE agents_meta SET lease_expires_at = now() - interval '5 minutes' WHERE id = %s",
        (agent_id,),
    )
    db_conn.commit()
    with bind_turn_identity(agent_id, incarnation=incarnation), pytest.raises(ImpersonationError):
        await native_status(database, event_bus, agent_id)
    await renew_hosted_owner(aops_pool, "host-test", owner)  # one beat
    assert db_conn.execute(
        "SELECT lease_expires_at > now() FROM agents_meta WHERE id = %s", (agent_id,)
    ).fetchone() == (True,)
    with bind_turn_identity(agent_id, incarnation=incarnation):
        assert await native_status(database, event_bus, agent_id) is None
