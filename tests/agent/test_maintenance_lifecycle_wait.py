"""Bounded wait for in-flight work during preparation (task #3591).

Three states, per the acceptance criteria: work that resolves during the wait
lets preparation proceed; one that outlives the bound aborts with the wait
result in the message and an ``exceeded`` event; and a collision-free
preparation is the unchanged pre-#3591 path. Class separation (maintenance-
authored commands refuse without waiting; ordinary lifecycle commands wait;
orphaned ordinary claims settle) and the clean retry boundary (a
collision freezes nothing) are asserted alongside.
"""

import asyncio
import time
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.ownership.hosted import admit_hosted_runtime, settle_hosted_runtime
from base import telemetry
from base.cluster.machine import machine_name
from base.config import settings
from base.db import Database, insert_inbound_message
from base.deploy.maintenance import admission, cohort, pause_owner
from base.deploy.maintenance.state import MaintenanceHold
from base.events.live.bus import EventBus
from ops import agent_pause
from ops.agent_pause.probe import HostIdentity
from tests.agent.test_maintenance import WHEN, _agent
from tests.agent.test_maintenance import isolate as isolate


def _as_live_host(monkeypatch: pytest.MonkeyPatch, owner: UUID) -> None:
    """Drive `_prepare` as the live agent-runner host owning `owner`."""
    monkeypatch.setattr(agent_pause, "machine_role", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(agent_pause, "host_running", lambda: True)
    monkeypatch.setattr(agent_pause, "host_identity", lambda: HostIdentity(owner, frozenset()))
    monkeypatch.setattr(agent_pause, "publish_inbound_wake", MagicMock())


def _poll_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cap each wait-loop sleep at 50 ms so a collision is re-checked quickly."""
    real_sleep = time.sleep

    def short_sleep(seconds: float) -> None:
        real_sleep(min(seconds, 0.05))

    monkeypatch.setattr(time, "sleep", short_sleep)


def _events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture `pause_lifecycle_wait` emissions instead of writing event rows."""
    calls: list[dict[str, Any]] = []

    def fake(category: str, event_name: str, **kwargs: Any) -> None:
        calls.append({"category": category, "event_name": event_name, **kwargs})

    monkeypatch.setattr(telemetry, "emit", fake)
    return calls


def _claim(conn: psycopg.Connection[Any], message: int) -> None:
    """Bring one inbound row to the claimed state the wait must observe."""
    conn.execute(
        "UPDATE inbound_messages SET status='claimed', claimed_at=clock_timestamp() WHERE id=%s",
        (message,),
    )


def _resolve_lifecycle_command(conn: psycopg.Connection[Any], command: int) -> None:
    """Complete a lifecycle command the way the host would (claimed targets, applied, observed).

    `inbound_lifecycle_target_check` requires the target fields on any
    claimed/done lifecycle row (db/schema.sql:398).
    """
    conn.execute(
        "UPDATE inbound_messages SET status='done', claimed_at=COALESCE(claimed_at, "
        "clock_timestamp()), target_generation=%s, target_owner=%s, applied_at=clock_timestamp(), "
        "observed_at=clock_timestamp() WHERE id=%s",
        (uuid4(), uuid4(), command),
    )


async def _live_member(
    conn: psycopg.Connection[Any], aops_pool: AsyncConnectionPool[Any], owner: UUID
) -> int:
    """An idling hosted agent under the live owner — preparation's original cohort."""
    agent = _agent(conn)
    incarnation = await admit_hosted_runtime(
        aops_pool, agent, machine_name(), owner, expected_from="idling", db=Database.from_settings()
    )
    assert incarnation is not None
    assert await settle_hosted_runtime(aops_pool, incarnation, bus=EventBus.from_settings())
    return agent


async def test_member_collision_is_waitable_and_freezes_nothing(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
) -> None:
    owner = uuid4()
    agent = await _live_member(db_conn, aops_pool, owner)
    other = await _live_member(db_conn, aops_pool, owner)
    command = insert_inbound_message(
        db_conn, agent, "", "agent:6090", kind="terminate", bus=event_bus, database=database
    )
    db_conn.commit()
    pause_owner.begin_maintenance("move", WHEN)

    with pytest.raises(cohort.LifecycleCollisionError) as raised:
        cohort.prepare(
            db_conn,
            machine=machine_name(),
            host_owner=owner,
            holder="move",
            acquired_at=WHEN,
        )
    assert raised.value.waitable
    assert raised.value.agent_ids == (agent,)

    # The collision ran before the capture: the journal still holds a clean
    # preparing boundary, so the retry re-derives the cohort from scratch.
    hold = admission.require_operation("move", WHEN).maintenance
    assert hold is not None and hold.phase == "preparing" and hold.commands == {}

    # The competing terminate resolves; preparation proceeds without the agent.
    _resolve_lifecycle_command(db_conn, command)
    db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (agent,))
    db_conn.commit()
    hold = cohort.prepare(
        db_conn,
        machine=machine_name(),
        host_owner=owner,
        holder="move",
        acquired_at=WHEN,
    )
    assert hold.phase == "draining"
    assert set(hold.commands) == {other}


async def test_prepare_waits_for_resolving_command_then_proceeds(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
) -> None:
    owner = uuid4()
    agent = await _live_member(db_conn, aops_pool, owner)
    chat = insert_inbound_message(
        db_conn, agent, "in flight", "user", bus=event_bus, database=database
    )
    _claim(db_conn, chat)
    command = insert_inbound_message(
        db_conn, agent, "", "agent:6090", kind="terminate", bus=event_bus, database=database
    )
    db_conn.commit()
    _as_live_host(monkeypatch, owner)
    monkeypatch.setattr(settings.gateway, "pause_lifecycle_wait_seconds", 5.0)
    _poll_fast(monkeypatch)
    events = _events(monkeypatch)

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
    await asyncio.to_thread(agent_pause.prepare, database, event_bus, "move", WHEN)

    hold = agent_pause._hold("move", WHEN)
    assert hold.phase == "draining"
    assert set(hold.commands) == {agent}
    assert [event["event_name"] for event in events] == ["pause_lifecycle_wait"]
    attributes = events[0]["attributes"]
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
) -> None:
    owner = uuid4()
    agent = await _live_member(db_conn, aops_pool, owner)
    insert_inbound_message(
        db_conn, agent, "", "agent:6090", kind="terminate", bus=event_bus, database=database
    )
    db_conn.commit()
    _as_live_host(monkeypatch, owner)
    monkeypatch.setattr(settings.gateway, "pause_lifecycle_wait_seconds", 0.3)
    _poll_fast(monkeypatch)
    events = _events(monkeypatch)

    with pytest.raises(RuntimeError, match=r"waited .*still unfinished after the") as raised:
        agent_pause.prepare(database, event_bus, "move", WHEN)
    assert not isinstance(raised.value, cohort.LifecycleCollisionError)
    assert "unfinished lifecycle command" in str(raised.value)

    # Fail-closed: nothing was captured, and the hold stays preparing.
    hold = agent_pause._hold("move", WHEN)
    assert hold.phase == "preparing" and hold.commands == {}
    assert [event["attributes"]["outcome"] for event in events] == ["exceeded"]
    assert events[0]["attributes"]["agents"] == [agent]
    assert events[0]["attributes"]["waited_s"] > 0


async def test_collision_free_prepare_is_unchanged(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
) -> None:
    owner = uuid4()
    agent = await _live_member(db_conn, aops_pool, owner)
    _as_live_host(monkeypatch, owner)
    events = _events(monkeypatch)

    agent_pause.prepare(database, event_bus, "move", WHEN)
    hold = agent_pause._hold("move", WHEN)
    assert hold.phase == "draining"
    assert set(hold.commands) == {agent}
    assert events == []


async def test_maintenance_command_refuses_without_wait(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
) -> None:
    owner = uuid4()
    agent = await _live_member(db_conn, aops_pool, owner)
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
    _as_live_host(monkeypatch, owner)
    events = _events(monkeypatch)

    with pytest.raises(RuntimeError, match="refusing without a wait") as raised:
        agent_pause.prepare(database, event_bus, "move", WHEN)
    assert not isinstance(raised.value, cohort.LifecycleCollisionError)
    assert [event["attributes"]["outcome"] for event in events] == ["refused"]


def test_parked_agent_lifecycle_command_is_waitable(
    db_conn: psycopg.Connection[Any],
    database: Database,
    event_bus: EventBus,
) -> None:
    agent = _agent(db_conn)
    insert_inbound_message(
        db_conn, agent, "", "agent:6090", kind="terminate", bus=event_bus, database=database
    )
    db_conn.commit()
    pause_owner.begin_maintenance("move", WHEN)

    with pytest.raises(cohort.LifecycleCollisionError) as raised:
        cohort.prepare(
            db_conn,
            machine=machine_name(),
            host_owner=None,
            holder="move",
            acquired_at=WHEN,
        )
    assert raised.value.waitable
    assert raised.value.agent_ids == (agent,)


@pytest.mark.parametrize("age_s,expected", [(10, "pending"), (601, "done")])
@pytest.mark.parametrize("legacy", [False, True])
def test_parked_claimed_ordinary_work_settles(
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    age_s: int,
    expected: str,
    legacy: bool,
    database: Database,
    event_bus: EventBus,
) -> None:
    agent = _agent(db_conn)
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
    monkeypatch.setattr(settings.daemon, "delivery_watchdog_stale_claimed_threshold_seconds", 600)
    events = _events(monkeypatch)
    pause_owner.begin_maintenance("move", WHEN)

    hold = cohort.prepare(
        db_conn,
        machine=machine_name(),
        host_owner=None,
        holder="move",
        acquired_at=WHEN,
    )
    assert hold.phase == "draining" and hold.parked == (agent,) and hold.commands == {}
    assert db_conn.execute(
        "SELECT status,applied_at FROM inbound_messages WHERE id=%s", (message,)
    ).fetchone() == (expected, None)
    assert len(events) == 1
    assert events[0]["event_name"] == "pause_orphan_claim_settled"
    attributes = events[0]["attributes"]
    assert attributes["agent"] == agent and attributes["message_id"] == message
    assert attributes["outcome"] == expected and attributes["age_s"] >= age_s


def test_parked_claimed_work_prepares_without_wait(
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
) -> None:
    agent = _agent(db_conn)
    message = insert_inbound_message(
        db_conn, agent, "hello", "user", bus=event_bus, database=database
    )
    _claim(db_conn, message)
    db_conn.commit()
    _as_live_host(monkeypatch, uuid4())
    monkeypatch.setattr(settings.gateway, "pause_lifecycle_wait_seconds", 0.0)
    events = _events(monkeypatch)

    agent_pause.prepare(database, event_bus, "move", WHEN)

    hold = agent_pause._hold("move", WHEN)
    assert hold.phase == "draining"
    assert set(hold.commands) == set()
    assert hold.parked == (agent,)
    assert [event["event_name"] for event in events] == ["pause_orphan_claim_settled"]
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (message,)
    ).fetchone() == ("pending",)


@pytest.mark.parametrize("kind", ["restart", "terminate"])
def test_parked_claimed_lifecycle_outliving_the_bound_aborts(
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    database: Database,
    event_bus: EventBus,
) -> None:
    agent = _agent(db_conn)
    message = insert_inbound_message(
        db_conn, agent, "", "user", kind=kind, bus=event_bus, database=database
    )
    _claim(db_conn, message)
    db_conn.commit()
    _as_live_host(monkeypatch, uuid4())
    monkeypatch.setattr(settings.gateway, "pause_lifecycle_wait_seconds", 0.3)
    _poll_fast(monkeypatch)
    events = _events(monkeypatch)

    with pytest.raises(RuntimeError, match=r"waited .*still unfinished after the") as raised:
        agent_pause.prepare(database, event_bus, "move", WHEN)
    assert not isinstance(raised.value, cohort.LifecycleCollisionError)
    assert "unfinished lifecycle command" in str(raised.value)

    # Fail-closed: nothing was captured, and the hold stays preparing.
    hold = agent_pause._hold("move", WHEN)
    assert hold.phase == "preparing" and hold.commands == {} and hold.parked == ()
    assert [event["attributes"]["outcome"] for event in events] == ["exceeded"]
    assert events[0]["attributes"]["agents"] == [agent]
    assert events[0]["attributes"]["waited_s"] > 0


def test_maintenance_authored_chat_refuses_and_rolls_back_orphan_settlement(
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
) -> None:
    agent = _agent(db_conn)
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
    _as_live_host(monkeypatch, uuid4())
    events = _events(monkeypatch)

    with pytest.raises(RuntimeError, match="refusing without a wait"):
        agent_pause.prepare(database, event_bus, "move", WHEN)
    assert db_conn.execute(
        "SELECT id,status FROM inbound_messages WHERE agent_id=%s ORDER BY id", (agent,)
    ).fetchall() == [(ordinary, "claimed"), (authored, "claimed")]
    assert [event["event_name"] for event in events] == ["pause_lifecycle_wait"]
    assert events[0]["attributes"]["outcome"] == "refused"
    hold = agent_pause._hold("move", WHEN)
    assert hold.phase == "preparing" and hold.parked == ()


def test_orphan_cas_miss_accepts_concurrent_settlement(
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
) -> None:
    agent = _agent(db_conn)
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
    events = _events(monkeypatch)
    pause_owner.begin_maintenance("move", WHEN)
    hold = cohort.prepare(
        db_conn,
        machine=machine_name(),
        host_owner=None,
        holder="move",
        acquired_at=WHEN,
    )
    assert hold.phase == "draining" and hold.parked == (agent,)
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (message,)
    ).fetchone() == ("done",)
    assert events == []


def test_parked_maintenance_command_refuses_without_wait(
    db_conn: psycopg.Connection[Any],
    database: Database,
    event_bus: EventBus,
) -> None:
    agent = _agent(db_conn)
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
            machine=machine_name(),
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
) -> None:
    """Both parked guards carry the same rule: ordinary work waits, maintenance refuses.

    ``_require_resolved`` sits behind ``_refuse_inflight_lifecycle`` as the
    second guard, and preparation's retry decision depends on the two staying
    consistent (task #4013).
    """
    agent = _agent(db_conn)
    message = insert_inbound_message(
        db_conn, agent, "hello", "user", bus=event_bus, database=database
    )
    _claim(db_conn, message)
    db_conn.commit()
    hold = MaintenanceHold(parked=(agent,))

    with pytest.raises(cohort.LifecycleCollisionError) as collision_guard:
        cohort._refuse_inflight_lifecycle(
            db_conn, hold, frozenset[int](), holder="move", acquired_at=WHEN
        )
    with pytest.raises(cohort.LifecycleCollisionError) as resolved_guard:
        cohort._require_resolved(db_conn, hold)
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
        cohort._refuse_inflight_lifecycle(
            db_conn, hold, frozenset[int](), holder="move", acquired_at=WHEN
        )
    with pytest.raises(cohort.LifecycleCollisionError) as resolved_refused:
        cohort._require_resolved(db_conn, hold)
    assert not collision_refused.value.waitable and not resolved_refused.value.waitable
