import asyncio
import time
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.ownership.hosted import admit_hosted_runtime, settle_hosted_runtime
from base.agents.inbox.insert import insert_inbound_message
from base.cluster.machine import MachineIdentity
from base.config import settings
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.deploy.maintenance import admission, cohort, pause_owner
from base.deploy.maintenance.state import MaintenanceHold, MaintenancePhase
from base.deploy.tests.maintenance_factory import WHEN, maintenance_agent
from base.deploy.tests.maintenance_factory import isolate as isolate
from base.events.live.bus import EventBus
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from ops import agent_pause
from ops.agent_pause.probe import HostIdentity


def _as_live_host(
    monkeypatch: pytest.MonkeyPatch, owner: UUID, *, machine_identity: MachineIdentity
) -> None:
    machine_identity.set(role="agent-runner")
    monkeypatch.setattr(agent_pause, "host_running", lambda: True)
    monkeypatch.setattr(agent_pause, "host_identity", lambda: HostIdentity(owner, frozenset()))
    monkeypatch.setattr(agent_pause, "publish_inbound_wake", MagicMock())


def _poll_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    real_sleep = time.sleep

    def short_sleep(seconds: float) -> None:
        real_sleep(min(seconds, 0.05))

    monkeypatch.setattr(time, "sleep", short_sleep)


def _claim(conn: psycopg.Connection[Any], message: int) -> None:
    conn.execute(
        "UPDATE inbound_messages SET status='claimed', claimed_at=clock_timestamp() WHERE id=%s",
        (message,),
    )


def _resolve_lifecycle_command(conn: psycopg.Connection[Any], command: int) -> None:
    conn.execute(
        "UPDATE inbound_messages SET status='done', claimed_at=COALESCE(claimed_at, "
        "clock_timestamp()), target_generation=%s, target_owner=%s, applied_at=clock_timestamp(), "
        "observed_at=clock_timestamp() WHERE id=%s",
        (uuid4(), uuid4(), command),
    )


async def _live_member(
    conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    owner: UUID,
    database_gate: ProcessDbGate,
    *,
    machine_identity: MachineIdentity,
) -> int:
    agent = maintenance_agent(conn, machine_name=machine_identity.name())
    incarnation = await admit_hosted_runtime(
        aops_pool,
        agent,
        machine_identity.name(),
        owner,
        expected_from="idling",
        db=Database.from_settings(gate=database_gate),
    )
    assert incarnation is not None
    assert await settle_hosted_runtime(
        aops_pool, incarnation, bus=EventBus.from_settings(), resources=None
    )
    return agent


async def test_member_collision_is_waitable_and_freezes_nothing(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    database_gate: ProcessDbGate,
    *,
    machine_identity: MachineIdentity,
) -> None:
    owner = uuid4()
    agent = await _live_member(
        db_conn, aops_pool, owner, database_gate=database_gate, machine_identity=machine_identity
    )
    other = await _live_member(
        db_conn, aops_pool, owner, database_gate=database_gate, machine_identity=machine_identity
    )
    command = insert_inbound_message(
        db_conn, agent, "", "agent:6090", kind="terminate", bus=event_bus, database=database
    )
    db_conn.commit()
    pause_owner.begin_maintenance("move", WHEN)

    with pytest.raises(cohort.LifecycleCollisionError) as raised:
        cohort.prepare(
            db_conn,
            machine=machine_identity.name(),
            host_owner=owner,
            holder="move",
            acquired_at=WHEN,
        )
    assert raised.value.waitable
    assert raised.value.agent_ids == (agent,)

    hold = admission.require_operation("move", WHEN).maintenance
    assert hold is not None and hold.phase == MaintenancePhase.PREPARING and hold.commands == {}

    _resolve_lifecycle_command(db_conn, command)
    db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (agent,))
    db_conn.commit()
    hold = cohort.prepare(
        db_conn,
        machine=machine_identity.name(),
        host_owner=owner,
        holder="move",
        acquired_at=WHEN,
    )
    assert hold.phase == MaintenancePhase.DRAINING
    assert set(hold.commands) == {other}


async def test_prepare_waits_for_resolving_command_then_proceeds(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    database_gate: ProcessDbGate,
    *,
    machine_identity: MachineIdentity,
    emitted_events: EmittedEvents,
) -> None:
    owner = uuid4()
    agent = await _live_member(
        db_conn, aops_pool, owner, database_gate=database_gate, machine_identity=machine_identity
    )
    chat = insert_inbound_message(
        db_conn, agent, "in flight", "user", bus=event_bus, database=database
    )
    _claim(db_conn, chat)
    command = insert_inbound_message(
        db_conn, agent, "", "agent:6090", kind="terminate", bus=event_bus, database=database
    )
    db_conn.commit()
    _as_live_host(monkeypatch, owner, machine_identity=machine_identity)
    monkeypatch.setattr(settings.gateway, "pause_lifecycle_wait_seconds", 5.0)
    _poll_fast(monkeypatch)
    emitted_events.clear()

    real_prepare = cohort.prepare
    resolved: list[bool] = []

    def resolving_prepare(*args: Any, **kwargs: Any) -> cohort.MaintenanceHold:
        try:
            return real_prepare(*args, **kwargs)
        except cohort.LifecycleCollisionError:
            if not resolved:
                resolved.append(True)
                _resolve_lifecycle_command(db_conn, command)
                db_conn.commit()
            raise

    monkeypatch.setattr(cohort, "prepare", resolving_prepare)
    await asyncio.to_thread(
        agent_pause.prepare, database, event_bus, "move", WHEN, local_machine=machine_identity
    )

    hold = agent_pause.require_maintenance_hold("move", WHEN)
    assert hold.phase == MaintenancePhase.DRAINING
    assert set(hold.commands) == {agent}
    assert emitted_events.names() == ["pause_lifecycle_wait"]
    [event] = emitted_events
    attributes = event.attributes
    assert attributes["outcome"] == "resolved"
    assert attributes["agents"] == [agent]
    assert 0 < attributes["waited_s"] < 5.0
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (chat,)
    ).fetchone() == ("claimed",)


async def test_prepare_aborts_when_collision_outlives_the_bound(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    database_gate: ProcessDbGate,
    *,
    machine_identity: MachineIdentity,
    emitted_events: EmittedEvents,
) -> None:
    owner = uuid4()
    agent = await _live_member(
        db_conn, aops_pool, owner, database_gate=database_gate, machine_identity=machine_identity
    )
    insert_inbound_message(
        db_conn, agent, "", "agent:6090", kind="terminate", bus=event_bus, database=database
    )
    db_conn.commit()
    _as_live_host(monkeypatch, owner, machine_identity=machine_identity)
    monkeypatch.setattr(settings.gateway, "pause_lifecycle_wait_seconds", 0.3)
    _poll_fast(monkeypatch)
    emitted_events.clear()

    with pytest.raises(RuntimeError, match=r"waited .*still unfinished after the") as raised:
        agent_pause.prepare(database, event_bus, "move", WHEN, local_machine=machine_identity)
    assert not isinstance(raised.value, cohort.LifecycleCollisionError)
    assert "unfinished lifecycle command" in str(raised.value)

    hold = agent_pause.require_maintenance_hold("move", WHEN)
    assert hold.phase == MaintenancePhase.PREPARING and hold.commands == {}
    assert [event.attributes["outcome"] for event in emitted_events] == ["exceeded"]
    [event] = emitted_events
    assert event.attributes["agents"] == [agent]
    assert event.attributes["waited_s"] > 0


async def test_collision_free_prepare_is_unchanged(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    database_gate: ProcessDbGate,
    *,
    machine_identity: MachineIdentity,
    emitted_events: EmittedEvents,
) -> None:
    owner = uuid4()
    agent = await _live_member(
        db_conn, aops_pool, owner, database_gate=database_gate, machine_identity=machine_identity
    )
    _as_live_host(monkeypatch, owner, machine_identity=machine_identity)
    emitted_events.clear()

    agent_pause.prepare(database, event_bus, "move", WHEN, local_machine=machine_identity)
    hold = agent_pause.require_maintenance_hold("move", WHEN)
    assert hold.phase == MaintenancePhase.DRAINING
    assert set(hold.commands) == {agent}
    assert emitted_events.names() == []


async def test_maintenance_command_refuses_without_wait(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    database_gate: ProcessDbGate,
    *,
    machine_identity: MachineIdentity,
    emitted_events: EmittedEvents,
) -> None:
    owner = uuid4()
    agent = await _live_member(
        db_conn, aops_pool, owner, database_gate=database_gate, machine_identity=machine_identity
    )
    insert_inbound_message(
        db_conn,
        agent,
        "",
        "system:maintenance",
        kind="restart",
        payload={"maintenance": {"holder": "other-move", "acquired_at": WHEN.isoformat()}},
        bus=event_bus,
        database=database,
    )
    db_conn.commit()
    _as_live_host(monkeypatch, owner, machine_identity=machine_identity)
    emitted_events.clear()

    with pytest.raises(RuntimeError, match="refusing without a wait") as raised:
        agent_pause.prepare(database, event_bus, "move", WHEN, local_machine=machine_identity)
    assert not isinstance(raised.value, cohort.LifecycleCollisionError)
    assert [event.attributes["outcome"] for event in emitted_events] == ["refused"]


def test_parked_agent_lifecycle_command_is_waitable(
    db_conn: psycopg.Connection[Any],
    database: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
) -> None:
    agent = maintenance_agent(db_conn, machine_name=machine_identity.name())
    insert_inbound_message(
        db_conn, agent, "", "agent:6090", kind="terminate", bus=event_bus, database=database
    )
    db_conn.commit()
    pause_owner.begin_maintenance("move", WHEN)

    with pytest.raises(cohort.LifecycleCollisionError) as raised:
        cohort.prepare(
            db_conn,
            machine=machine_identity.name(),
            host_owner=None,
            holder="move",
            acquired_at=WHEN,
        )
    assert raised.value.waitable
    assert raised.value.agent_ids == (agent,)


@pytest.mark.parametrize("age_s", [10, 601])
@pytest.mark.parametrize("legacy", [False, True])
def test_parked_claimed_ordinary_work_is_consumed_not_redelivered(
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    age_s: int,
    legacy: bool,
    database: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
    emitted_events: EmittedEvents,
) -> None:
    agent = maintenance_agent(db_conn, machine_name=machine_identity.name())
    message = insert_inbound_message(
        db_conn, agent, "hello", "user", bus=event_bus, database=database
    )
    _claim(db_conn, message)
    db_conn.execute(
        "UPDATE inbound_messages SET created_at=now()-make_interval(secs => %s), "
        "claimed_at=CASE WHEN %s THEN NULL ELSE now()-make_interval(secs => %s) END "
        "WHERE id=%s",
        (age_s, legacy, age_s, message),
    )
    db_conn.commit()
    emitted_events.clear()
    pause_owner.begin_maintenance("move", WHEN)

    hold = cohort.prepare(
        db_conn,
        machine=machine_identity.name(),
        host_owner=None,
        holder="move",
        acquired_at=WHEN,
    )
    assert (
        hold.phase == MaintenancePhase.DRAINING and hold.parked == (agent,) and hold.commands == {}
    )
    assert db_conn.execute(
        "SELECT status,applied_at FROM inbound_messages WHERE id=%s", (message,)
    ).fetchone() == ("done", None)
    [event] = emitted_events
    assert event.event_name == "pause_orphan_claim_settled"
    attributes = event.attributes
    assert attributes["agent"] == agent and attributes["message_id"] == message
    assert attributes["age_s"] >= age_s


def test_parked_claimed_work_prepares_without_wait(
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
    emitted_events: EmittedEvents,
) -> None:
    agent = maintenance_agent(db_conn, machine_name=machine_identity.name())
    message = insert_inbound_message(
        db_conn, agent, "hello", "user", bus=event_bus, database=database
    )
    _claim(db_conn, message)
    db_conn.commit()
    _as_live_host(monkeypatch, uuid4(), machine_identity=machine_identity)
    monkeypatch.setattr(settings.gateway, "pause_lifecycle_wait_seconds", 0.0)
    emitted_events.clear()

    agent_pause.prepare(database, event_bus, "move", WHEN, local_machine=machine_identity)

    hold = agent_pause.require_maintenance_hold("move", WHEN)
    assert hold.phase == MaintenancePhase.DRAINING
    assert set(hold.commands) == set()
    assert hold.parked == (agent,)
    assert emitted_events.names() == ["pause_orphan_claim_settled"]
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (message,)
    ).fetchone() == ("done",)


@pytest.mark.parametrize("kind", ["restart", "terminate"])
def test_parked_claimed_lifecycle_outliving_the_bound_aborts(
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    database: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
    emitted_events: EmittedEvents,
) -> None:
    agent = maintenance_agent(db_conn, machine_name=machine_identity.name())
    message = insert_inbound_message(
        db_conn, agent, "", "user", kind=kind, bus=event_bus, database=database
    )
    _claim(db_conn, message)
    db_conn.commit()
    _as_live_host(monkeypatch, uuid4(), machine_identity=machine_identity)
    monkeypatch.setattr(settings.gateway, "pause_lifecycle_wait_seconds", 0.3)
    _poll_fast(monkeypatch)
    emitted_events.clear()

    with pytest.raises(RuntimeError, match=r"waited .*still unfinished after the") as raised:
        agent_pause.prepare(database, event_bus, "move", WHEN, local_machine=machine_identity)
    assert not isinstance(raised.value, cohort.LifecycleCollisionError)
    assert "unfinished lifecycle command" in str(raised.value)

    hold = agent_pause.require_maintenance_hold("move", WHEN)
    assert hold.phase == MaintenancePhase.PREPARING and hold.commands == {} and hold.parked == ()
    assert [event.attributes["outcome"] for event in emitted_events] == ["exceeded"]
    [event] = emitted_events
    assert event.attributes["agents"] == [agent]
    assert event.attributes["waited_s"] > 0


def test_maintenance_authored_chat_refuses_and_rolls_back_orphan_settlement(
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
    emitted_events: EmittedEvents,
) -> None:
    agent = maintenance_agent(db_conn, machine_name=machine_identity.name())
    ordinary = insert_inbound_message(
        db_conn, agent, "orphan", "user", bus=event_bus, database=database
    )
    authored = insert_inbound_message(
        db_conn,
        agent,
        "maintenance",
        "system:maintenance",
        payload={"maintenance": {}},
        bus=event_bus,
        database=database,
    )
    _claim(db_conn, ordinary)
    _claim(db_conn, authored)
    db_conn.commit()
    _as_live_host(monkeypatch, uuid4(), machine_identity=machine_identity)
    emitted_events.clear()

    with pytest.raises(RuntimeError, match="refusing without a wait"):
        agent_pause.prepare(database, event_bus, "move", WHEN, local_machine=machine_identity)
    assert db_conn.execute(
        "SELECT id,status FROM inbound_messages WHERE agent_id=%s ORDER BY id", (agent,)
    ).fetchall() == [(ordinary, "claimed"), (authored, "claimed")]
    assert emitted_events.names() == ["pause_lifecycle_wait"]
    [event] = emitted_events
    assert event.attributes["outcome"] == "refused"
    hold = agent_pause.require_maintenance_hold("move", WHEN)
    assert hold.phase == MaintenancePhase.PREPARING and hold.parked == ()


def test_orphan_cas_miss_accepts_concurrent_settlement(
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
    emitted_events: EmittedEvents,
) -> None:
    agent = maintenance_agent(db_conn, machine_name=machine_identity.name())
    message = insert_inbound_message(
        db_conn, agent, "orphan", "user", bus=event_bus, database=database
    )
    _claim(db_conn, message)
    db_conn.commit()
    read_claims = cohort.orphaned_claims

    def already_settled(conn: psycopg.Connection[Any], **kwargs: Any) -> Any:
        rows = read_claims(conn, **kwargs)
        conn.execute("UPDATE inbound_messages SET status='done' WHERE id=%s", (message,))
        return rows

    monkeypatch.setattr(cohort, "orphaned_claims", already_settled)
    emitted_events.clear()
    pause_owner.begin_maintenance("move", WHEN)
    hold = cohort.prepare(
        db_conn,
        machine=machine_identity.name(),
        host_owner=None,
        holder="move",
        acquired_at=WHEN,
    )
    assert hold.phase == MaintenancePhase.DRAINING and hold.parked == (agent,)
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (message,)
    ).fetchone() == ("done",)
    assert emitted_events.names() == []


def test_parked_maintenance_command_refuses_without_wait(
    db_conn: psycopg.Connection[Any],
    database: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
) -> None:
    agent = maintenance_agent(db_conn, machine_name=machine_identity.name())
    command = insert_inbound_message(
        db_conn,
        agent,
        "",
        "system:maintenance",
        kind="restart",
        payload={"maintenance": {"holder": "other-move", "acquired_at": WHEN.isoformat()}},
        bus=event_bus,
        database=database,
    )
    _claim(db_conn, command)
    db_conn.commit()
    pause_owner.begin_maintenance("move", WHEN)

    with pytest.raises(cohort.LifecycleCollisionError) as raised:
        cohort.prepare(
            db_conn,
            machine=machine_identity.name(),
            host_owner=None,
            holder="move",
            acquired_at=WHEN,
        )
    assert not raised.value.waitable
    assert raised.value.agent_ids == (agent,)
    assert "another unfinished lifecycle command" in str(raised.value)


def test_parked_claim_guards_agree_on_waitability(
    db_conn: psycopg.Connection[Any],
    database: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
) -> None:
    agent = maintenance_agent(db_conn, machine_name=machine_identity.name())
    message = insert_inbound_message(
        db_conn, agent, "hello", "user", bus=event_bus, database=database
    )
    _claim(db_conn, message)
    db_conn.commit()
    hold = MaintenanceHold(parked=(agent,))

    with pytest.raises(cohort.LifecycleCollisionError) as collision_guard:
        cohort.refuse_inflight_lifecycle(
            db_conn, hold, frozenset[int](), holder="move", acquired_at=WHEN
        )
    with pytest.raises(cohort.LifecycleCollisionError) as resolved_guard:
        cohort.require_resolved(db_conn, hold)
    assert collision_guard.value.waitable and resolved_guard.value.waitable
    assert collision_guard.value.agent_ids == resolved_guard.value.agent_ids == (agent,)

    command = insert_inbound_message(
        db_conn,
        agent,
        "",
        "system:maintenance",
        kind="restart",
        payload={"maintenance": {"holder": "other-move", "acquired_at": WHEN.isoformat()}},
        bus=event_bus,
        database=database,
    )
    _claim(db_conn, command)
    db_conn.commit()

    with pytest.raises(cohort.LifecycleCollisionError) as collision_refused:
        cohort.refuse_inflight_lifecycle(
            db_conn, hold, frozenset[int](), holder="move", acquired_at=WHEN
        )
    with pytest.raises(cohort.LifecycleCollisionError) as resolved_refused:
        cohort.require_resolved(db_conn, hold)
    assert not collision_refused.value.waitable and not resolved_refused.value.waitable
