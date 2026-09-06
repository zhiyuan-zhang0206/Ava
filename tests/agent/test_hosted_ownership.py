"""Hosted owner authority outlives turns but not explicit release/replacement."""

import asyncio
import subprocess
import sys
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import psutil
import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from agent.db import claim_inbound_batch
from agent.hosted_ownership import (
    admit_hosted_runtime,
    apply_hosted_lifecycle,
    release_hosted_owner,
    renew_hosted_owner,
    settle_hosted_runtime,
)
from shared.config import settings
from shared.db import PG_KEEPALIVE_KWARGS, create_agent, insert_inbound_message
from shared.incarnation_resources import IncarnationResources, ResourceProcess, decode_resources
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


async def test_hosted_status_changes_publish_agent_updated(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish = AsyncMock()
    monkeypatch.setattr("agent.hosted_ownership.publish_agent_updated", publish)
    agent_id, owner = _agent(db_conn), uuid4()

    incarnation = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", owner, expected_from="idling"
    )
    assert incarnation is not None
    # #1687 moved admission's live announce into the bounded _run_turn
    # settlement boundary, so admit_hosted_runtime itself no longer publishes.
    publish.assert_not_awaited()

    publish.reset_mock()
    assert await settle_hosted_runtime(aops_pool, incarnation)
    publish.assert_awaited_once_with(aops_pool, agent_id)

    incarnation = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", owner, expected_from="idling"
    )
    assert incarnation is not None
    insert_inbound_message(db_conn, agent_id, "", "user", "terminate")
    publish.reset_mock()
    with bind_turn_identity(agent_id, incarnation=incarnation):
        await claim_inbound_batch(aops_pool, agent_id)
        assert await apply_hosted_lifecycle(aops_pool, incarnation) == "terminate"
    publish.assert_awaited_once_with(aops_pool, agent_id)


async def test_cancel_during_live_announce_settles_the_committed_admission(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The optional Redis announce is downstream of the durable status flip.

    Model the exact half-open #5740 boundary: admission committed ``running``,
    then PUBLISH parked before the graph could claim its pending inbound. A
    work-task cancellation at that await must still settle the same incarnation
    to ``idling``; its live host lease must not preserve a false running row.
    """
    from services.agent_host.host import AgentHost

    agent_id = _agent(db_conn)
    announce_entered = asyncio.Event()
    announce_release = asyncio.Event()
    publish_calls = 0

    async def half_open_publish(_pool: object, published_agent_id: int) -> None:
        nonlocal publish_calls
        assert published_agent_id == agent_id
        publish_calls += 1
        if publish_calls == 1:
            announce_entered.set()
            await announce_release.wait()

    monkeypatch.setattr("agent.hosted_ownership.publish_agent_updated", half_open_publish)
    monkeypatch.setattr(
        "services.agent_host.host.publish_agent_updated", half_open_publish, raising=False
    )

    def allow_model_config(*, model: str | None = None) -> None:
        assert model is not None

    monkeypatch.setattr("services.agent_host.host.validate_model_config", allow_model_config)

    host = AgentHost(
        pool=aops_pool,
        control_pool=aops_pool,
        checkpointer=Mock(),
        graph=Mock(),
        machine="host-test",
    )
    # Exercise the owned work task itself. ``run_turn`` deliberately shields
    # this inner task from scheduler cancellation; injecting cancellation at
    # the exact inner boundary proves that boundary is independently clean.
    task = asyncio.create_task(host._run_turn(agent_id))
    await asyncio.wait_for(announce_entered.wait(), timeout=2.0)
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("running",)

    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=1.0)
    cancellation_settled = bool(done)
    if not cancellation_settled:
        announce_release.set()
        await asyncio.gather(task, return_exceptions=True)

    assert cancellation_settled, "announcement cancellation did not finish settlement"
    assert task.cancelled()
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("idling",)
    assert publish_calls == 2, "settlement must announce after restoring durable status"
    assert agent_id not in host._in_flight


async def test_hosted_restart_releases_before_new_incarnation(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, owner = _agent(db_conn), uuid4()
    first = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", owner, expected_from="idling"
    )
    assert first is not None
    insert_inbound_message(db_conn, agent_id, "", "user", "restart")
    with bind_turn_identity(agent_id, incarnation=first):
        await claim_inbound_batch(aops_pool, agent_id)
        assert await apply_hosted_lifecycle(aops_pool, first) == "restart"
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


async def test_new_host_owner_requires_exact_old_host_exit_for_managed_set(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
) -> None:
    """A normal agent-host restart transfers only an empty set whose host died."""
    agent_id = _agent(db_conn)
    old = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    old_host = subprocess.Popen(
        [sys.executable, "-I", "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        native = psutil.Process(old_host.pid)
        evidence = IncarnationResources(
            generation=old.generation,
            owner=old.owner,
            host_process=ResourceProcess(pid=native.pid, birth=native.create_time()),
            requests={},
        )
        db_conn.execute(
            "UPDATE agents_meta SET status='idling',runtime_generation=%s,runtime_owner=%s,"
            "runtime_kind='hosted',lease_expires_at=clock_timestamp()+interval '1 minute',"
            "incarnation_resources=%s WHERE id=%s",
            (old.generation, old.owner, Jsonb(evidence.model_dump(mode="json")), agent_id),
        )
        db_conn.commit()
        await release_hosted_owner(aops_pool, "host-test", old.owner, set())

        assert (
            await admit_hosted_runtime(
                aops_pool, agent_id, "host-test", uuid4(), expected_from="idling"
            )
            is None
        )
        old_host.terminate()
        old_host.wait(timeout=5)

        successor = await admit_hosted_runtime(
            aops_pool, agent_id, "host-test", uuid4(), expected_from="idling"
        )
        assert successor is not None and successor.generation != old.generation
        stored = db_conn.execute(
            "SELECT incarnation_resources FROM agents_meta WHERE id=%s", (agent_id,)
        ).fetchone()
        assert stored is not None
        transferred = decode_resources(stored[0])
        assert isinstance(transferred, IncarnationResources)
        assert (transferred.generation, transferred.owner) == (
            successor.generation,
            successor.owner,
        )
        assert transferred.requests == {}
        assert transferred.host_process is not None
        assert transferred.host_process.pid == psutil.Process().pid
    finally:
        if old_host.poll() is None:
            old_host.kill()
            old_host.wait(timeout=5)


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


async def test_hosted_exit_matches_generation_and_owner(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
) -> None:
    import asyncio

    from ops.ops_exit import _mark_exited_blocking

    agent_id = _agent(db_conn)
    incarnation = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", uuid4(), expected_from="idling"
    )
    assert incarnation is not None
    with ConnectionPool[psycopg.Connection](
        settings.data_plane.db_url, min_size=1, max_size=2, kwargs=PG_KEEPALIVE_KWARGS
    ) as pool:
        stale = await asyncio.to_thread(
            _mark_exited_blocking, agent_id, pool, uuid4(), incarnation.owner
        )
        assert stale[0] == 0
        current = await asyncio.to_thread(
            _mark_exited_blocking, agent_id, pool, incarnation.generation, incarnation.owner
        )
        assert current[0] == 1


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
