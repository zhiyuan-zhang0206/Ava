"""Active external executors receive periodic, pausable heartbeat check-ins."""

from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.agents import impersonation as leases
from base.config import settings
from base.db import Database
from base.events.live.bus import EventBus
from base.tests._impersonation_helpers import _active, _agent
from services.wake.heartbeat.daemon import (
    _reconcile_checkin_outcomes,
    _select_idle_agents_needing_heartbeat,
    _send_heartbeat_checkin,
)
from tests.impersonation_support import attested_caller


@pytest.fixture
def pool():
    with ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2) as pool:
        yield pool


def active_lease(conn: psycopg.Connection, *, age: float = 400) -> dict[str, Any]:
    lease = _active(_agent(conn))
    conn.execute(
        "UPDATE agent_impersonations SET activated_at=now()-make_interval(secs=>%s) WHERE id=%s",
        (age, lease["id"]),
    )
    conn.commit()
    return lease


def selected(pool: ConnectionPool) -> dict[int, float]:
    return dict(_select_idle_agents_needing_heartbeat(pool, 300, heartbeat_interval_s=300))


@pytest.mark.parametrize("status", ["idling", "running"])
def test_active_clock_does_not_depend_on_native_turns(
    pool: ConnectionPool, db_conn: psycopg.Connection, status: str
):
    lease = active_lease(db_conn)
    db_conn.execute(
        "UPDATE agents_meta SET status=%s,last_active_at=now(),heartbeat_backoff_level=5 WHERE id=%s",
        (status, lease["agent_id"]),
    )
    db_conn.commit()
    assert selected(pool)[lease["agent_id"]] == pytest.approx(400 / 60, abs=0.1)


def test_activation_starts_the_clock(pool: ConnectionPool, db_conn: psycopg.Connection):
    lease = active_lease(db_conn, age=20)
    db_conn.execute(
        "UPDATE agents_meta SET last_active_at=now()-interval '1 hour' WHERE id=%s",
        (lease["agent_id"],),
    )
    db_conn.commit()
    assert lease["agent_id"] not in selected(pool)


def test_pause_then_normal_cadence(
    pool: ConnectionPool, db_conn: psycopg.Connection, database: Database, event_bus: EventBus
):
    lease = active_lease(db_conn)
    aid = lease["agent_id"]
    db_conn.execute(
        "UPDATE agents_meta SET heartbeat_paused_until=now()+interval '1 hour' WHERE id=%s", (aid,)
    )
    db_conn.commit()
    assert aid not in selected(pool)
    db_conn.execute(
        "UPDATE agents_meta SET heartbeat_paused_until=now()-interval '1 second' WHERE id=%s",
        (aid,),
    )
    db_conn.commit()
    assert aid in selected(pool)
    _send_heartbeat_checkin(pool, database, event_bus, aid, 400 / 60)
    messages = leases.inbox(database, lease["id"], attested_caller(lease))
    assert [m["kind"] for m in messages] == ["heartbeat"]
    leases.ack(database, event_bus, lease["id"], attested_caller(lease), [messages[0]["id"]])
    assert aid not in selected(pool)
    db_conn.execute(
        "UPDATE agents_meta SET last_heartbeat_at=now()-interval '301 seconds' WHERE id=%s", (aid,)
    )
    db_conn.commit()
    assert aid in selected(pool)


def test_external_work_is_not_a_failed_native_turn(
    pool: ConnectionPool, db_conn: psycopg.Connection
):
    lease = active_lease(db_conn)
    aid = lease["agent_id"]
    pending, failures, noops = {aid: 400 / 60}, {aid: 2}, {aid: 2}
    _reconcile_checkin_outcomes(
        pool,
        pending_checkin=pending,
        failure_streak=failures,
        noop_streak=noops,
        idle_threshold_s=300,
        noop_nudges_threshold=1,
    )
    assert pending == failures == noops == {}
    assert db_conn.execute(
        "SELECT heartbeat_backoff_level FROM agents_meta WHERE id=%s", (aid,)
    ).fetchone() == (0,)


def test_expired_active_lease_does_not_receive_checkin(
    pool: ConnectionPool, db_conn: psycopg.Connection
):
    lease = active_lease(db_conn)
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=now()-interval '1 second' WHERE id=%s",
        (lease["id"],),
    )
    db_conn.commit()
    assert lease["agent_id"] not in selected(pool)


def test_sdk_attachment_can_pause_heartbeat(
    pool: ConnectionPool, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
):
    import ava
    from agent.state import BaseAgentState
    from ava import external
    from tests.fixtures.pin_agent import pin_agent

    lease = active_lease(db_conn)
    pin_agent(None)
    monkeypatch.setattr(external, "process_metadata", lambda: attested_caller(lease))

    def snapshot(_agent_id: int) -> tuple[BaseAgentState, dict[str, Any], None]:
        return BaseAgentState(), {}, None

    monkeypatch.setattr(external, "load_snapshot", snapshot)
    with external.attach(lease["id"]):
        ava.self.pause_heartbeat(1800)
    assert lease["agent_id"] not in selected(pool)
    assert db_conn.execute(
        "SELECT duration_s FROM heartbeat_pause_log WHERE agent_id=%s", (lease["agent_id"],)
    ).fetchone() == (1800,)


def test_heartbeat_history_survives_ack_including_activation_backlog(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
):
    owner = _agent(db_conn)
    _send_heartbeat_checkin(pool, database, event_bus, owner.agent_id, 7)
    lease = _active(owner)
    _send_heartbeat_checkin(pool, database, event_bus, owner.agent_id, 8)
    messages = leases.inbox(database, lease["id"], attested_caller(lease))
    ids = [m["id"] for m in messages]
    assert len(ids) == 2
    assert all(m["kind"] == "heartbeat" for m in messages)
    leases.ack(database, event_bus, lease["id"], attested_caller(lease), ids)
    assert leases.inbox(database, lease["id"], attested_caller(lease)) == []
    records = db_conn.execute(
        "SELECT (payload->>'inbound_id')::bigint FROM agent_impersonation_entries "
        "WHERE lease_id=%s AND kind='message' AND payload->>'kind'='heartbeat' ORDER BY seq",
        (lease["id"],),
    ).fetchall()
    assert [r[0] for r in records] == ids
