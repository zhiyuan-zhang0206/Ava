"""Hosted owner authority outlives turns but not explicit release/replacement."""

import asyncio
import subprocess
import sys
from uuid import uuid4

import psutil
import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

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
from base.config import settings
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


def _version(conn: psycopg.Connection, agent_id: int) -> int:
    row = conn.execute(
        "SELECT runtime_protocol_version FROM agents_meta WHERE id = %s", (agent_id,)
    ).fetchone()
    assert row is not None
    return int(row[0])


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
    # A legacy admission (no current publication) advertises protocol zero,
    # and settling neither invents nor clears an advertisement (task #4122).
    assert _version(db_conn, agent_id) == 0
    assert await settle_hosted_runtime(aops_pool, first, bus=event_bus, resources=None)
    assert _version(db_conn, agent_id) == 0
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
        # SIGKILL: SIGTERM=SIG_IGN is inherited from a shell session, so a
        # graceful terminate would never reap this look-alike host.
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
    """A concurrently held row lock on the deployment singleton does not stall a
    hosted birth: admission reads and locks nothing but the agent's own row, and
    advertises protocol zero."""
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
        assert _version(db_conn, agent_id) == 0
    finally:
        db_conn.rollback()


async def test_settle_retains_a_granted_advertisement(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
) -> None:
    """An ordinary settle keeps a protocol advertisement already granted (task #4122)."""
    agent_id, owner = _agent(db_conn), uuid4()
    admitted = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", owner, expected_from="idling", db=database
    )
    assert admitted is not None
    db_conn.execute(
        "UPDATE agents_meta SET runtime_protocol_version = 1 WHERE id = %s", (agent_id,)
    )
    db_conn.commit()
    assert await settle_hosted_runtime(aops_pool, admitted, bus=event_bus, resources=None)
    assert _version(db_conn, agent_id) == 1


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
        "SELECT lease_expires_at > now(), runtime_protocol_version FROM agents_meta WHERE id = %s",
        (agent_id,),
    ).fetchone() == (True, 0)


# ── corpse marker: stamp / settle / reap / renew ─────────────────────────────


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

    # First crash stamps, and the outcome names it a first death (no mark
    # existed), not a re-crash.
    first_stamp = await stamp_turn_fatal(aops_pool, incarnation)
    assert first_stamp.applied and not first_stamp.recrash
    first = _marker(db_conn, agent_id)
    assert first is not None

    # A later crash must NOT refresh the stamp (heartbeat crash-loops would
    # restart the reap grace forever). Capture the old value first: a
    # COALESCE -> now() regression would silently pass a `!= first` check
    # (both stamps read ~now), so the assertion must demand equality with the
    # pre-existing stamp. The outcome must also flag the re-crash — the
    # settle boundary's prompt-reap decision reads it (task #3616).
    _set_marker(db_conn, agent_id, minutes_ago=100)
    old_stamp = _marker(db_conn, agent_id)
    recrash = await stamp_turn_fatal(aops_pool, incarnation)
    assert recrash.applied and recrash.recrash
    assert _marker(db_conn, agent_id) == old_stamp

    # A settled (non-running) row is not stamped — the mark names the live
    # incarnation only.
    assert await settle_hosted_runtime(aops_pool, incarnation, bus=event_bus, resources=None)
    assert not (await stamp_turn_fatal(aops_pool, incarnation)).applied

    # A foreign incarnation's stamp is a no-op.
    foreign = RuntimeIncarnation(agent_id, uuid4(), owner)
    assert not (await stamp_turn_fatal(aops_pool, foreign)).applied


async def test_settle_never_touches_the_corpse_marker(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
) -> None:
    """Settlement only writes the idling flip. The marker lifecycle belongs
    elsewhere (stamp at crash, clear on a completed LLM turn / resurrect), so
    a no-work park settle can never relabel a crash-dead row healthy and
    resume its lease renewal forever (the 5858 escape)."""
    agent_id, owner = _agent(db_conn), uuid4()
    incarnation = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", owner, expected_from="idling", db=database
    )
    assert incarnation is not None

    _set_marker(db_conn, agent_id, minutes_ago=30)
    assert await settle_hosted_runtime(aops_pool, incarnation, bus=event_bus, resources=None)
    assert _marker(db_conn, agent_id) is not None

    # A markerless row stays markerless — settle invents nothing.
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

    monkeypatch.setattr("agent.ownership.corpse_reap.record_audit_async", _event)
    published: list[int] = []

    async def _publish(_bus: object, agent_id: int) -> None:
        published.append(agent_id)

    monkeypatch.setattr("agent.ownership.corpse_reap.publish_agent_updated", _publish)
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
    # A corpse left by a predecessor host (fresh owner UUID after a restart)
    # is ownerless with an expired lease: the lease-qualified scope must
    # reap it too, or it hangs offline forever.
    abandoned_corpse = await _row(minutes_ago=16, row_owner=None)
    db_conn.execute(
        "UPDATE agents_meta SET lease_expires_at = NULL WHERE id = %s", (abandoned_corpse,)
    )
    db_conn.commit()

    published.clear()  # settle publishes on every flip; keep only reap's
    reaped = await reap_crash_corpses(
        aops_pool,
        "host-test",
        owner,
        bus=event_bus,
        wake_enabled=lambda: settings.daemon.hosted_crash_recovery_wake_enabled,
    )
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
    # A second pass finds nothing new (the corpses are terminated).
    assert (
        await reap_crash_corpses(
            aops_pool,
            "host-test",
            owner,
            bus=event_bus,
            wake_enabled=lambda: settings.daemon.hosted_crash_recovery_wake_enabled,
        )
        == []
    )


async def test_crash_pipeline_marker_survives_settle_and_reaper_terminates(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
) -> None:
    """The 5858 flow end-to-end at the ownership layer: crash stamps while
    running, the idling settle keeps the stamp, renew stops renewing the
    corpse, and the reaper terminates it once the grace window elapses."""
    agent_id, owner = _agent(db_conn), uuid4()
    incarnation = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", owner, expected_from="idling", db=database
    )
    assert incarnation is not None

    # Crash while running: stamp first (CAS on running), then settle.
    assert await stamp_turn_fatal(aops_pool, incarnation)
    assert await settle_hosted_runtime(aops_pool, incarnation, bus=event_bus, resources=None)
    assert _marker(db_conn, agent_id) is not None

    # The beat renews healthy rows only — the corpse's lease stays expired.
    db_conn.execute("UPDATE agents_meta SET lease_expires_at = NULL WHERE id = %s", (agent_id,))
    db_conn.commit()
    await renew_hosted_owner(aops_pool, "host-test", owner)
    assert db_conn.execute(
        "SELECT lease_expires_at FROM agents_meta WHERE id = %s", (agent_id,)
    ).fetchone() == (None,)

    # Within the grace window the row is dead-but-waiting, not terminated.
    assert (
        await reap_crash_corpses(
            aops_pool,
            "host-test",
            owner,
            bus=event_bus,
            wake_enabled=lambda: settings.daemon.hosted_crash_recovery_wake_enabled,
        )
        == []
    )

    # Past the grace window the reaper terminates it with the reaper stamp.
    _set_marker(db_conn, agent_id, minutes_ago=16)
    reaped = await reap_crash_corpses(
        aops_pool,
        "host-test",
        owner,
        bus=event_bus,
        wake_enabled=lambda: settings.daemon.hosted_crash_recovery_wake_enabled,
    )
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


async def test_recovery_wake_gate_reads_two_independent_live_config_owners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typing import Any, cast
    from unittest.mock import MagicMock

    from agent.ownership import corpse_reap
    from base.config import ConfigBoot

    first, second = ConfigBoot(), ConfigBoot()
    first.set_field("hosted_crash_recovery_wake_enabled", False)
    second.set_field("hosted_crash_recovery_wake_enabled", True)
    conn = cast(psycopg.AsyncConnection[Any], MagicMock())
    queued: list[int] = []

    async def queue(actual: psycopg.AsyncConnection[Any], agent_id: int) -> int:
        assert actual is conn
        queued.append(agent_id)
        return agent_id + 100

    monkeypatch.setattr(corpse_reap, "_queue_recovery_wake", queue)
    assert (
        await corpse_reap._queue_wake_if_enabled(
            conn, 1, wake_enabled=lambda: first.view.daemon.hosted_crash_recovery_wake_enabled
        )
        is None
    )
    assert (
        await corpse_reap._queue_wake_if_enabled(
            conn, 2, wake_enabled=lambda: second.view.daemon.hosted_crash_recovery_wake_enabled
        )
        == 102
    )
    first.set_field("hosted_crash_recovery_wake_enabled", True)
    second.set_field("hosted_crash_recovery_wake_enabled", False)
    assert (
        await corpse_reap._queue_wake_if_enabled(
            conn, 1, wake_enabled=lambda: first.view.daemon.hosted_crash_recovery_wake_enabled
        )
        == 101
    )
    assert (
        await corpse_reap._queue_wake_if_enabled(
            conn, 2, wake_enabled=lambda: second.view.daemon.hosted_crash_recovery_wake_enabled
        )
        is None
    )
    assert queued == [2, 1]
