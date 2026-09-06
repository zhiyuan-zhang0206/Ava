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
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from gateway import ttl_reaper
from gateway.ttl_reaper import (
    _reap_expired_pages_blocking,
    _reap_expired_shells,
    _reap_expired_web_sessions_blocking,
    _reaper_loop,
)
from ops.rpc_schemas import ShellKillResult
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


def _empty_web_session_reap(_pool: ConnectionPool) -> int:
    return 0


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
            "VALUES (%s, %s, 8001, '127.0.0.1', %s) RETURNING id",
            (agent_id, name, expires_at),
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


async def _empty_shell_reap(_pool: ConnectionPool) -> list[tuple[int, int]]:
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
