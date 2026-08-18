"""`services.events_maintenance.daemon._apply_resolved_markers` — resolved 标记 pass。

warning_resolved / error_resolved 事件被消费：精确 id 或 msg-LIKE 模式匹配的
warning/error 事件被写回 ``attributes.resolved_by``（unresolved 面板据此过滤）。
Pins: 精确 id 标记；模式匹配只标记 resolved 之前且 90 天内的事件；幂等
（已标记不重标）；resolved 事件本身留在流里。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from services.events_maintenance.daemon import _apply_resolved_markers

_NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


@pytest.fixture
def pool(db_conn: psycopg.Connection) -> Iterator[ConnectionPool[Any]]:
    """A pool to a throwaway DB with a fresh partitioned events table."""
    _ = db_conn
    from tests.services.test_events_maintenance_ttl import _throwaway_db

    gen = cast(Any, _throwaway_db())
    url = next(gen)  # keep the generator alive — GC would run its finally (DROP DATABASE)
    try:
        with ConnectionPool(url, min_size=1, max_size=2, open=True) as p:
            yield p
    finally:
        gen.close()


def _ins(
    conn: psycopg.Connection,
    *,
    ts: datetime,
    level: str,
    msg: str | None,
    event_name: str = "log",
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO events (ts, machine, process, category, event_name, level, source, attributes) "
            "VALUES (%s, 'test', 'test', 'log', %s, %s, 'system', %s) RETURNING id",
            (ts, event_name, level, Jsonb({"msg": msg} if msg else {})),
        )
        row = cur.fetchone()
        assert row is not None
        return row[0]


def _resolved(
    conn: psycopg.Connection,
    *,
    ts: datetime,
    name: str,
    target_event_id: int | None = None,
    match: str | None = None,
) -> None:
    attrs = {}
    if target_event_id is not None:
        attrs["target_event_id"] = target_event_id
    if match is not None:
        attrs["match"] = match
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO events (ts, machine, process, category, event_name, level, source, attributes) "
            "VALUES (%s, 'test', 'test', 'log', %s, 'info', 'system', %s)",
            (ts, name, Jsonb(attrs)),
        )


def _marked(conn: psycopg.Connection, event_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT attributes->>'resolved_by' IS NOT NULL FROM events WHERE id = %s", (event_id,)
        )
        row = cur.fetchone()
        assert row is not None
        return bool(row[0])


def test_exact_id_resolved(pool: ConnectionPool) -> None:
    with pool.connection() as conn:
        conn.autocommit = True
        wid = _ins(conn, ts=_NOW - timedelta(hours=1), level="warning", msg="boom")
        _resolved(conn, ts=_NOW, name="warning_resolved", target_event_id=wid)
    assert _apply_resolved_markers(pool) == 1
    with pool.connection() as conn:
        conn.autocommit = True
        assert _marked(conn, wid)


def test_pattern_match_resolves_class(pool: ConnectionPool) -> None:
    with pool.connection() as conn:
        conn.autocommit = True
        hit1 = _ins(
            conn,
            ts=_NOW - timedelta(hours=2),
            level="warning",
            msg="page-server spawn failed for x",
        )
        hit2 = _ins(
            conn, ts=_NOW - timedelta(hours=1), level="error", msg="page-server spawn failed for y"
        )
        miss = _ins(conn, ts=_NOW - timedelta(minutes=30), level="warning", msg="unrelated flake")
        _resolved(conn, ts=_NOW, name="warning_resolved", match="%page-server%spawn failed%")
    assert _apply_resolved_markers(pool) == 2
    with pool.connection() as conn:
        conn.autocommit = True
        assert _marked(conn, hit1)
        assert _marked(conn, hit2)
        assert not _marked(conn, miss)


def test_future_events_not_pattern_marked(pool: ConnectionPool) -> None:
    with pool.connection() as conn:
        conn.autocommit = True
        future = _ins(
            conn,
            ts=_NOW + timedelta(hours=1),
            level="warning",
            msg="page-server spawn failed later",
        )
        _resolved(conn, ts=_NOW, name="warning_resolved", match="%spawn failed%")
    assert _apply_resolved_markers(pool) == 0
    with pool.connection() as conn:
        conn.autocommit = True
        assert not _marked(conn, future)


def test_idempotent(pool: ConnectionPool) -> None:
    with pool.connection() as conn:
        conn.autocommit = True
        wid = _ins(conn, ts=_NOW - timedelta(hours=1), level="error", msg="e1")
        _resolved(conn, ts=_NOW, name="error_resolved", target_event_id=wid)
    assert _apply_resolved_markers(pool) == 1
    assert _apply_resolved_markers(pool) == 0
