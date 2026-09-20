"""Gateway TTL reaper — pages and TTL-tracked shells past their deadline.

Covers the reclaim pass against the real Postgres fixture: page rows are
terminalized (expired_at, never closed_at), unexpired/closed rows are left
alone, PageClosed is published, owners are notified only when running/idling,
and expired shell rows dispatch a shell_kill op and are deleted only on a
definitive verdict (killed / absent) — unreachable machines and failed ops
leave the row for the next pass. Shell owners are notified only when the reap
interrupted a running job; an idle or already-absent shell's reaping is silent.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from gateway import ttl_reaper
from gateway.ttl_reaper import (
    _PASS_BATCH,
    _SHELL_KILL_TIMEOUT_S,
    _claim_shell_row_still_expired,
    _reap_expired_notices_blocking,
    _reap_expired_pages_blocking,
    _reap_expired_shells,
    _reap_expired_web_sessions_blocking,
    _reaper_loop,
)
from gateway.watcher_ttl import reap_terminated_owner_watchers
from ops.rpc_schemas import ShellKillResult
from shared.config import settings
from shared.db import create_agent


@pytest.fixture()
def reaper_pool() -> Iterator[ConnectionPool]:
    """The reaper's own small pool (the functions take a ConnectionPool)."""
    import shared.db

    pool = shared.db.pool(max_size=2)
    yield pool
    pool.close()


def _empty_page_reap(_pool: ConnectionPool) -> list[tuple[int, str, int]]:
    return []


async def _reap_terminated_watchers(reaper_pool: ConnectionPool) -> list[tuple[int, int]]:
    """The terminated-owner watcher pass with the reaper loop's run parameters."""
    return await reap_terminated_owner_watchers(
        reaper_pool, timeout_s=_SHELL_KILL_TIMEOUT_S, batch=_PASS_BATCH
    )


def _empty_web_session_reap(_pool: ConnectionPool) -> int:
    return 0


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


def test_reap_expired_web_sessions_removes_only_expired_rows(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool
) -> None:
    """The periodic gateway pass, not login traffic, reclaims expired browser sessions."""
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
    monkeypatch.setattr(ttl_reaper, "_PASS_BATCH", 1)
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

    pruned = ttl_reaper._prune_schedule_fire_log_blocking(reaper_pool)

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
    monkeypatch.setattr(ttl_reaper, "_FIRE_LOG_PASS_BATCH", 2)
    s1 = _new_schedule(db_conn, "fire-log-bounded")
    _insert_fire_log(db_conn, s1, [50 * 24, 49 * 24, 48 * 24, 47 * 24, 46 * 24])
    db_conn.commit()

    assert ttl_reaper._prune_schedule_fire_log_blocking(reaper_pool) == 2
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM schedule_fire_log WHERE schedule_id = %s", (s1,))
        remaining = cur.fetchone()
        assert remaining is not None and remaining[0] == 3

    assert ttl_reaper._prune_schedule_fire_log_blocking(reaper_pool) == 2
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM schedule_fire_log WHERE schedule_id = %s", (s1,))
        # the newest claim is retained as the catch-up baseline
        remaining = cur.fetchone()
        assert remaining is not None and remaining[0] == 1


def test_schedule_fire_log_prune_due_throttles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prune fires on the first pass, then only after the cleanup interval."""
    monkeypatch.setattr(ttl_reaper, "_schedule_fire_log_last_pruned", None)
    monkeypatch.setattr(settings.daemon, "schedule_fire_log_cleanup_interval_seconds", 3600.0)
    assert ttl_reaper._schedule_fire_log_prune_due() is True
    assert ttl_reaper._schedule_fire_log_prune_due() is False
    monkeypatch.setattr(ttl_reaper, "_schedule_fire_log_last_pruned", time.monotonic() - 7200.0)
    assert ttl_reaper._schedule_fire_log_prune_due() is True


def test_torn_pointer_scan_due_throttles(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scan fires on the first pass, then only after the scan interval."""
    monkeypatch.setattr(ttl_reaper, "_torn_pointer_last_scan", None)
    assert ttl_reaper._torn_pointer_scan_due() is True
    assert ttl_reaper._torn_pointer_scan_due() is False
    monkeypatch.setattr(
        ttl_reaper,
        "_torn_pointer_last_scan",
        time.monotonic() - 2 * ttl_reaper._TORN_POINTER_SCAN_INTERVAL_S,
    )
    assert ttl_reaper._torn_pointer_scan_due() is True


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
        from shared.events.contract import EVENTS

        assert len(args) > 1 and args[1] in EVENTS, f"unregistered emit name: {args!r}"
        emitted.append((args, kwargs))

    monkeypatch.setattr(ttl_reaper.telemetry, "emit", _capture_emit)
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

    assert ttl_reaper._scan_torn_lifecycle_pointers_blocking(reaper_pool) == 1
    assert emitted
    assert emitted[-1][0][1] == "lifecycle_pointer_done_torn"

    db_conn.execute("UPDATE agents_meta SET lifecycle_command_id=NULL WHERE id=%s", (agent_id,))
    db_conn.commit()
    assert ttl_reaper._scan_torn_lifecycle_pointers_blocking(reaper_pool) == 0


async def test_reaper_reconciles_stale_work_failures_on_its_startup_pass(
    reaper_pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()
    calls: list[ConnectionPool] = []

    monkeypatch.setattr(ttl_reaper, "_reap_expired_pages_blocking", _empty_page_reap)
    monkeypatch.setattr(ttl_reaper, "_reap_expired_shells", _empty_shell_reap)
    monkeypatch.setattr(
        ttl_reaper,
        "_reap_expired_web_sessions_blocking",
        _empty_web_session_reap,
    )

    async def _reconcile(pool: ConnectionPool) -> int:
        calls.append(pool)
        stop.set()
        return 0

    monkeypatch.setattr(
        ttl_reaper.work_failed_router,
        "reconcile_stale_work_failures",
        _reconcile,
    )

    reaper_task = asyncio.create_task(_reaper_loop(reaper_pool, stop))
    done, _ = await asyncio.wait([reaper_task], timeout=0.2)
    if reaper_task not in done:
        stop.set()
        await reaper_task

    assert calls == [reaper_pool]


async def _empty_shell_reap(
    _pool: ConnectionPool, _stop: asyncio.Event | None = None
) -> list[tuple[int, int]]:
    return []


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


async def test_reap_expired_shells_deletes_on_killed(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = _running_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at, created_at) "
            "VALUES (%s, 3, now() - interval '1 minute', now() - interval '1 hour 1 minute')",
            (aid,),
        )
        cur.execute("UPDATE agents_meta SET machine = 'macmini' WHERE id = %s", (aid,))
    db_conn.commit()

    async def _dispatch(
        machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        assert machine == "macmini"
        assert kind == "shell_kill"
        assert payload == {"agent_id": aid, "session_id": 3}
        return ShellKillResult(mode="killed", interrupted=True, name="build").model_dump()

    monkeypatch.setattr(
        ttl_reaper.cluster_rpc,
        "dispatch_to_machine",
        _dispatch,
    )
    reaped = await _reap_expired_shells(reaper_pool)

    assert reaped == [(aid, 3)]
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agent_shell_ttls WHERE agent_id = %s", (aid,))
        row = cur.fetchone()
        assert row is not None and row[0] == 0
    msgs = _system_inbounds(db_conn, aid)
    assert len(msgs) == 1
    assert "Shell session 'build' (id 3, agent" in msgs[0]
    assert "expired at " in msgs[0] and "TTL 1h" in msgs[0], msgs[0]
    assert "interrupting a running task" in msgs[0]


async def test_reap_expired_shells_keeps_row_on_unreachable(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = _running_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at) "
            "VALUES (%s, 7, now() - interval '1 minute')",
            (aid,),
        )
        cur.execute("UPDATE agents_meta SET machine = 'macmini' WHERE id = %s", (aid,))
    db_conn.commit()

    async def _dispatch(
        machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        raise ttl_reaper.cluster_rpc.ClusterOpUnreachable("boom")

    monkeypatch.setattr(
        ttl_reaper.cluster_rpc,
        "dispatch_to_machine",
        _dispatch,
    )
    reaped = await _reap_expired_shells(reaper_pool)

    assert reaped == []
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agent_shell_ttls WHERE agent_id = %s", (aid,))
        row = cur.fetchone()
        assert row is not None and row[0] == 1


async def test_reap_expired_shells_kills_watcher_past_deadline(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One lifecycle (task #3411): a watcher session whose deadline passed is
    reclaimed like any other — no registry skip — and its still-running row
    is marked `reaped` so no later boot rebuilds it. The shell-shaped
    interruption notice is suppressed (the reclaim ends a scheduled window,
    not a user task)."""
    aid = _running_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at, created_at) "
            "VALUES (%s, 21, now() - interval '1 minute', now() - interval '1 hour 1 minute')",
            (aid,),
        )
        cur.execute(
            "INSERT INTO agent_watchers (agent_id, session_id, kind, name, status, cron_end_at) "
            "VALUES (%s, 21, 'cron', 'escalation-check', 'running', now() - interval '1 minute')",
            (aid,),
        )
        cur.execute("UPDATE agents_meta SET machine = 'macmini' WHERE id = %s", (aid,))
    db_conn.commit()

    dispatched: list[tuple[str, object]] = []

    async def _dispatch(
        machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        dispatched.append((kind, payload))
        return ShellKillResult(mode="killed", interrupted=True, name="x").model_dump()

    monkeypatch.setattr(ttl_reaper.cluster_rpc, "dispatch_to_machine", _dispatch)
    reaped = await _reap_expired_shells(reaper_pool)

    assert reaped == [(aid, 21)]
    assert [d[0] for d in dispatched] == ["shell_kill"]
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agent_shell_ttls WHERE agent_id = %s", (aid,))
        row = cur.fetchone()
        assert row is not None and row[0] == 0
    assert _watcher_status(db_conn, aid, 21) == "reaped"
    assert (
        _system_inbounds(db_conn, aid) == []
    )  # watcher reclaim is not an "interrupted task" notice


async def test_reap_expired_shells_heals_legacy_watcher_before_deadline(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task #3411 migration: a running watcher whose recorded TTL is a stale
    placeholder (spawned before the unified write path) while its true
    deadline is still ahead is healed to that deadline — never reclaimed —
    and the heal is idempotent (a second pass finds nothing)."""
    aid = _running_agent(db_conn)
    end = datetime.now(UTC) + timedelta(hours=6)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at, created_at) "
            "VALUES (%s, 21, now() - interval '1 minute', now() - interval '1 hour 1 minute')",
            (aid,),
        )
        cur.execute(
            "INSERT INTO agent_watchers (agent_id, session_id, kind, name, status, cron_end_at) "
            "VALUES (%s, 21, 'cron', 'escalation-check', 'running', %s)",
            (aid, end),
        )
        cur.execute("UPDATE agents_meta SET machine = 'macmini' WHERE id = %s", (aid,))
    db_conn.commit()

    dispatched: list[tuple[str, object]] = []

    async def _dispatch(
        machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        dispatched.append((kind, payload))
        return ShellKillResult(mode="killed", interrupted=True, name="x").model_dump()

    monkeypatch.setattr(ttl_reaper.cluster_rpc, "dispatch_to_machine", _dispatch)
    reaped = await _reap_expired_shells(reaper_pool)

    assert reaped == []
    assert dispatched == []
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT expires_at FROM agent_shell_ttls WHERE agent_id = %s AND session_id = 21",
            (aid,),
        )
        row = cur.fetchone()
    assert row is not None
    assert abs((row[0] - end).total_seconds()) < 5  # rewritten to the true deadline
    assert _watcher_status(db_conn, aid, 21) == "running"

    # Idempotent: the healed row is no longer expired, so a second pass has
    # nothing to heal and nothing to kill.
    assert await _reap_expired_shells(reaper_pool) == []
    assert dispatched == []


async def test_reap_expired_shells_reaps_zombie_session_of_rebuilt_row(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1482-class: a terminal (`rebuilt`) row no longer pins the old session
    it names — a live session with an expired TTL is reclaimed like any
    other, while the history row keeps its status."""
    aid = _running_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at, created_at) "
            "VALUES (%s, 23, now() - interval '1 minute', now() - interval '1 hour 1 minute')",
            (aid,),
        )
        cur.execute(
            "INSERT INTO agent_watchers (agent_id, session_id, kind, name, status) "
            "VALUES (%s, 23, 'at', 'orphaned-one-shot', 'rebuilt')",
            (aid,),
        )
        cur.execute("UPDATE agents_meta SET machine = 'macmini' WHERE id = %s", (aid,))
    db_conn.commit()

    async def _dispatch(
        machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        return ShellKillResult(mode="killed", interrupted=False).model_dump()

    monkeypatch.setattr(ttl_reaper.cluster_rpc, "dispatch_to_machine", _dispatch)
    reaped = await _reap_expired_shells(reaper_pool)

    assert reaped == [(aid, 23)]
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agent_shell_ttls WHERE agent_id = %s", (aid,))
        row = cur.fetchone()
        assert row is not None and row[0] == 0
    assert _watcher_status(db_conn, aid, 23) == "rebuilt"


async def test_reap_expired_shells_reaps_closed_watcher_rows(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A watcher registry row in a terminal status (missed / reaped) does not
    shield the TTL row — the watcher is gone and normal reclamation applies."""
    aid = _running_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at, created_at) "
            "VALUES (%s, 22, now() - interval '1 minute', now() - interval '1 hour 1 minute')",
            (aid,),
        )
        cur.execute(
            "INSERT INTO agent_watchers (agent_id, session_id, kind, name, status) "
            "VALUES (%s, 22, 'at', 'one-shot', 'missed')",
            (aid,),
        )
        cur.execute("UPDATE agents_meta SET machine = 'macmini' WHERE id = %s", (aid,))
    db_conn.commit()

    async def _dispatch(
        machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        assert machine == "macmini"
        assert kind == "shell_kill"
        assert payload == {"agent_id": aid, "session_id": 22}
        return ShellKillResult(mode="absent").model_dump()

    monkeypatch.setattr(ttl_reaper.cluster_rpc, "dispatch_to_machine", _dispatch)
    reaped = await _reap_expired_shells(reaper_pool)

    assert reaped == [(aid, 22)]
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agent_shell_ttls WHERE agent_id = %s", (aid,))
        row = cur.fetchone()
        assert row is not None and row[0] == 0


async def test_reap_expired_shells_keeps_row_on_unknown_machine(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool
) -> None:
    aid = _running_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at) "
            "VALUES (%s, 9, now() - interval '1 minute')",
            (aid,),
        )
    db_conn.commit()

    reaped = await _reap_expired_shells(reaper_pool)
    assert reaped == []
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agent_shell_ttls WHERE agent_id = %s", (aid,))
        row = cur.fetchone()
        assert row is not None and row[0] == 1


async def test_reap_expired_shells_idle_reaping_is_silent(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A killed shell that carried no running job is reclaimed without a
    notice — the user ruling: only a reap that interrupts running work
    messages the owner."""
    aid = _running_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at, created_at) "
            "VALUES (%s, 4, now() - interval '1 minute', now() - interval '31 minutes')",
            (aid,),
        )
        cur.execute("UPDATE agents_meta SET machine = 'macmini' WHERE id = %s", (aid,))
    db_conn.commit()

    async def _dispatch(
        machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        return ShellKillResult(mode="killed", interrupted=False).model_dump()

    monkeypatch.setattr(
        ttl_reaper.cluster_rpc,
        "dispatch_to_machine",
        _dispatch,
    )
    reaped = await _reap_expired_shells(reaper_pool)

    assert reaped == [(aid, 4)]
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agent_shell_ttls WHERE agent_id = %s", (aid,))
        row = cur.fetchone()
        assert row is not None and row[0] == 0
    assert _system_inbounds(db_conn, aid) == []


async def test_reap_expired_shells_absent_reaping_is_silent(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session already gone was not interrupted — reclaiming its row sends
    no notice (previously it notified; the 2026-08-27 ruling narrows shell
    notices to interrupted work)."""
    aid = _running_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at) "
            "VALUES (%s, 5, now() - interval '1 minute')",
            (aid,),
        )
        cur.execute("UPDATE agents_meta SET machine = 'macmini' WHERE id = %s", (aid,))
    db_conn.commit()

    async def _dispatch(
        machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        return ShellKillResult(mode="absent").model_dump()

    monkeypatch.setattr(
        ttl_reaper.cluster_rpc,
        "dispatch_to_machine",
        _dispatch,
    )
    reaped = await _reap_expired_shells(reaper_pool)

    assert reaped == [(aid, 5)]
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agent_shell_ttls WHERE agent_id = %s", (aid,))
        row = cur.fetchone()
        assert row is not None and row[0] == 0
    assert _system_inbounds(db_conn, aid) == []


async def test_reap_expired_shells_missing_interrupted_field_notifies(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-policy runner's shell_kill result has no `interrupted` field —
    default to notifying (the old behavior) so a version-skewed fleet never
    silently swallows an interruption notice."""
    aid = _running_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at) "
            "VALUES (%s, 6, now() - interval '1 minute')",
            (aid,),
        )
        cur.execute("UPDATE agents_meta SET machine = 'macmini' WHERE id = %s", (aid,))
    db_conn.commit()

    async def _dispatch(
        machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        return {"mode": "killed"}  # old runner: no interrupted field

    monkeypatch.setattr(
        ttl_reaper.cluster_rpc,
        "dispatch_to_machine",
        _dispatch,
    )
    reaped = await _reap_expired_shells(reaper_pool)

    assert reaped == [(aid, 6)]
    msgs = _system_inbounds(db_conn, aid)
    assert len(msgs) == 1 and "interrupting a running task" in msgs[0]


def test_human_ttl_formats_compact_durations() -> None:
    """The notice's TTL duration reads compactly: whole hours/months as h/m,
    anything else in seconds; negative (clock-skewed) durations clamp to 0s."""
    from gateway.ttl_reaper import _human_ttl

    assert _human_ttl(3600) == "1h"
    assert _human_ttl(2 * 3600) == "2h"
    assert _human_ttl(1800) == "30m"
    assert _human_ttl(90) == "90s"
    assert _human_ttl(0) == "0s"
    assert _human_ttl(-61) == "0s"


def test_wall_clock_renders_cluster_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    """The notice's expiry time renders in the cluster timezone: bare HH:MM on
    the cluster's today, MM-DD HH:MM when the TTL crosses midnight."""
    from datetime import UTC, datetime, timedelta
    from zoneinfo import ZoneInfo

    from gateway import ttl_reaper

    sh = ZoneInfo("Asia/Shanghai")
    monkeypatch.setattr(ttl_reaper, "cluster_tz", lambda: sh)
    now = datetime.now(sh)

    # A specific wall-clock moment on the cluster's today: the same instant
    # fed in as UTC must render the Shanghai wall clock, no date prefix.
    at = now.replace(hour=23, minute=59, second=0, microsecond=0)
    assert ttl_reaper._wall_clock(at.astimezone(UTC)) == "23:59"

    # A moment on the cluster's tomorrow (TTL crossing midnight) carries the
    # MM-DD prefix.
    cross = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
    assert ttl_reaper._wall_clock(cross) == cross.strftime("%m-%d %H:%M")


def test_wall_clock_none_falls_back_to_host_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    """cluster_tz() None is the host-zone fallback signal: the stamp matches
    dt.astimezone(None) (machine-local), not UTC."""
    from datetime import UTC, datetime

    from gateway import ttl_reaper

    monkeypatch.setattr(ttl_reaper, "cluster_tz", lambda: None)
    # A moment on the host's today: the fallback stamp carries no date prefix.
    # (A fixed past date would cross midnight and gain the MM-DD prefix the
    # next day — a time-bomb assertion that expired 2026-08-28.)
    dt = (
        datetime.now()
        .astimezone()
        .replace(hour=12, minute=0, second=0, microsecond=0)
        .astimezone(UTC)
    )
    assert ttl_reaper._wall_clock(dt) == dt.astimezone(None).strftime("%H:%M")


# --- Expired notices reaping ------------------------------------------------


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

    monkeypatch.setattr(ttl_reaper, "publish_agent_updated_sync", capture_hint)
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


def _insert_shell_row(
    db_conn: psycopg.Connection,
    agent_id: int,
    session_id: int,
    *,
    expires_at: datetime,
) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at, created_at) "
            "VALUES (%s, %s, %s, now() - interval '1 hour') "
            "ON CONFLICT (agent_id, session_id) DO UPDATE SET expires_at = EXCLUDED.expires_at",
            (agent_id, session_id, expires_at),
        )
    db_conn.commit()


def test_claim_still_expired_true_for_expired_row(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool
) -> None:
    aid = _running_agent(db_conn)
    _insert_shell_row(db_conn, aid, 41, expires_at=datetime.now(UTC) - timedelta(minutes=1))
    assert _claim_shell_row_still_expired(reaper_pool, aid, 41)


def test_claim_still_expired_false_for_renewed_row(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool
) -> None:
    """A row renewed between the expired select and the kill dispatch fails
    the claim — the deadline moved forward, no kill may be dispatched."""
    aid = _running_agent(db_conn)
    _insert_shell_row(db_conn, aid, 42, expires_at=datetime.now(UTC) + timedelta(hours=1))
    assert not _claim_shell_row_still_expired(reaper_pool, aid, 42)


def test_claim_still_expired_evaluates_at_statement_time(
    db_conn: psycopg.Connection,
) -> None:
    """Issue #2053: the claim re-check reads the deadline at statement time.

    A claim transaction that began before the deadline still claims the row
    once the deadline has actually passed; with now() (transaction start) it
    would skip the row and the pass would never dispatch the kill.
    """
    import time

    aid = _running_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at, created_at) "
            "VALUES (%s, %s, clock_timestamp() + interval '1 second', clock_timestamp())",
            (aid, 45),
        )
    db_conn.commit()
    with db_conn.cursor() as cur:
        # The transaction starts before the deadline passes.
        cur.execute("SET TRANSACTION READ WRITE")
        time.sleep(2.0)
        assert ttl_reaper._claim_still_expired(cur, aid, 45)
    db_conn.rollback()


def test_claim_still_expired_false_for_missing_row(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool
) -> None:
    aid = _running_agent(db_conn)
    assert not _claim_shell_row_still_expired(reaper_pool, aid, 43)


async def test_reap_expired_shells_skips_row_renewed_after_select(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The race the claim closes: the expired-row select ran first, then the
    owner renewed (deadline now in the future). The pass must not kill —
    the stale select's row fails the claim and the live row survives."""
    aid = _running_agent(db_conn)
    _insert_shell_row(db_conn, aid, 44, expires_at=datetime.now(UTC) - timedelta(minutes=1))
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET machine = 'macmini' WHERE id = %s", (aid,))
        # The renewal that landed after the (stale) select: deadline moved forward.
        cur.execute(
            "UPDATE agent_shell_ttls SET expires_at = now() + interval '1 hour', "
            "renewals = renewals + 1 WHERE agent_id = %s AND session_id = %s",
            (aid, 44),
        )
    db_conn.commit()

    stale_rows: list[dict[str, object]] = [
        {
            "agent_id": aid,
            "session_id": 44,
            "expires_at": datetime.now(UTC) - timedelta(minutes=1),
            "created_at": datetime.now(UTC) - timedelta(hours=2),
            "watcher_kind": None,
            "watcher_status": None,
            "watcher_created_at": None,
            "watcher_timeout_secs": None,
            "watcher_fires_at": None,
            "watcher_cron_end_at": None,
        }
    ]

    def _stale_select(_pool: ConnectionPool) -> list[dict[str, object]]:
        return stale_rows

    monkeypatch.setattr(ttl_reaper, "_expired_shell_rows_blocking", _stale_select)
    dispatched: list[tuple[str, object]] = []

    async def _dispatch(
        machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        dispatched.append((kind, payload))
        return ShellKillResult(mode="killed", interrupted=True, name="x").model_dump()

    monkeypatch.setattr(ttl_reaper.cluster_rpc, "dispatch_to_machine", _dispatch)
    reaped = await _reap_expired_shells(reaper_pool)

    assert reaped == []
    assert dispatched == []
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM agent_shell_ttls WHERE agent_id = %s AND session_id = %s",
            (aid, 44),
        )
        row = cur.fetchone()
        assert row is not None and row[0] == 1


# ─── terminated-owner watcher reclamation (task #2617) ───────────────────────


def _terminated_agent(conn: psycopg.Connection, *, source: str | None) -> int:
    """An agent terminated for good under the given termination_source."""
    aid = _running_agent(conn)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status='terminated', termination_source=%s, machine='macmini' "
            "WHERE id = %s",
            (source, aid),
        )
    conn.commit()
    return aid


def _watcher_row(
    conn: psycopg.Connection, agent_id: int, session_id: int, status: str = "running"
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_watchers (agent_id, session_id, kind, name, status) "
            "VALUES (%s, %s, 'cron', 'daily', %s)",
            (agent_id, session_id, status),
        )
    conn.commit()


def _watcher_status(conn: psycopg.Connection, agent_id: int, session_id: int) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM agent_watchers WHERE agent_id = %s AND session_id = %s",
            (agent_id, session_id),
        )
        row = cur.fetchone()
    assert row is not None
    return row[0]


@pytest.mark.parametrize("source", ["user", "exit", "integrity", None])
async def test_reap_terminated_owner_watcher_killed_marks_reaped(
    db_conn: psycopg.Connection,
    reaper_pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    source: str | None,
) -> None:
    """A watcher whose owner is terminated for good is killed and its row
    terminalized to 'reaped' — on a definitive kill verdict only."""
    aid = _terminated_agent(db_conn, source=source)
    _watcher_row(db_conn, aid, 31)

    async def _dispatch(
        machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        assert machine == "macmini"
        assert kind == "shell_kill"
        assert payload == {"agent_id": aid, "session_id": 31}
        return ShellKillResult(mode="killed", interrupted=True, name="daily").model_dump()

    monkeypatch.setattr(ttl_reaper.cluster_rpc, "dispatch_to_machine", _dispatch)
    reaped = await _reap_terminated_watchers(reaper_pool)

    assert reaped == [(aid, 31)]
    assert _watcher_status(db_conn, aid, 31) == "reaped"


async def test_reap_terminated_owner_watcher_stamps_reaped_at(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Terminalizing a reclaimed row moves updated_at with it (task #3525).

    The terminated-owner path used to leave updated_at == created_at while
    both sibling entry points (live-owner reaping, the registry's mark) bump
    it — so a reaped row's updated_at read as its insert time instead of
    "reaped at". The stamp is pre-aged to make the refresh observable."""
    aid = _terminated_agent(db_conn, source="user")
    _watcher_row(db_conn, aid, 41)
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_watchers SET updated_at = now() - interval '1 hour' "
            "WHERE agent_id = %s AND session_id = %s",
            (aid, 41),
        )
    db_conn.commit()

    async def _dispatch(
        machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        return ShellKillResult(mode="killed", interrupted=False, name="daily").model_dump()

    monkeypatch.setattr(ttl_reaper.cluster_rpc, "dispatch_to_machine", _dispatch)
    reaped = await _reap_terminated_watchers(reaper_pool)

    assert reaped == [(aid, 41)]
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT status, updated_at > created_at, "
            "updated_at > now() - interval '5 minutes' "
            "FROM agent_watchers WHERE agent_id = %s AND session_id = %s",
            (aid, 41),
        )
        row = cur.fetchone()
    assert row is not None
    status, after_created, fresh = row
    assert status == "reaped"
    assert after_created, "updated_at must move past created_at on reap"
    assert fresh, "updated_at must be refreshed to the reap time"


async def test_reap_terminated_owner_watcher_absent_marks_reaped(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An already-gone session is a definitive verdict too — the row is
    terminalized without a second dispatch."""
    aid = _terminated_agent(db_conn, source="exit")
    _watcher_row(db_conn, aid, 32)

    async def _dispatch(
        machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        return ShellKillResult(mode="absent").model_dump()

    monkeypatch.setattr(ttl_reaper.cluster_rpc, "dispatch_to_machine", _dispatch)
    reaped = await _reap_terminated_watchers(reaper_pool)

    assert reaped == [(aid, 32)]
    assert _watcher_status(db_conn, aid, 32) == "reaped"
    # The absent verdict is definitive too — the #2060 notice rides on it.
    assert _system_inbounds(db_conn, aid) == [
        f"Watcher schedule 'daily' (agent {aid}) was reclaimed because its "
        "owner agent was terminated. Re-register it with ava.watcher.cron() "
        "if it is still needed."
    ]


@pytest.mark.parametrize("source", ["user", "exit", "integrity", None])
async def test_reap_terminated_owner_watcher_queues_reclamation_notice(
    db_conn: psycopg.Connection,
    reaper_pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    source: str | None,
) -> None:
    """The #2060 ruling: reaped is terminal — the schedule never auto-restores
    — so the definitive reap must QUEUE a reclamation notice for the owner in
    the same transaction. The owner is terminated at mark time, so the notice
    is inserted as a pending inbound WITHOUT waking it (a reclamation notice
    never resurrects); it delivers on the agent's next resurrect through any
    channel and tells it to re-register. The text attributes the reap to the
    owner's termination — the real cause; this path checks no expiry and must
    not claim one (task #4051)."""
    aid = _terminated_agent(db_conn, source=source)
    _watcher_row(db_conn, aid, 35)

    async def _dispatch(
        machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        return ShellKillResult(mode="killed", interrupted=False, name="daily").model_dump()

    monkeypatch.setattr(ttl_reaper.cluster_rpc, "dispatch_to_machine", _dispatch)
    reaped = await _reap_terminated_watchers(reaper_pool)

    assert reaped == [(aid, 35)]
    assert _watcher_status(db_conn, aid, 35) == "reaped"
    assert _system_inbounds(db_conn, aid) == [
        f"Watcher schedule 'daily' (agent {aid}) was reclaimed because its "
        "owner agent was terminated. Re-register it with ava.watcher.cron() "
        "if it is still needed."
    ]
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (aid,))
        assert cur.fetchone() == ("terminated",)  # the notice never resurrects


async def test_reap_terminated_owner_watcher_unreachable_keeps_row(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreachable machine leaves the row running for the next pass —
    marking it first would orphan the live session (the shell-TTL discipline)."""
    aid = _terminated_agent(db_conn, source="user")
    _watcher_row(db_conn, aid, 33)

    async def _dispatch(
        machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        raise ttl_reaper.cluster_rpc.ClusterOpUnreachable("boom")

    monkeypatch.setattr(ttl_reaper.cluster_rpc, "dispatch_to_machine", _dispatch)
    reaped = await _reap_terminated_watchers(reaper_pool)

    assert reaped == []
    assert _watcher_status(db_conn, aid, 33) == "running"


async def test_reap_terminated_owner_watcher_resurrected_mid_pass_keeps_row_running(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The #2589/#1938 atomic-guard discipline: the kill verdict is only
    terminalized when the owner is STILL terminated in the same statement. A
    mid-flight resurrect (terminated -> idling) leaves the row 'running' so
    the agent's own boot reconcile rebuilds the schedule — never a silent
    loss."""
    aid = _terminated_agent(db_conn, source="user")
    _watcher_row(db_conn, aid, 34)

    async def _dispatch(
        machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        # The owner resurrects between the scan and the mark.
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET status='idling', termination_source=NULL WHERE id=%s",
                (aid,),
            )
        db_conn.commit()
        return ShellKillResult(mode="killed", interrupted=True, name="daily").model_dump()

    monkeypatch.setattr(ttl_reaper.cluster_rpc, "dispatch_to_machine", _dispatch)
    reaped = await _reap_terminated_watchers(reaper_pool)

    assert reaped == []
    assert _watcher_status(db_conn, aid, 34) == "running"
    # The mark never happened, so no reclamation notice was queued either —
    # the surviving row is the agent's own reconcile business (the #2060
    # notice rides on the definitive reap only).
    assert _system_inbounds(db_conn, aid) == []


@pytest.mark.parametrize("source", ["reaper", "launch-confirm"])
async def test_crash_corpse_watchers_are_not_reaped(
    db_conn: psycopg.Connection,
    reaper_pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    """Crash-recovery corpses keep their watchers: they are auto-resurrect-
    eligible and their own cron wakes are a revival channel — killing them
    would race the crash-resurrect controller (the #2589 lesson)."""
    aid = _terminated_agent(db_conn, source=source)
    _watcher_row(db_conn, aid, 35)

    dispatched: list[tuple[str, object]] = []

    async def _dispatch(
        machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        dispatched.append((kind, payload))
        return ShellKillResult(mode="killed", interrupted=True, name="x").model_dump()

    monkeypatch.setattr(ttl_reaper.cluster_rpc, "dispatch_to_machine", _dispatch)
    reaped = await _reap_terminated_watchers(reaper_pool)

    assert reaped == []
    assert dispatched == []
    assert _watcher_status(db_conn, aid, 35) == "running"


async def test_live_owner_watchers_are_not_reaped(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live owner's watcher is never touched — only status='terminated'
    owners are in scope."""
    aid = _running_agent(db_conn)
    _watcher_row(db_conn, aid, 36)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET machine='macmini' WHERE id=%s", (aid,))
    db_conn.commit()

    dispatched: list[tuple[str, object]] = []

    async def _dispatch(
        machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        dispatched.append((kind, payload))
        return ShellKillResult(mode="killed", interrupted=True, name="x").model_dump()

    monkeypatch.setattr(ttl_reaper.cluster_rpc, "dispatch_to_machine", _dispatch)
    reaped = await _reap_terminated_watchers(reaper_pool)

    assert reaped == []
    assert dispatched == []
    assert _watcher_status(db_conn, aid, 36) == "running"


async def test_terminal_watcher_rows_are_not_reaped(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only status='running' rows carry live sessions; rebuilt/missed/reaped
    rows are terminal history and stay untouched."""
    aid = _terminated_agent(db_conn, source="user")
    _watcher_row(db_conn, aid, 37, status="rebuilt")

    dispatched: list[tuple[str, object]] = []

    async def _dispatch(
        machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        dispatched.append((kind, payload))
        return ShellKillResult(mode="killed", interrupted=True, name="x").model_dump()

    monkeypatch.setattr(ttl_reaper.cluster_rpc, "dispatch_to_machine", _dispatch)
    reaped = await _reap_terminated_watchers(reaper_pool)

    assert reaped == []
    assert dispatched == []
    assert _watcher_status(db_conn, aid, 37) == "rebuilt"


async def test_shutdown_stop_defers_the_rest_of_the_shell_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop waits out only the in-flight dispatch, never the rest of the batch.

    Each unreachable-machine ``shell_kill`` dispatch burns its full retry
    budget (tens of seconds) and the batch is serial, so a stop landing during
    one dispatch used to make ``stop_ttl_reaper`` — and with it the gateway
    lifespan — wait out every remaining row. With ``stop`` set, the pass must
    finish the in-flight dispatch and leave the rest to the next pass.
    """
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[int] = []

    async def _dispatch(
        _machine: str, _kind: str, payload: dict[str, Any], **kwargs: object
    ) -> dict[str, object]:
        calls.append(int(payload["session_id"]))
        started.set()
        await release.wait()
        raise ttl_reaper.cluster_rpc.ClusterOpUnreachable("machine unreachable")

    def _rows(_pool: object) -> list[dict[str, object]]:
        return [
            {"agent_id": 1, "session_id": session_id, "watcher_status": None}
            for session_id in (101, 102, 103)
        ]

    def _claim(_pool: object, _agent_id: int, _session_id: int) -> bool:
        return True

    def _machine_of(_pool: object, _agent_id: int) -> str:
        return "machine-x"

    def _deadline(_row: dict[str, object]) -> None:
        return None

    monkeypatch.setattr(ttl_reaper, "_expired_shell_rows_blocking", _rows)
    monkeypatch.setattr(ttl_reaper, "_claim_shell_row_still_expired", _claim)
    monkeypatch.setattr(ttl_reaper, "_agent_machine", _machine_of)
    monkeypatch.setattr(ttl_reaper, "watcher_deadline_of", _deadline)
    monkeypatch.setattr(ttl_reaper.cluster_rpc, "dispatch_to_machine", _dispatch)

    pool = cast(ConnectionPool, None)  # every pool consumer above is faked
    stop = asyncio.Event()

    async def _drive() -> None:
        await ttl_reaper._reap_expired_shells(pool, stop)

    reap_task = asyncio.create_task(_drive())
    await asyncio.wait_for(started.wait(), timeout=5)

    reaper = ttl_reaper.TtlReaper(task=reap_task, stop=stop)
    stopper = asyncio.create_task(ttl_reaper.stop_ttl_reaper(reaper))
    while not stop.is_set():
        await asyncio.sleep(0.01)
    assert not stopper.done()  # the in-flight dispatch still holds the pass open

    release.set()
    await asyncio.wait_for(stopper, timeout=5)
    assert calls == [101]  # rows 102/103 deferred to the next pass
