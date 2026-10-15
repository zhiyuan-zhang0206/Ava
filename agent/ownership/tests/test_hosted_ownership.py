import asyncio
import subprocess
import sys
from uuid import uuid4

import psutil
import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.ownership import corpse_reap
from agent.ownership.corpse_reap import reap_crash_corpses
from agent.ownership.hosted import (
    admit_hosted_runtime,
    release_hosted_owner,
    renew_hosted_owner,
    settle_hosted_runtime,
    stamp_turn_fatal,
)
from base.agents.incarnation.resources import (
    IncarnationResources,
    ResourceProcess,
    decode_resources,
)
from base.db import Database, create_agent
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.telemetry import Event


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
    database: Database,
    event_bus: EventBus,
) -> None:
    agent_id, owner = _agent(db_conn), uuid4()
    first = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", owner, expected_from="idling", db=database
    )
    assert first is not None
    assert await settle_hosted_runtime(aops_pool, first, bus=event_bus, resources=None)
    second = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", owner, expected_from="idling", db=database
    )
    assert second == first


@pytest.mark.parametrize("status", ["running", "idling"])
async def test_live_other_host_owner_cannot_be_admitted(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    status: str,
    database: Database,
    event_bus: EventBus,
) -> None:
    agent_id = _agent(db_conn)
    first = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", uuid4(), expected_from="idling", db=database
    )
    assert first is not None
    if status == "idling":
        assert await settle_hosted_runtime(aops_pool, first, bus=event_bus, resources=None)
    assert (
        await admit_hosted_runtime(
            aops_pool, agent_id, "host-test", uuid4(), expected_from=status, db=database
        )
        is None
    )


async def test_expired_owner_replacement_fences_old_settlement(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
) -> None:
    agent_id = _agent(db_conn)
    old = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", uuid4(), expected_from="idling", db=database
    )
    assert old is not None
    db_conn.execute("UPDATE agents_meta SET lease_expires_at = NULL WHERE id = %s", (agent_id,))
    db_conn.commit()
    new = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", uuid4(), expected_from="running", db=database
    )
    assert new is not None and new.generation != old.generation
    assert not await settle_hosted_runtime(aops_pool, old, bus=event_bus, resources=None)


async def test_legacy_null_resource_row_waits_for_lease_expiry(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
) -> None:
    agent_id = _agent(db_conn)
    old = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", uuid4(), expected_from="idling", db=database
    )
    assert old is not None
    assert db_conn.execute(
        "SELECT incarnation_resources IS NULL FROM agents_meta WHERE id = %s", (agent_id,)
    ).fetchone() == (True,)
    db_conn.execute(
        "UPDATE agents_meta SET lease_expires_at = now() + interval '5 minutes' WHERE id = %s",
        (agent_id,),
    )
    db_conn.commit()

    assert (
        await admit_hosted_runtime(
            aops_pool, agent_id, "host-test", uuid4(), expected_from="running", db=database
        )
        is None
    )

    db_conn.execute(
        "UPDATE agents_meta SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
        (agent_id,),
    )
    db_conn.commit()
    successor = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", uuid4(), expected_from="running", db=database
    )
    assert successor is not None and successor.generation != old.generation


@pytest.mark.parametrize("status", ["running", "idling"])
@pytest.mark.parametrize("release_lease", [False, True])
async def test_new_host_owner_requires_exact_old_host_exit_for_managed_set(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    status: str,
    release_lease: bool,
    database: Database,
    event_bus: EventBus,
) -> None:
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
            host_process=ResourceProcess.capture(native),
            requests={},
        )
        db_conn.execute(
            "UPDATE agents_meta SET status=%s,runtime_generation=%s,runtime_owner=%s,"
            "runtime_kind='hosted',lease_expires_at=clock_timestamp()+interval '1 minute',"
            "incarnation_resources=%s WHERE id=%s",
            (status, old.generation, old.owner, Jsonb(evidence.model_dump(mode="json")), agent_id),
        )
        db_conn.commit()
        if release_lease:
            await release_hosted_owner(aops_pool, "host-test", old.owner, set())

        assert (
            await admit_hosted_runtime(
                aops_pool,
                agent_id,
                "host-test",
                uuid4(),
                expected_from=status,
                db=database,
            )
            is None
        )
        old_host.kill()
        old_host.wait(timeout=5)

        successor = await admit_hosted_runtime(
            aops_pool,
            agent_id,
            "host-test",
            uuid4(),
            expected_from=status,
            db=database,
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
        assert not await settle_hosted_runtime(aops_pool, old, bus=event_bus, resources=None)
    finally:
        if old_host.poll() is None:
            old_host.kill()
            old_host.wait(timeout=5)


async def test_admission_does_not_lock_deployment_state(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
) -> None:
    agent_id, owner = _agent(db_conn), uuid4()
    try:
        db_conn.execute("SELECT id FROM deployment_state WHERE id = 1 FOR UPDATE")
        admitted = await asyncio.wait_for(
            admit_hosted_runtime(
                aops_pool, agent_id, "host-test", owner, expected_from="idling", db=database
            ),
            10,
        )
        assert admitted is not None
    finally:
        db_conn.rollback()


async def test_owner_beat_renews_idle_but_not_other_owner(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
) -> None:
    agent_id, owner = _agent(db_conn), uuid4()
    incarnation = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", owner, expected_from="idling", db=database
    )
    assert incarnation is not None
    assert await settle_hosted_runtime(aops_pool, incarnation, bus=event_bus, resources=None)
    db_conn.execute("UPDATE agents_meta SET lease_expires_at = NULL WHERE id = %s", (agent_id,))
    db_conn.commit()
    await renew_hosted_owner(aops_pool, "host-test", uuid4())
    assert db_conn.execute(
        "SELECT lease_expires_at FROM agents_meta WHERE id = %s", (agent_id,)
    ).fetchone() == (None,)
    db_conn.commit()
    await renew_hosted_owner(aops_pool, "host-test", owner)
    assert db_conn.execute(
        "SELECT lease_expires_at > now() FROM agents_meta WHERE id = %s", (agent_id,)
    ).fetchone() == (True,)


def _marker(db_conn: psycopg.Connection, agent_id: int) -> object:
    row = db_conn.execute(
        "SELECT last_turn_fatal_at FROM agents_meta WHERE id = %s", (agent_id,)
    ).fetchone()
    assert row is not None, f"agents_meta row {agent_id} missing"
    return row[0]


def _set_marker(
    db_conn: psycopg.Connection, agent_id: int, *, minutes_ago: int | None = None
) -> None:
    if minutes_ago is None:
        db_conn.execute(
            "UPDATE agents_meta SET last_turn_fatal_at = NULL WHERE id = %s", (agent_id,)
        )
    else:
        db_conn.execute(
            "UPDATE agents_meta SET last_turn_fatal_at = now() - make_interval(mins => %s) "
            "WHERE id = %s",
            (minutes_ago, agent_id),
        )
    db_conn.commit()


async def test_stamp_turn_fatal_is_monotonic_and_cas_guarded(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
) -> None:
    agent_id, owner = _agent(db_conn), uuid4()
    incarnation = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", owner, expected_from="idling", db=database
    )
    assert incarnation is not None

    assert (await stamp_turn_fatal(aops_pool, incarnation)).applied
    first = _marker(db_conn, agent_id)
    assert first is not None

    _set_marker(db_conn, agent_id, minutes_ago=100)
    old_stamp = _marker(db_conn, agent_id)
    assert (await stamp_turn_fatal(aops_pool, incarnation)).applied
    assert _marker(db_conn, agent_id) == old_stamp

    assert await settle_hosted_runtime(aops_pool, incarnation, bus=event_bus, resources=None)
    assert not (await stamp_turn_fatal(aops_pool, incarnation)).applied

    foreign = RuntimeIncarnation(agent_id, uuid4(), owner)
    assert not (await stamp_turn_fatal(aops_pool, foreign)).applied


async def test_settle_never_touches_the_corpse_marker(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
) -> None:
    agent_id, owner = _agent(db_conn), uuid4()
    incarnation = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", owner, expected_from="idling", db=database
    )
    assert incarnation is not None

    _set_marker(db_conn, agent_id, minutes_ago=30)
    assert await settle_hosted_runtime(aops_pool, incarnation, bus=event_bus, resources=None)
    assert _marker(db_conn, agent_id) is not None

    assert await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", owner, expected_from="idling", db=database
    )
    _set_marker(db_conn, agent_id, minutes_ago=None)
    assert await settle_hosted_runtime(aops_pool, incarnation, bus=event_bus, resources=None)
    assert _marker(db_conn, agent_id) is None


async def test_reap_crash_corpses_terminates_only_grace_elapsed_idling_corpses(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
) -> None:
    events: list[tuple[int, str, str]] = []

    async def _event(_conn: object, event: Event) -> Event:
        if event.attributes.get("reason") == "corpse_reaper":
            assert event.agent_id is not None
            events.append((event.agent_id, event.event_name, "corpse_reaper"))
        return event

    monkeypatch.setattr(corpse_reap, "record_audit_async", _event)
    published: list[int] = []

    async def _publish(_bus: object, agent_id: int) -> None:
        published.append(agent_id)

    monkeypatch.setattr(corpse_reap, "publish_agent_updated", _publish)
    owner = uuid4()

    async def _row(
        minutes_ago: int | None, *, status: str = "idling", row_owner: object = owner
    ) -> int:
        agent_id, _owner = _agent(db_conn), owner
        incarnation = await admit_hosted_runtime(
            aops_pool, agent_id, "host-test", _owner, expected_from="idling", db=database
        )
        assert incarnation is not None
        await settle_hosted_runtime(aops_pool, incarnation, bus=event_bus, resources=None)
        db_conn.execute(
            "UPDATE agents_meta SET status=%s, runtime_owner=%s WHERE id=%s",
            (status, row_owner, agent_id),
        )
        _set_marker(db_conn, agent_id, minutes_ago=minutes_ago)
        return agent_id

    past_grace = await _row(minutes_ago=16)
    within_grace = await _row(minutes_ago=5)
    healthy = await _row(minutes_ago=None)
    running_corpse = await _row(minutes_ago=16, status="running")
    foreign_corpse = await _row(minutes_ago=16, row_owner=None)
    abandoned_corpse = await _row(minutes_ago=16, row_owner=None)
    db_conn.execute(
        "UPDATE agents_meta SET lease_expires_at = NULL WHERE id = %s", (abandoned_corpse,)
    )
    db_conn.commit()

    published.clear()
    reaped = await reap_crash_corpses(aops_pool, "host-test", owner, bus=event_bus)
    assert sorted(corpse.agent_id for corpse in reaped) == sorted([past_grace, abandoned_corpse])

    for corpse in reaped:
        row = db_conn.execute(
            "SELECT status, termination_source, lease_expires_at FROM agents_meta WHERE id = %s",
            (corpse.agent_id,),
        ).fetchone()
        assert row is not None
        assert row[0] == "terminated" and row[1] == "reaper" and row[2] is None

    for survivor, expected in (
        (within_grace, "idling"),
        (healthy, "idling"),
        (running_corpse, "running"),
        (foreign_corpse, "idling"),
    ):
        assert db_conn.execute(
            "SELECT status FROM agents_meta WHERE id = %s", (survivor,)
        ).fetchone() == (expected,)

    assert sorted(events) == sorted(
        [
            (past_grace, "status_change", "corpse_reaper"),
            (abandoned_corpse, "status_change", "corpse_reaper"),
        ]
    )
    assert sorted(published) == sorted([past_grace, abandoned_corpse])
    assert await reap_crash_corpses(aops_pool, "host-test", owner, bus=event_bus) == []


async def test_crash_pipeline_marker_survives_settle_and_reaper_terminates(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
) -> None:
    agent_id, owner = _agent(db_conn), uuid4()
    incarnation = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", owner, expected_from="idling", db=database
    )
    assert incarnation is not None

    assert await stamp_turn_fatal(aops_pool, incarnation)
    assert await settle_hosted_runtime(aops_pool, incarnation, bus=event_bus, resources=None)
    assert _marker(db_conn, agent_id) is not None

    db_conn.execute("UPDATE agents_meta SET lease_expires_at = NULL WHERE id = %s", (agent_id,))
    db_conn.commit()
    await renew_hosted_owner(aops_pool, "host-test", owner)
    assert db_conn.execute(
        "SELECT lease_expires_at FROM agents_meta WHERE id = %s", (agent_id,)
    ).fetchone() == (None,)

    assert await reap_crash_corpses(aops_pool, "host-test", owner, bus=event_bus) == []

    _set_marker(db_conn, agent_id, minutes_ago=16)
    reaped = await reap_crash_corpses(aops_pool, "host-test", owner, bus=event_bus)
    assert [corpse.agent_id for corpse in reaped] == [agent_id]
    row = db_conn.execute(
        "SELECT status, termination_source FROM agents_meta WHERE id = %s", (agent_id,)
    ).fetchone()
    assert row is not None
    assert row[0] == "terminated" and row[1] == "reaper"


async def test_renew_hosted_owner_skips_crash_marked_rows(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
) -> None:
    owner = uuid4()

    async def _row(marked: bool) -> int:
        agent_id, _owner = _agent(db_conn), owner
        incarnation = await admit_hosted_runtime(
            aops_pool, agent_id, "host-test", _owner, expected_from="idling", db=database
        )
        assert incarnation is not None
        await settle_hosted_runtime(aops_pool, incarnation, bus=event_bus, resources=None)
        if marked:
            _set_marker(db_conn, agent_id, minutes_ago=0)
        db_conn.execute("UPDATE agents_meta SET lease_expires_at = NULL WHERE id = %s", (agent_id,))
        db_conn.commit()
        return agent_id

    corpse = await _row(marked=True)
    live = await _row(marked=False)

    await renew_hosted_owner(aops_pool, "host-test", owner)

    assert db_conn.execute(
        "SELECT lease_expires_at FROM agents_meta WHERE id = %s", (corpse,)
    ).fetchone() == (None,)
    assert db_conn.execute(
        "SELECT lease_expires_at > now() FROM agents_meta WHERE id = %s", (live,)
    ).fetchone() == (True,)
