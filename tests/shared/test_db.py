"""`shared/db.py` live-agent helpers — the SQL the `ava cluster update` quiesce step drives.

These are the relocated home of the agents_meta / inbound_messages queries the
gateway CLI used to hand-write inline: signal_live_agents_restart (bulk
restart), list_live_agent_ids. "Live" = status running/idling.
Each helper opens its own connection, so it sees rows committed by the fixture.
"""

from __future__ import annotations

import asyncio
import time

import psycopg
import pytest

from shared import db
from shared.config import settings
from shared.dotenv_boot import UNANCHORED_DB_SENTINEL
from shared.redis_listener import RedisInboundListener


def test_connect_refuses_unanchored_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    """connect() raises UnanchoredHomeError when db_url is the unanchored sentinel,
    rather than letting an unanchored dev checkout reach a real database."""
    monkeypatch.setattr(settings.data_plane, "db_url", UNANCHORED_DB_SENTINEL)
    with pytest.raises(db.UnanchoredHomeError):
        db.connect()


def test_pool_refuses_unanchored_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.data_plane, "db_url", UNANCHORED_DB_SENTINEL)
    with pytest.raises(db.UnanchoredHomeError):
        db.pool()


def _seed_agent(db_conn: psycopg.Connection, status: str, *, live_lease: bool = True) -> int:
    """Create an agent + its agents_meta row in the given status, return id.

    `live_lease` grants the R1 liveness lease (default True — a seeded live
    agent renews like a real one); pass False to seed a lease-less (pre-lease /
    zombie) row, which the alive predicate reads as dead."""
    from datetime import UTC, datetime, timedelta

    agent_id = db.create_agent(db_conn)
    lease = datetime.now(UTC) + timedelta(seconds=600) if live_lease else None
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status, lease_expires_at) "
            "VALUES (%s, 'test', %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, "
            "    lease_expires_at = EXCLUDED.lease_expires_at",
            (agent_id, status, lease),
        )
    db_conn.commit()
    return agent_id


def _inbound_rows(db_conn: psycopg.Connection, agent_id: int) -> list[tuple[str, str, str]]:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT kind, source, content FROM inbound_messages WHERE agent_id = %s",
            (agent_id,),
        )
        return cur.fetchall()


def test_signal_live_agents_restart_only_live(db_conn: psycopg.Connection) -> None:
    """One restart inbound (content='', the given source) per running/idling agent;
    terminated/restarting get none. Returns the ids signalled."""
    running = _seed_agent(db_conn, "running")
    idling = _seed_agent(db_conn, "idling")
    terminated = _seed_agent(db_conn, "terminated")
    restarting = _seed_agent(db_conn, "restarting")

    ids = db.signal_live_agents_restart(source="system:update")

    assert sorted(ids) == sorted([running, idling])
    assert _inbound_rows(db_conn, running) == [("restart", "system:update", "")]
    assert _inbound_rows(db_conn, idling) == [("restart", "system:update", "")]
    assert _inbound_rows(db_conn, terminated) == []
    assert _inbound_rows(db_conn, restarting) == []


def test_signal_live_agents_restart_requires_an_unexpired_lease(
    db_conn: psycopg.Connection,
) -> None:
    """R1 (Task #1021): a running/idling row WITHOUT a lease (pre-lease code) or
    with an EXPIRED one (a process that stopped renewing) is not alive — the
    lease is the liveness authority, and the quiesce must not signal a zombie."""
    running_no_lease = _seed_agent(db_conn, "running", live_lease=False)
    idling_no_lease = _seed_agent(db_conn, "idling", live_lease=False)
    running_fresh = _seed_agent(db_conn, "running")

    ids = db.signal_live_agents_restart(source="system:update")

    assert ids == [running_fresh]
    assert _inbound_rows(db_conn, running_no_lease) == []
    assert _inbound_rows(db_conn, idling_no_lease) == []


def test_agent_is_alive_predicate() -> None:
    """The Python half of the single alive predicate — status AND unexpired
    lease, one definition for row-based checks."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    future = now + timedelta(seconds=600)
    past = now - timedelta(seconds=1)

    assert db.agent_is_alive("running", future) is True
    assert db.agent_is_alive("idling", future) is True
    assert db.agent_is_alive("running", None) is False  # pre-lease row
    assert db.agent_is_alive("running", past) is False  # expired
    assert db.agent_is_alive("terminated", future) is False
    assert db.agent_is_alive("restarting", future) is False


def test_signal_live_agents_restart_none_live(db_conn: psycopg.Connection) -> None:
    """No live agents → no inbound inserted, returns []."""
    terminated = _seed_agent(db_conn, "terminated")
    assert db.signal_live_agents_restart(source="system:update") == []
    assert _inbound_rows(db_conn, terminated) == []


def test_signal_live_agents_restart_exclude_ids(db_conn: psycopg.Connection) -> None:
    """exclude_agent_ids agents are skipped even when live — the quiesce
    convergence loop passes its already-signalled set so a pass only signals
    newly-live agents."""
    already = _seed_agent(db_conn, "running")
    late = _seed_agent(db_conn, "running")

    ids = db.signal_live_agents_restart(source="system:update", exclude_agent_ids={already})

    assert ids == [late]
    assert _inbound_rows(db_conn, already) == []
    assert _inbound_rows(db_conn, late) == [("restart", "system:update", "")]


def test_list_live_agent_ids(db_conn: psycopg.Connection) -> None:
    """list_live_agent_ids lists agents with a LIVE process to act on (quiesce):
    running/idling only."""
    running = _seed_agent(db_conn, "running")
    idling = _seed_agent(db_conn, "idling")
    _seed_agent(db_conn, "terminated")
    assert sorted(db.list_live_agent_ids()) == sorted([running, idling])


async def test_signal_live_agents_restart_publishes_redis_wake(
    db_conn: psycopg.Connection,
) -> None:
    """The bulk restart wakes each signalled agent over Redis — the same
    per-agent publish as insert_inbound_message — so an idling agent restarts now
    instead of stalling to its SELECT recheck (the quiesce step's convergence
    depends on live agents draining promptly). Park a per-agent listener on the
    agent's channel, fire the bulk signal, assert the parked wait wakes."""
    tid = _seed_agent(db_conn, "idling")
    listener = RedisInboundListener(settings.data_plane.redis_url, tid)
    try:
        wait_task = asyncio.create_task(listener.wait_one(timeout=10.0))
        await asyncio.sleep(0.2)  # let the subscribe take effect before the publish
        t0 = time.monotonic()
        ids = await asyncio.to_thread(db.signal_live_agents_restart, source="system:update")
        assert tid in ids
        await asyncio.wait_for(wait_task, timeout=5.0)
        assert time.monotonic() - t0 < 5.0, "bulk restart did not wake the parked listener"
    finally:
        await listener.close()


def test_pool_check_connections_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """`check_connections=True` arms the checkout-time dead-connection check
    (server-closed idle conns are discarded and replaced before a borrow is
    handed out — Task #1027); the default leaves it off so hot pools do not pay
    a round trip per checkout."""
    real_check = db.ConnectionPool.check_connection
    captured: dict[str, object] = {}

    class _FakePool:
        check_connection = real_check

        def __init__(self, *_a: object, **_kw: object) -> None:
            captured.update(_kw)

    monkeypatch.setattr(db, "ConnectionPool", _FakePool)
    db.pool(check_connections=True)
    assert captured.get("check") is real_check
    captured.clear()
    db.pool()
    assert captured.get("check") is None
