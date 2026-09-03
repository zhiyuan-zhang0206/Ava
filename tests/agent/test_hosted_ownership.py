"""Hosted owner authority outlives turns but not explicit release/replacement."""

from unittest.mock import Mock
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.hosted_ownership import admit_hosted_runtime, renew_hosted_owner, settle_hosted_runtime
from shared.db import create_agent
from shared.runtime_incarnation import RuntimeIncarnation, current_incarnation
from shared.turn_identity import bind_turn_identity


def _agent(conn: psycopg.Connection) -> int:
    agent_id = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta (id, status, machine) VALUES (%s, 'idling', 'host-test') "
        "ON CONFLICT (id) DO UPDATE SET status = 'idling', machine = 'host-test'",
        (agent_id,),
    )
    conn.commit()
    return agent_id


async def test_hosted_incarnation_survives_idle_and_next_turn(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, owner = _agent(db_conn), uuid4()
    first = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", owner, expected_from="idling"
    )
    assert first is not None
    assert await settle_hosted_runtime(aops_pool, first)
    second = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", owner, expected_from="idling"
    )
    assert second == first


async def test_hosted_restart_releases_before_new_incarnation(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, owner = _agent(db_conn), uuid4()
    first = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", owner, expected_from="idling"
    )
    assert first is not None
    assert await settle_hosted_runtime(aops_pool, first, release=True)
    second = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", owner, expected_from="idling"
    )
    assert second is not None and second.generation != first.generation
    assert not await settle_hosted_runtime(aops_pool, first)


@pytest.mark.parametrize("status", ["running", "idling"])
async def test_live_other_host_owner_cannot_be_admitted(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    status: str,
) -> None:
    agent_id = _agent(db_conn)
    first = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", uuid4(), expected_from="idling"
    )
    assert first is not None
    if status == "idling":
        assert await settle_hosted_runtime(aops_pool, first)
    assert (
        await admit_hosted_runtime(aops_pool, agent_id, "host-test", uuid4(), expected_from=status)
        is None
    )


async def test_expired_owner_replacement_fences_old_settlement(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id = _agent(db_conn)
    old = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", uuid4(), expected_from="idling"
    )
    assert old is not None
    db_conn.execute("UPDATE agents_meta SET lease_expires_at = NULL WHERE id = %s", (agent_id,))
    db_conn.commit()
    new = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", uuid4(), expected_from="running"
    )
    assert new is not None and new.generation != old.generation
    assert not await settle_hosted_runtime(aops_pool, old)


async def test_owner_beat_renews_idle_but_not_other_owner(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, owner = _agent(db_conn), uuid4()
    incarnation = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", owner, expected_from="idling"
    )
    assert incarnation is not None
    assert await settle_hosted_runtime(aops_pool, incarnation)
    db_conn.execute("UPDATE agents_meta SET lease_expires_at = NULL WHERE id = %s", (agent_id,))
    db_conn.commit()
    await renew_hosted_owner(aops_pool, "host-test", uuid4())
    assert db_conn.execute(
        "SELECT lease_expires_at FROM agents_meta WHERE id = %s", (agent_id,)
    ).fetchone() == (None,)
    db_conn.commit()
    await renew_hosted_owner(aops_pool, "host-test", owner)
    assert db_conn.execute(
        "SELECT lease_expires_at > now(), runtime_protocol_version FROM agents_meta WHERE id = %s",
        (agent_id,),
    ).fetchone() == (True, 0)


async def test_hosted_incarnation_context_is_task_local_and_copies_to_thread() -> None:
    import asyncio

    async def read(agent_id: int) -> RuntimeIncarnation:
        original = RuntimeIncarnation(agent_id, uuid4(), uuid4())
        with bind_turn_identity(agent_id, incarnation=original):
            await asyncio.sleep(0)
            assert await asyncio.to_thread(current_incarnation, agent_id) == original
            return original

    first, second = await asyncio.gather(read(1), read(2))
    assert first != second
    assert current_incarnation(1) is None


@pytest.mark.parametrize("status", ["running", "idling"])
async def test_host_refuses_a_turn_owned_by_another_live_instance(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    from services.agent_host.host import AgentHost

    agent_id = _agent(db_conn)
    original = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", uuid4(), expected_from="idling"
    )
    assert original is not None
    if status == "idling":
        assert await settle_hosted_runtime(aops_pool, original)
    host = AgentHost(pool=aops_pool, checkpointer=Mock(), graph=Mock(), machine="host-test")

    async def forbidden_runtime(_agent_id: int, _fingerprint: str) -> None:
        raise AssertionError("a live other owner must prevent all runtime work")

    monkeypatch.setattr(host, "_runtime_for", forbidden_runtime)
    await host.run_turn(agent_id)
