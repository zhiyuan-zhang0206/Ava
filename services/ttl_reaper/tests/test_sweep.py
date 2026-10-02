"""The reaper's database-only sweep against the real Postgres fixture: page rows
are terminalized (expired_at, never closed_at), unexpired/closed rows are left
alone, owners are notified only when running/idling, expired browser sessions
and notices are reclaimed in bounded batches, the fire-log prune keeps the
newest claim per schedule, and the lifecycle-pointer scans report and settle
their rows."""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.daemon.loop_health import LoopProgress
from base.db import create_agent
from services.ttl_reaper import lifecycle_fences, sweep
from services.ttl_reaper.sweep import (
    _reap_expired_notices_blocking,
    _reap_expired_pages_blocking,
    _reap_expired_web_sessions_blocking,
)


@pytest.fixture()
def reaper_pool() -> Iterator[ConnectionPool]:
    """The reaper's own small pool (the functions take a ConnectionPool)."""
    import base.db

    pool = base.db.pool(max_size=2)
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


def _capture_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[tuple[object, ...], dict[str, object]]]:
    """Capture telemetry emits, asserting every name is registered (mirrors the
    production emit contract, like the torn-pointer test does inline)."""
    emitted: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def _capture_emit(*args: object, **kwargs: object) -> None:
        from base.events.contract import EVENTS

        assert len(args) > 1 and args[1] in EVENTS, f"unregistered emit name: {args!r}"
        emitted.append((args, kwargs))

    monkeypatch.setattr(lifecycle_fences.telemetry, "emit", _capture_emit)
    return emitted


def _stuck_force_fence(conn: psycopg.Connection, *, machine: str, applied: bool = True) -> int:
    """A terminated agent whose force-terminate fence is stuck: claimed
    (+applied), unobserved, with ``lifecycle_command_id`` still pointing at it
    — the shape a decommissioned machine leaves behind (task #4143)."""
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
            " CASE WHEN %s THEN now() ELSE NULL END, %s, %s) RETURNING id",
            (aid, applied, gen, owner),
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

    assert _reap_expired_web_sessions_blocking(reaper_pool) == 1

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

    assert _reap_expired_web_sessions_blocking(reaper_pool) == 1

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM web_sessions WHERE id = ANY(%s) ORDER BY id",
            (["older-expired-session", "newer-expired-session", "live-session"],),
        )
        assert [row[0] for row in cur.fetchall()] == ["live-session", "newer-expired-session"]


def test_reap_expired_pages_terminalizes_only_past_deadlines(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool
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

    reaped = _reap_expired_pages_blocking(reaper_pool)

    assert [(aid, "expired")] == [(a, n) for a, n, _i in reaped]
    expired_closed, expired_marked = _page_state(db_conn, aid, "expired")
    assert expired_closed is None and expired_marked is not None
    future_closed, future_marked = _page_state(db_conn, aid, "future")
    assert future_closed is None and future_marked is None
    closed_closed, closed_marked = _page_state(db_conn, aid, "closed")
    assert closed_closed is not None and closed_marked is None


def test_reap_expired_pages_skips_already_terminal(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool
) -> None:
    aid = _running_agent(db_conn)
    _open_page(db_conn, aid, "expired", expires_at=datetime.now(UTC) - timedelta(seconds=5))
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_pages SET expired_at = now() WHERE agent_id = %s AND name = 'expired'",
            (aid,),
        )
    db_conn.commit()

    assert _reap_expired_pages_blocking(reaper_pool) == []


def test_reap_expired_pages_notifies_only_live_agents(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool
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

    _reap_expired_pages_blocking(reaper_pool)

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


def test_prune_schedule_fire_log_respects_retention_and_keeps_newest_per_schedule(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool
) -> None:
    """The retention prune deletes only rows past the window, and always keeps
    the newest claim per schedule — a sparse cron (all claims past the window)
    must not lose its baseline, or catch-up could refire an already-claimed
    slot."""
    s1 = _new_schedule(db_conn, "fire-log-s1")
    _insert_fire_log(db_conn, s1, [40 * 24 + 2, 40 * 24 + 1, 40 * 24, 1])
    s2 = _new_schedule(db_conn, "fire-log-s2")
    _insert_fire_log(db_conn, s2, [50 * 24, 45 * 24])
    db_conn.commit()

    pruned = sweep._prune_schedule_fire_log_blocking(reaper_pool)

    assert pruned == 4
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT schedule_id, slot_fire_at FROM schedule_fire_log "
            "ORDER BY schedule_id, slot_fire_at"
        )
        rows = cur.fetchall()
    # s1: only its 1-hour-old claim remains; s2: only its newest (45d) claim
    # remains — the oldest-ever claim per schedule is never pruned away.
    assert len(rows) == 2
    assert rows[0][0] == s1 and rows[0][1] > datetime.now(UTC) - timedelta(hours=2)
    assert rows[1][0] == s2 and rows[1][1] > datetime.now(UTC) - timedelta(days=46)


def test_prune_schedule_fire_log_is_bounded_per_pass(
    db_conn: psycopg.Connection,
    reaper_pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One pass deletes at most _FIRE_LOG_PASS_BATCH rows; the backlog drains
    over successive passes (and the newest claim is never a candidate)."""
    monkeypatch.setattr(sweep, "_FIRE_LOG_PASS_BATCH", 2)
    s1 = _new_schedule(db_conn, "fire-log-bounded")
    _insert_fire_log(db_conn, s1, [50 * 24, 49 * 24, 48 * 24, 47 * 24, 46 * 24])
    db_conn.commit()

    assert sweep._prune_schedule_fire_log_blocking(reaper_pool) == 2
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM schedule_fire_log WHERE schedule_id = %s", (s1,))
        remaining = cur.fetchone()
        assert remaining is not None and remaining[0] == 3

    assert sweep._prune_schedule_fire_log_blocking(reaper_pool) == 2
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM schedule_fire_log WHERE schedule_id = %s", (s1,))
        # the newest claim is retained as the catch-up baseline
        remaining = cur.fetchone()
        assert remaining is not None and remaining[0] == 1


def test_torn_pointer_scan_reports_torn_commands(
    db_conn: psycopg.Connection,
    reaper_pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`done` with a live pointer is counted and alerted; a cleared pointer is quiet."""
    # (args, kwargs) pairs, so the assertions stay fully typed.
    emitted: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def _capture_emit(*args: object, **kwargs: object) -> None:
        # Mirror the production emit() contract: an unregistered name raises
        # there, so a stub that accepts one would hide exactly that class of
        # drift (PR #2667 review).
        from base.events.contract import EVENTS

        assert len(args) > 1 and args[1] in EVENTS, f"unregistered emit name: {args!r}"
        emitted.append((args, kwargs))

    monkeypatch.setattr(lifecycle_fences.telemetry, "emit", _capture_emit)
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

    assert lifecycle_fences._scan_torn_lifecycle_pointers_blocking(reaper_pool) == 1
    assert emitted
    assert emitted[-1][0][1] == "lifecycle_pointer_done_torn"

    db_conn.execute("UPDATE agents_meta SET lifecycle_command_id=NULL WHERE id=%s", (agent_id,))
    db_conn.commit()
    assert lifecycle_fences._scan_torn_lifecycle_pointers_blocking(reaper_pool) == 0


def test_settle_absent_machine_fences_settles_stuck_force(
    db_conn: psycopg.Connection,
    reaper_pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task #4143: a decommissioned machine never runs the boot recovery that
    observes its agents' force fences, so the reaper settles them to the exact
    transition ``recover_orphaned_hosted_forces`` would have written —
    observed + done, pointer and lease cleared — and reports the event."""
    emitted = _capture_telemetry(monkeypatch)
    aid = _stuck_force_fence(db_conn, machine="ghost")

    settled = lifecycle_fences.settle_absent_machine_fences(reaper_pool, batch=50)

    assert settled == [aid]
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT i.status, i.observed_at IS NOT NULL, m.lifecycle_command_id IS NULL, "
            "m.lease_expires_at IS NULL FROM agents_meta m "
            "JOIN inbound_messages i ON i.agent_id = m.id AND i.kind = 'terminate' "
            "WHERE m.id = %s",
            (aid,),
        )
        row = cur.fetchone()
    assert row == ("done", True, True, True)
    assert emitted and emitted[-1][0][1] == "lifecycle_fences_settled_absent_machine"
    assert emitted[-1][1]["attributes"] == {"count": 1, "samples": str(aid)}
    # Idempotent: once settled, the scan is quiet.
    assert lifecycle_fences.settle_absent_machine_fences(reaper_pool, batch=50) == []


def test_settle_absent_machine_fences_leaves_registered_and_unapplied(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool
) -> None:
    """Only absent machines are terminalized, and only applied commands:
    a registered machine's boot recovery owns its fences, and an unapplied
    command has no observation evidence to write (never fabricate one)."""
    registered = _stuck_force_fence(db_conn, machine="macmini")
    unapplied = _stuck_force_fence(db_conn, machine="ghost", applied=False)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO machines (name, role, gateway_url) "
            "VALUES ('macmini', '{agent-runner}', 'http://macmini:8106')"
        )
    db_conn.commit()

    assert lifecycle_fences.settle_absent_machine_fences(reaper_pool, batch=50) == []
    with db_conn.cursor() as cur:
        for aid in (registered, unapplied):
            cur.execute(
                "SELECT i.status, i.observed_at IS NULL, m.lifecycle_command_id IS NOT NULL "
                "FROM agents_meta m JOIN inbound_messages i "
                "ON i.agent_id = m.id AND i.kind = 'terminate' WHERE m.id = %s",
                (aid,),
            )
            assert cur.fetchone() == ("claimed", True, True)


def test_reap_expired_notices_resolves_and_notifies_live_agent(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool
) -> None:
    """Notices past expire_at are resolved with resolution='expired', and a system note is sent to live owner."""
    aid = _running_agent(db_conn)
    with db_conn.cursor() as cur:
        # One expired notice
        cur.execute(
            "INSERT INTO agent_notices (agent_id, local_id, title, priority, require_response, blocking, expire_at) "
            "VALUES (%s, 0, 'expired notice', 'P1', TRUE, FALSE, now() - interval '1 minute') RETURNING id",
            (aid,),
        )
        row = cur.fetchone()
        assert row is not None
        nid = row[0]
        # One unexpired notice
        cur.execute(
            "INSERT INTO agent_notices (agent_id, local_id, title, priority, require_response, blocking, expire_at) "
            "VALUES (%s, 1, 'active notice', 'P2', FALSE, FALSE, now() + interval '1 hour')",
            (aid,),
        )
    db_conn.commit()

    reaped = _reap_expired_notices_blocking(reaper_pool)
    assert reaped == [(aid, nid)]

    # Check notice state in DB
    with db_conn.cursor() as cur:
        cur.execute("SELECT resolution, resolved_at FROM agent_notices WHERE id = %s", (nid,))
        row = cur.fetchone()
        assert row is not None
        res, resolved_at = row
    assert res == "expired"
    assert resolved_at is not None

    # Check inbound message delivered
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT source, content FROM inbound_messages WHERE agent_id = %s",
            (aid,),
        )
        inbounds = cur.fetchall()
    assert len(inbounds) == 1
    assert inbounds[0][0] == "system:notice-expire"
    assert "expired notice" in inbounds[0][1]
    assert "[This notice has expired.]" in inbounds[0][1]


@pytest.mark.parametrize("require_response", [False, True])
def test_reap_expired_notices_does_not_resurrect_terminated_agent(
    db_conn: psycopg.Connection,
    reaper_pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    require_response: bool,
) -> None:
    """Terminated agents have their notice closed as expired, but no inbound message is delivered."""
    aid = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', 'terminated')",
            (aid,),
        )
        cur.execute(
            "INSERT INTO agent_notices (agent_id, local_id, title, priority, require_response, blocking, expire_at) "
            "VALUES (%s, 0, 'dead agent notice', 'P2', %s, FALSE, now() - interval '1 minute') RETURNING id",
            (aid, require_response),
        )
        row = cur.fetchone()
        assert row is not None
        nid = row[0]
    db_conn.commit()

    observed: list[tuple[int, str | None]] = []

    def capture_hint(agent_id: int) -> None:
        # A different connection must see the expiry before its hint can fire.
        with db_conn.cursor() as cur:
            cur.execute("SELECT resolution FROM agent_notices WHERE id = %s", (nid,))
            row = cur.fetchone()
        assert row is not None
        observed.append((agent_id, row[0]))

    monkeypatch.setattr(sweep, "publish_agent_updated_sync", capture_hint)
    reaped = _reap_expired_notices_blocking(reaper_pool)
    assert reaped == [(aid, nid)]
    assert observed == ([(aid, "expired")] if require_response else [])

    with db_conn.cursor() as cur:
        cur.execute("SELECT resolution FROM agent_notices WHERE id = %s", (nid,))
        row = cur.fetchone()
        assert row is not None and row[0] == "expired"
        cur.execute("SELECT count(*) FROM inbound_messages WHERE agent_id = %s", (aid,))
        row = cur.fetchone()
        assert row is not None and row[0] == 0


# ─────────────── shell TTL renewal vs the reaper (task #2647) ───────────────


async def test_a_sweep_round_runs_every_database_phase(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool
) -> None:
    """The round wires the phases together: one pass reclaims an expired page, an
    expired browser session and an expired notice, and beats its progress."""
    aid = _running_agent(db_conn)
    _open_page(db_conn, aid, "expired", expires_at=datetime.now(UTC) - timedelta(seconds=5))
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO web_sessions (id, expires_at) VALUES ('gone', now() - interval '1 second')"
        )
        cur.execute(
            "INSERT INTO agent_notices "
            "(agent_id, local_id, title, priority, require_response, blocking, expire_at) "
            "VALUES (%s, 0, 'old', 'P2', FALSE, FALSE, now() - interval '1 second')",
            (aid,),
        )
    db_conn.commit()
    progress = LoopProgress("test", 60.0)

    await sweep.sweep_round(reaper_pool, progress)

    assert _page_state(db_conn, aid, "expired")[1] is not None
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM web_sessions")
        assert cur.fetchone() == (0,)
        cur.execute("SELECT resolution FROM agent_notices WHERE agent_id = %s", (aid,))
        assert cur.fetchone() == ("expired",)


async def test_a_sweep_round_runs_the_impersonation_seal_stuck_alert_pass(
    reaper_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seal-stuck monitor rides the sweep round, after the lease reap."""
    order: list[str] = []

    def reap(_pool: object) -> int:
        order.append("reap")
        return 0

    def alert(_pool: object) -> int:
        order.append("alert")
        return 0

    monkeypatch.setattr(sweep, "reap_impersonations", reap)
    monkeypatch.setattr(sweep, "alert_stuck_event_logs", alert)

    await sweep.sweep_round(reaper_pool, LoopProgress("test", 60.0))

    assert order == ["reap", "alert"]
