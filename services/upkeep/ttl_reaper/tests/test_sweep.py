"""The reaper's database-only sweep against the real Postgres fixture: page rows
are terminalized (expired_at, never closed_at), unexpired/closed rows are left
alone, owners are notified only when running/idling, expired browser sessions
and notices are reclaimed in bounded batches, the fire-log prune deletes every
claim past the window, and the stuck lifecycle-pointer scan reports (and never
settles) its rows."""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from services.upkeep.ttl_reaper import lifecycle_fences, sweep
from services.upkeep.ttl_reaper.sweep import (
    reap_expired_pages_blocking,
    reap_expired_web_sessions_blocking,
)


@pytest.fixture()
def reaper_pool(db_url: str, *, database_gate: ProcessDbGate) -> Iterator[ConnectionPool]:
    """The reaper's own small pool (the functions take a ConnectionPool)."""
    import base.db

    pool = base.db.pool(max_size=2, gate=database_gate)
    yield pool
    pool.close()


# One distinct port per row: an open (host, port) pair is unique since
# agent_pages_unique_live_port (20260920) — the reaper never cares about the
# port value, and a shared fixed port now collides between pages.
_PAGE_PORTS = itertools.count(8001)


def _open_page(
    conn: psycopg.Connection,
    agent_id: int,
    name: str,
    *,
    expires_at: datetime | None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_pages (agent_id, name, port, host, expires_at) "
            "VALUES (%s, %s, %s, '127.0.0.1', %s) RETURNING id",
            (agent_id, name, next(_PAGE_PORTS), expires_at),
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return row[0]


def _page_state(
    conn: psycopg.Connection, agent_id: int, name: str
) -> tuple[datetime | None, datetime | None]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT closed_at, expired_at FROM agent_pages WHERE agent_id = %s AND name = %s",
            (agent_id, name),
        )
        row = cur.fetchone()
    assert row is not None
    return row[0], row[1]


def _running_agent(conn: psycopg.Connection) -> int:
    aid = create_agent(conn)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', 'running')",
            (aid,),
        )
    conn.commit()
    return aid


def _system_inbounds(conn: psycopg.Connection, agent_id: int) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT content FROM inbound_messages WHERE agent_id = %s AND source = 'system'",
            (agent_id,),
        )
        return [r[0] for r in cur.fetchall()]


def _stuck_force_fence(
    conn: psycopg.Connection, *, machine: str, applied_ago: str | None = "2 hours"
) -> int:
    """A terminated agent whose force-terminate fence was applied `applied_ago`
    and never observed, with ``lifecycle_command_id`` still pointing at it — the
    shape an original host that died before observing it (or a decommissioned
    machine) leaves behind. ``applied_ago=None`` makes the command unapplied."""
    aid = _running_agent(conn)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status='terminated', termination_source='user', "
            "machine=%s, runtime_generation=gen_random_uuid(), "
            "runtime_owner=gen_random_uuid(), lease_expires_at=now() + interval '1 hour' "
            "WHERE id=%s RETURNING runtime_generation, runtime_owner",
            (machine, aid),
        )
        gen, owner = cast(tuple[Any, Any], cur.fetchone())
        cur.execute(
            "INSERT INTO inbound_messages "
            "(agent_id, content, kind, source, status, claimed_at, applied_at, "
            " target_generation, target_owner) "
            "VALUES (%s, '', 'terminate', 'machine-pause', 'claimed', now(), "
            " CASE WHEN %s::text IS NULL THEN NULL ELSE now() - %s::text::interval END, %s, %s) "
            "RETURNING id",
            (aid, applied_ago, applied_ago, gen, owner),
        )
        row = cur.fetchone()
        assert row is not None
        cur.execute("UPDATE agents_meta SET lifecycle_command_id=%s WHERE id=%s", (row[0], aid))
    conn.commit()
    return aid


def test_reap_expired_web_sessions_removes_only_expired_rows(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool
) -> None:
    """The periodic pass, not login traffic, reclaims expired browser sessions."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO web_sessions (id, expires_at) VALUES (%s, now() - interval '1 second')",
            ("expired-session",),
        )
        cur.execute(
            "INSERT INTO web_sessions (id, expires_at) VALUES (%s, now() + interval '1 hour')",
            ("live-session",),
        )
    db_conn.commit()

    assert reap_expired_web_sessions_blocking(reaper_pool) == 1

    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM web_sessions ORDER BY id")
        assert [row[0] for row in cur.fetchall()] == ["live-session"]


def test_reap_expired_web_sessions_is_limited_to_one_pass_batch(
    db_conn: psycopg.Connection,
    reaper_pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A large expired-session backlog is reclaimed across bounded transactions."""
    monkeypatch.setattr(sweep, "PASS_BATCH", 1)
    with db_conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO web_sessions (id, expires_at) VALUES (%s, now() + %s::interval)",
            [
                ("older-expired-session", "-2 seconds"),
                ("newer-expired-session", "-1 second"),
                ("live-session", "1 hour"),
            ],
        )
    db_conn.commit()

    assert reap_expired_web_sessions_blocking(reaper_pool) == 1

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM web_sessions WHERE id = ANY(%s) ORDER BY id",
            (["older-expired-session", "newer-expired-session", "live-session"],),
        )
        assert [row[0] for row in cur.fetchall()] == ["live-session", "newer-expired-session"]


def test_reap_expired_pages_terminalizes_only_past_deadlines(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, database: Database
) -> None:
    aid = _running_agent(db_conn)
    _open_page(db_conn, aid, "expired", expires_at=datetime.now(UTC) - timedelta(seconds=5))
    _open_page(db_conn, aid, "future", expires_at=datetime.now(UTC) + timedelta(hours=1))
    closed_id = _open_page(
        db_conn, aid, "closed", expires_at=datetime.now(UTC) - timedelta(seconds=5)
    )
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agent_pages SET closed_at = now() WHERE id = %s", (closed_id,))
    db_conn.commit()

    reaped = reap_expired_pages_blocking(reaper_pool, database, EventBus.from_settings())

    assert [(aid, "expired")] == [(a, n) for a, n, _i in reaped]
    expired_closed, expired_marked = _page_state(db_conn, aid, "expired")
    assert expired_closed is None and expired_marked is not None
    future_closed, future_marked = _page_state(db_conn, aid, "future")
    assert future_closed is None and future_marked is None
    closed_closed, closed_marked = _page_state(db_conn, aid, "closed")
    assert closed_closed is not None and closed_marked is None


def test_reap_expired_pages_skips_already_terminal(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, database: Database
) -> None:
    aid = _running_agent(db_conn)
    _open_page(db_conn, aid, "expired", expires_at=datetime.now(UTC) - timedelta(seconds=5))
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_pages SET expired_at = now() WHERE agent_id = %s AND name = 'expired'",
            (aid,),
        )
    db_conn.commit()

    assert reap_expired_pages_blocking(reaper_pool, database, EventBus.from_settings()) == []


def test_reap_expired_pages_notifies_only_live_agents(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, database: Database
) -> None:
    live = _running_agent(db_conn)
    dead = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', 'terminated')",
            (dead,),
        )
    db_conn.commit()
    _open_page(db_conn, live, "live-page", expires_at=datetime.now(UTC) - timedelta(seconds=5))
    _open_page(db_conn, dead, "dead-page", expires_at=datetime.now(UTC) - timedelta(seconds=5))

    reap_expired_pages_blocking(reaper_pool, database, EventBus.from_settings())

    live_msgs = _system_inbounds(db_conn, live)
    assert len(live_msgs) == 1
    assert "reclaimed after its TTL" in live_msgs[0]
    assert _system_inbounds(db_conn, dead) == []


def _new_schedule(conn: psycopg.Connection, name: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schedules (name, script, command) VALUES (%s, 'x', 'x') RETURNING id",
            (name,),
        )
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


def _insert_fire_log(conn: psycopg.Connection, schedule_id: int, age_hours: list[int]) -> None:
    with conn.cursor() as cur:
        for hours in age_hours:
            cur.execute(
                "INSERT INTO schedule_fire_log (schedule_id, slot_fire_at) "
                "VALUES (%s, now() - %s * interval '1 hour')",
                (schedule_id, hours),
            )


def test_prune_schedule_fire_log_deletes_every_row_past_the_window(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool
) -> None:
    """The retention prune deletes every claim past the window, including the newest
    claim of a schedule whose whole log is older than it; rows inside the window stay."""
    s1 = _new_schedule(db_conn, "fire-log-s1")
    _insert_fire_log(db_conn, s1, [40 * 24 + 2, 40 * 24 + 1, 40 * 24, 1])
    s2 = _new_schedule(db_conn, "fire-log-s2")
    _insert_fire_log(db_conn, s2, [50 * 24, 45 * 24])
    db_conn.commit()

    pruned = sweep.prune_schedule_fire_log_blocking(reaper_pool)

    assert pruned == 5
    with db_conn.cursor() as cur:
        cur.execute("SELECT schedule_id, slot_fire_at FROM schedule_fire_log")
        rows = cur.fetchall()
    # Only s1's 1-hour-old claim is inside the window; s2 keeps nothing.
    assert len(rows) == 1
    assert rows[0][0] == s1 and rows[0][1] > datetime.now(UTC) - timedelta(hours=2)


def test_prune_schedule_fire_log_is_bounded_per_pass(
    db_conn: psycopg.Connection,
    reaper_pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One pass deletes at most FIRE_LOG_PASS_BATCH rows; the backlog drains
    over successive passes."""
    monkeypatch.setattr(sweep, "FIRE_LOG_PASS_BATCH", 2)
    s1 = _new_schedule(db_conn, "fire-log-bounded")
    _insert_fire_log(db_conn, s1, [50 * 24, 49 * 24, 48 * 24, 47 * 24, 46 * 24])
    db_conn.commit()

    assert sweep.prune_schedule_fire_log_blocking(reaper_pool) == 2
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM schedule_fire_log WHERE schedule_id = %s", (s1,))
        remaining = cur.fetchone()
        assert remaining is not None and remaining[0] == 3

    assert sweep.prune_schedule_fire_log_blocking(reaper_pool) == 2
    assert sweep.prune_schedule_fire_log_blocking(reaper_pool) == 1
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM schedule_fire_log WHERE schedule_id = %s", (s1,))
        remaining = cur.fetchone()
        assert remaining is not None and remaining[0] == 0


def test_stuck_pointer_scan_reports_torn_commands(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, emitted_events: EmittedEvents
) -> None:
    """`done` with a live pointer is counted and alerted; a cleared pointer is quiet."""
    agent_id = _running_agent(db_conn)
    row = db_conn.execute(
        "INSERT INTO inbound_messages "
        "(agent_id, content, kind, source, status, applied_at, observed_at, "
        " claimed_at, target_generation, target_owner) "
        "VALUES (%s, '', 'terminate', 'user', 'done', now(), NULL, now(), "
        "gen_random_uuid(), gen_random_uuid()) RETURNING id",
        (agent_id,),
    ).fetchone()
    assert row is not None
    db_conn.execute(
        "UPDATE agents_meta SET lifecycle_command_id=%s WHERE id=%s", (row[0], agent_id)
    )
    db_conn.commit()

    assert lifecycle_fences.scan_stuck_lifecycle_pointers_blocking(reaper_pool) == 1
    assert emitted_events.names()[-1] == "lifecycle_pointer_stuck"

    db_conn.execute("UPDATE agents_meta SET lifecycle_command_id=NULL WHERE id=%s", (agent_id,))
    db_conn.commit()
    assert lifecycle_fences.scan_stuck_lifecycle_pointers_blocking(reaper_pool) == 0


def test_a_force_nobody_observed_is_alerted_not_settled(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, emitted_events: EmittedEvents
) -> None:
    """An applied force whose original host died, or whose machine was
    decommissioned, is never settled on a guess: the pointer stays set and the
    scan alerts so an operator settles it."""
    aid = _stuck_force_fence(db_conn, machine="ghost")

    assert lifecycle_fences.scan_stuck_lifecycle_pointers_blocking(reaper_pool) == 1

    assert emitted_events.names()[-1] == "lifecycle_pointer_stuck"
    assert emitted_events.attributes("lifecycle_pointer_stuck")[-1] == {
        "count": 1,
        "samples": str(aid),
    }
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT i.status, i.observed_at IS NULL, m.lifecycle_command_id IS NOT NULL "
            "FROM agents_meta m JOIN inbound_messages i "
            "ON i.agent_id = m.id AND i.kind = 'terminate' WHERE m.id = %s",
            (aid,),
        )
        assert cur.fetchone() == ("claimed", True, True)


def test_a_fresh_or_unapplied_force_is_not_alerted(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, emitted_events: EmittedEvents
) -> None:
    """A live host observes a just-applied force within moments, and an unapplied
    command is not a fence: neither is stuck."""
    _stuck_force_fence(db_conn, machine="macmini", applied_ago="30 seconds")
    _stuck_force_fence(db_conn, machine="macmini", applied_ago=None)

    assert lifecycle_fences.scan_stuck_lifecycle_pointers_blocking(reaper_pool) == 0
    assert len(emitted_events) == 0
