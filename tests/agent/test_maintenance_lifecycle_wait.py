"""Bounded wait for in-flight work during preparation (task #3591).

Three states, per the acceptance criteria: work that resolves during the wait
lets preparation proceed; one that outlives the bound aborts with the wait
result in the message and an ``exceeded`` event; and a collision-free
preparation is the unchanged pre-#3591 path. Class separation (maintenance-
authored commands refuse without waiting; ordinary lifecycle commands and
claimed ordinary work on a parked agent wait) and the clean retry boundary (a
collision freezes nothing) are asserted alongside.
"""

import asyncio
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.hosted_ownership import admit_hosted_runtime, settle_hosted_runtime
from ops import agent_pause
from ops.agent_pause_probe import HostIdentity
from shared import maintenance, maintenance_cohort, pause_owner, telemetry
from shared.db import insert_inbound_message
from shared.machine import machine_name
from shared.maintenance_state import MaintenanceHold
from tests.agent.test_maintenance import WHEN, _agent
from tests.agent.test_maintenance import isolate as isolate


def _as_live_host(monkeypatch: pytest.MonkeyPatch, owner: UUID) -> None:
    """Drive `_prepare` as the live agent-runner host owning `owner`."""
    monkeypatch.setattr(agent_pause, "machine_role", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(agent_pause, "host_running", lambda: True)
    monkeypatch.setattr(agent_pause, "host_identity", lambda: HostIdentity(owner, frozenset()))
    monkeypatch.setattr(agent_pause, "_wake", MagicMock())


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
        aops_pool, agent, machine_name(), owner, expected_from="idling"
    )
    assert incarnation is not None
    assert await settle_hosted_runtime(aops_pool, incarnation)
    return agent


async def test_member_collision_is_waitable_and_freezes_nothing(
    db_conn: psycopg.Connection[Any], aops_pool: AsyncConnectionPool[Any]
) -> None:
    owner = uuid4()
    agent = await _live_member(db_conn, aops_pool, owner)
    other = await _live_member(db_conn, aops_pool, owner)
    command = insert_inbound_message(db_conn, agent, "", "agent:6090", kind="terminate")
    db_conn.commit()
    pause_owner.begin_maintenance("move", WHEN)

    with pytest.raises(maintenance_cohort.LifecycleCollisionError) as raised:
        maintenance_cohort.prepare(
            db_conn, machine=machine_name(), host_owner=owner, holder="move", acquired_at=WHEN
        )
    assert raised.value.waitable
    assert raised.value.agent_ids == (agent,)

    # The collision ran before the capture: the journal still holds a clean
    # preparing boundary, so the retry re-derives the cohort from scratch.
    hold = maintenance.require_operation("move", WHEN).maintenance
    assert hold is not None and hold.phase == "preparing" and hold.commands == {}

    # The competing terminate resolves; preparation proceeds without the agent.
    _resolve_lifecycle_command(db_conn, command)
    db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (agent,))
    db_conn.commit()
    hold = maintenance_cohort.prepare(
        db_conn, machine=machine_name(), host_owner=owner, holder="move", acquired_at=WHEN
    )
    assert hold.phase == "draining"
    assert set(hold.commands) == {other}


async def test_prepare_waits_for_resolving_command_then_proceeds(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = uuid4()
    agent = await _live_member(db_conn, aops_pool, owner)
    command = insert_inbound_message(db_conn, agent, "", "agent:6090", kind="terminate")
    db_conn.commit()
    _as_live_host(monkeypatch, owner)
    monkeypatch.setattr(agent_pause, "_lifecycle_wait_seconds", lambda: 5.0)
    monkeypatch.setattr(agent_pause, "_LIFECYCLE_WAIT_POLL_SECONDS", 0.05)
    events = _events(monkeypatch)

    real_prepare = maintenance_cohort.prepare
    resolved: list[bool] = []

    def resolving_prepare(*args: Any, **kwargs: Any) -> maintenance_cohort.MaintenanceHold:
        try:
            return real_prepare(*args, **kwargs)
        except maintenance_cohort.LifecycleCollisionError:
            if not resolved:
                resolved.append(True)
                _resolve_lifecycle_command(db_conn, command)
                db_conn.commit()
            raise

    monkeypatch.setattr(maintenance_cohort, "prepare", resolving_prepare)
    await asyncio.to_thread(agent_pause._prepare, "move", WHEN)

    hold = agent_pause._hold("move", WHEN)
    assert hold.phase == "draining"
    assert set(hold.commands) == {agent}
    assert [event["event_name"] for event in events] == ["pause_lifecycle_wait"]
    attributes = events[0]["attributes"]
    assert attributes["outcome"] == "resolved"
    assert attributes["agents"] == [agent]
    assert 0 < attributes["waited_s"] < 5.0


async def test_prepare_aborts_when_collision_outlives_the_bound(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = uuid4()
    agent = await _live_member(db_conn, aops_pool, owner)
    insert_inbound_message(db_conn, agent, "", "agent:6090", kind="terminate")
    db_conn.commit()
    _as_live_host(monkeypatch, owner)
    monkeypatch.setattr(agent_pause, "_lifecycle_wait_seconds", lambda: 0.3)
    monkeypatch.setattr(agent_pause, "_LIFECYCLE_WAIT_POLL_SECONDS", 0.05)
    events = _events(monkeypatch)

    with pytest.raises(RuntimeError, match=r"waited .*still unfinished after the") as raised:
        agent_pause._prepare("move", WHEN)
    assert not isinstance(raised.value, maintenance_cohort.LifecycleCollisionError)
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
) -> None:
    owner = uuid4()
    agent = await _live_member(db_conn, aops_pool, owner)
    _as_live_host(monkeypatch, owner)
    events = _events(monkeypatch)

    agent_pause._prepare("move", WHEN)
    hold = agent_pause._hold("move", WHEN)
    assert hold.phase == "draining"
    assert set(hold.commands) == {agent}
    assert events == []


async def test_maintenance_command_refuses_without_wait(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
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
    )
    db_conn.commit()
    _as_live_host(monkeypatch, owner)
    events = _events(monkeypatch)

    with pytest.raises(RuntimeError, match="refusing without a wait") as raised:
        agent_pause._prepare("move", WHEN)
    assert not isinstance(raised.value, maintenance_cohort.LifecycleCollisionError)
    assert [event["attributes"]["outcome"] for event in events] == ["refused"]


def test_parked_agent_lifecycle_command_is_waitable(
    db_conn: psycopg.Connection[Any],
) -> None:
    agent = _agent(db_conn)
    insert_inbound_message(db_conn, agent, "", "agent:6090", kind="terminate")
    db_conn.commit()
    pause_owner.begin_maintenance("move", WHEN)

    with pytest.raises(maintenance_cohort.LifecycleCollisionError) as raised:
        maintenance_cohort.prepare(
            db_conn, machine=machine_name(), host_owner=None, holder="move", acquired_at=WHEN
        )
    assert raised.value.waitable
    assert raised.value.agent_ids == (agent,)


def test_parked_claimed_ordinary_work_is_waitable(
    db_conn: psycopg.Connection[Any],
) -> None:
    agent = _agent(db_conn)
    message = insert_inbound_message(db_conn, agent, "hello", "user")
    _claim(db_conn, message)
    db_conn.commit()
    pause_owner.begin_maintenance("move", WHEN)

    with pytest.raises(maintenance_cohort.LifecycleCollisionError) as raised:
        maintenance_cohort.prepare(
            db_conn, machine=machine_name(), host_owner=None, holder="move", acquired_at=WHEN
        )
    assert raised.value.waitable
    assert raised.value.agent_ids == (agent,)
    assert "unresolved claimed work" in str(raised.value)

    # The collision ran before the capture: the retry starts from the clean
    # preparing boundary, not from a partial cohort.
    hold = maintenance.require_operation("move", WHEN).maintenance
    assert hold is not None and hold.phase == "preparing" and hold.commands == {}


def test_parked_claimed_work_resolving_during_wait_prepares(
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _agent(db_conn)
    message = insert_inbound_message(db_conn, agent, "hello", "user")
    _claim(db_conn, message)
    db_conn.commit()
    _as_live_host(monkeypatch, uuid4())
    monkeypatch.setattr(agent_pause, "_lifecycle_wait_seconds", lambda: 5.0)
    monkeypatch.setattr(agent_pause, "_LIFECYCLE_WAIT_POLL_SECONDS", 0.05)
    events = _events(monkeypatch)

    real_prepare = maintenance_cohort.prepare

    def resolving_prepare(*args: Any, **kwargs: Any) -> maintenance_cohort.MaintenanceHold:
        try:
            return real_prepare(*args, **kwargs)
        except maintenance_cohort.LifecycleCollisionError:
            db_conn.execute("UPDATE inbound_messages SET status='done' WHERE id=%s", (message,))
            db_conn.commit()
            raise

    monkeypatch.setattr(maintenance_cohort, "prepare", resolving_prepare)
    agent_pause._prepare("move", WHEN)

    hold = agent_pause._hold("move", WHEN)
    assert hold.phase == "draining"
    assert set(hold.commands) == set()
    assert hold.parked == (agent,)
    assert [event["event_name"] for event in events] == ["pause_lifecycle_wait"]
    attributes = events[0]["attributes"]
    assert attributes["outcome"] == "resolved"
    assert attributes["agents"] == [agent]
    assert 0 < attributes["waited_s"] < 5.0


def test_parked_claimed_work_outliving_the_bound_aborts(
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _agent(db_conn)
    message = insert_inbound_message(db_conn, agent, "hello", "user")
    _claim(db_conn, message)
    db_conn.commit()
    _as_live_host(monkeypatch, uuid4())
    monkeypatch.setattr(agent_pause, "_lifecycle_wait_seconds", lambda: 0.3)
    monkeypatch.setattr(agent_pause, "_LIFECYCLE_WAIT_POLL_SECONDS", 0.05)
    events = _events(monkeypatch)

    with pytest.raises(RuntimeError, match=r"waited .*still unfinished after the") as raised:
        agent_pause._prepare("move", WHEN)
    assert not isinstance(raised.value, maintenance_cohort.LifecycleCollisionError)
    assert "unresolved claimed work" in str(raised.value)

    # Fail-closed: nothing was captured, and the hold stays preparing.
    hold = agent_pause._hold("move", WHEN)
    assert hold.phase == "preparing" and hold.commands == {} and hold.parked == ()
    assert [event["attributes"]["outcome"] for event in events] == ["exceeded"]
    assert events[0]["attributes"]["agents"] == [agent]
    assert events[0]["attributes"]["waited_s"] > 0


def test_parked_maintenance_command_refuses_without_wait(
    db_conn: psycopg.Connection[Any],
) -> None:
    agent = _agent(db_conn)
    command = insert_inbound_message(
        db_conn,
        agent,
        "",
        "system:maintenance",
        kind="restart",
        payload={"maintenance": {"holder": "other-move", "acquired_at": WHEN.isoformat()}},
    )
    _claim(db_conn, command)
    db_conn.commit()
    pause_owner.begin_maintenance("move", WHEN)

    with pytest.raises(maintenance_cohort.LifecycleCollisionError) as raised:
        maintenance_cohort.prepare(
            db_conn, machine=machine_name(), host_owner=None, holder="move", acquired_at=WHEN
        )
    assert not raised.value.waitable
    assert raised.value.agent_ids == (agent,)
    assert "another unfinished lifecycle command" in str(raised.value)


def test_parked_claim_guards_agree_on_waitability(
    db_conn: psycopg.Connection[Any],
) -> None:
    """Both parked guards carry the same rule: ordinary work waits, maintenance refuses.

    ``_require_resolved`` sits behind ``_refuse_inflight_lifecycle`` as the
    second guard, and preparation's retry decision depends on the two staying
    consistent (task #4013).
    """
    agent = _agent(db_conn)
    message = insert_inbound_message(db_conn, agent, "hello", "user")
    _claim(db_conn, message)
    db_conn.commit()
    hold = MaintenanceHold(parked=(agent,))

    with pytest.raises(maintenance_cohort.LifecycleCollisionError) as collision_guard:
        maintenance_cohort._refuse_inflight_lifecycle(
            db_conn, hold, frozenset[int](), holder="move", acquired_at=WHEN
        )
    with pytest.raises(maintenance_cohort.LifecycleCollisionError) as resolved_guard:
        maintenance_cohort._require_resolved(db_conn, hold)
    assert collision_guard.value.waitable and resolved_guard.value.waitable
    assert collision_guard.value.agent_ids == resolved_guard.value.agent_ids == (agent,)

    command = insert_inbound_message(
        db_conn,
        agent,
        "",
        "system:maintenance",
        kind="restart",
        payload={"maintenance": {"holder": "other-move", "acquired_at": WHEN.isoformat()}},
    )
    _claim(db_conn, command)
    db_conn.commit()

    with pytest.raises(maintenance_cohort.LifecycleCollisionError) as collision_refused:
        maintenance_cohort._refuse_inflight_lifecycle(
            db_conn, hold, frozenset[int](), holder="move", acquired_at=WHEN
        )
    with pytest.raises(maintenance_cohort.LifecycleCollisionError) as resolved_refused:
        maintenance_cohort._require_resolved(db_conn, hold)
    assert not collision_refused.value.waitable and not resolved_refused.value.waitable
