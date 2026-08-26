"""`services.events_maintenance.table_retention` — the append-only fact tables'
retention pass.

Pins: only terminal rows are deleted (done inbounds, resolved notices, closed
pages, done/cancelled tasks, ops buckets) and only past their retention;
open/live rows are never touched; tasks referenced as a parent or by a notice
are kept; the pass is idempotent; the daemon's hourly maintenance pass runs it.
Runs against a throwaway DB with the minimal table shapes the policies touch.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace

import psycopg
import pytest
from psycopg import sql
from psycopg_pool import ConnectionPool

from services.events_maintenance import daemon
from services.events_maintenance.table_retention import (
    TABLE_POLICIES,
    apply_table_retention,
)
from shared.config import settings
from shared.daemon_health import LoopProgress

_NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)

# Minimal shapes of the fact tables (only the columns the policies touch).
_SCHEMA = """
CREATE TABLE agents (
    id BIGSERIAL PRIMARY KEY
);
CREATE TABLE agents_meta (
    id BIGINT PRIMARY KEY REFERENCES agents(id)
);
CREATE TABLE inbound_messages (
    id BIGSERIAL PRIMARY KEY,
    agent_id BIGINT,
    status TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE agent_notices (
    id BIGSERIAL PRIMARY KEY,
    agent_id BIGINT,
    resolved_at TIMESTAMPTZ,
    task_id BIGINT
);
CREATE TABLE agent_pages (
    id BIGSERIAL PRIMARY KEY,
    agent_id BIGINT,
    closed_at TIMESTAMPTZ,
    expired_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE agent_tasks (
    id BIGSERIAL PRIMARY KEY,
    parent_id BIGINT,
    status TEXT,
    is_root BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _throwaway_db() -> Iterator[str]:
    base_url, _name = settings.data_plane.db_url.rsplit("/", 1)
    admin_url = f"{base_url}/postgres"
    name = f"ava_test_tblret_{os.getpid()}_{int(time.time() * 1_000_000)}"
    url = f"{base_url}/{name}"
    with psycopg.connect(admin_url, autocommit=True) as admin, admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        with psycopg.connect(url, autocommit=True) as setup:
            setup.execute(_SCHEMA)  # type: ignore[arg-type]  # trusted multi-statement setup
        yield url
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin, admin.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


@pytest.fixture
def tbl_db_url(db_conn: psycopg.Connection) -> Iterator[str]:
    _ = db_conn
    yield from _throwaway_db()


@pytest.fixture
def tbl_conn(tbl_db_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(tbl_db_url) as conn:
        yield conn


def _count(conn: psycopg.Connection, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
        row = cur.fetchone()
        assert row is not None
        return row[0]


def test_inbound_messages_terminal_only_past_retention(tbl_conn: psycopg.Connection) -> None:
    """Done inbounds older than 90d are deleted; fresh done rows and every
    pending/claimed row are kept."""
    old = datetime(2026, 1, 1, tzinfo=UTC)
    fresh = datetime(2026, 7, 20, tzinfo=UTC)
    with tbl_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, status, created_at) VALUES "
            "(1, 'done', %s), (1, 'done', %s), (1, 'pending', %s), (1, 'claimed', %s), "
            "(2, 'done', %s)",
            (old, fresh, old, old, fresh),
        )
    result = apply_table_retention(tbl_conn, now_utc=_NOW)
    assert result.deleted == (("inbound_messages", 1),)
    assert _count(tbl_conn, "inbound_messages") == 4


def test_notices_resolved_only_past_retention(tbl_conn: psycopg.Connection) -> None:
    """Resolved notices older than 365d are deleted; open and fresh resolved
    ones are kept."""
    old = datetime(2025, 1, 1, tzinfo=UTC)
    fresh = datetime(2026, 6, 1, tzinfo=UTC)
    with tbl_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_notices (agent_id, resolved_at) VALUES "
            "(1, %s), (1, %s), (1, NULL), (2, NULL)",
            (old, fresh),
        )
    result = apply_table_retention(tbl_conn, now_utc=_NOW)
    assert result.deleted == (("agent_notices", 1),)
    assert _count(tbl_conn, "agent_notices") == 3


def test_pages_closed_only_past_retention(tbl_conn: psycopg.Connection) -> None:
    """Closed pages older than 90d are deleted; open and fresh closed ones are
    kept."""
    old = datetime(2026, 1, 1, tzinfo=UTC)
    fresh = datetime(2026, 7, 20, tzinfo=UTC)
    with tbl_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_pages (agent_id, closed_at, created_at) VALUES "
            "(1, %s, now()), (1, %s, now()), (1, NULL, now()), (2, %s, now())",
            (old, fresh, fresh),
        )
    result = apply_table_retention(tbl_conn, now_utc=_NOW)
    assert result.deleted == (("agent_pages", 1),)
    assert _count(tbl_conn, "agent_pages") == 3


def test_tasks_done_cancelled_only_past_retention_with_guards(
    tbl_conn: psycopg.Connection,
) -> None:
    """Done/cancelled tasks older than 365d are deleted — but never a task that
    is a parent of another task, never a task referenced by a notice, and never
    the root; open/in_progress tasks are untouched regardless of age."""
    old = datetime(2025, 1, 1, tzinfo=UTC)
    with tbl_conn.cursor() as cur:
        # 1: deletable (done, old). 2: parent of 3 (kept — FK guard). 3: child,
        # done + old (deleted — no guard protects a child itself). 4: referenced
        # by a notice (kept — FK guard). 5: in_progress (kept). 6: cancelled
        # old, standalone (deleted). 7: root (kept).
        cur.execute(
            "INSERT INTO agent_tasks (id, parent_id, status, is_root, updated_at) VALUES "
            "(1, NULL, 'done', FALSE, %s), "
            "(2, NULL, 'done', FALSE, %s), "
            "(3, 2, 'done', FALSE, %s), "
            "(4, NULL, 'done', FALSE, %s), "
            "(5, NULL, 'in_progress', FALSE, %s), "
            "(6, NULL, 'cancelled', FALSE, %s), "
            "(7, NULL, 'in_progress', TRUE, %s)",
            (old, old, old, old, old, old, old),
        )
        cur.execute(
            "INSERT INTO agent_notices (agent_id, resolved_at, task_id) VALUES (1, NULL, 4)"
        )
    result = apply_table_retention(tbl_conn, now_utc=_NOW)
    assert result.deleted == (("agent_tasks", 3),)
    assert _count(tbl_conn, "agent_tasks") == 4


def test_idempotent(tbl_conn: psycopg.Connection) -> None:
    """A second pass after a successful one deletes nothing."""
    old = datetime(2026, 1, 1, tzinfo=UTC)
    with tbl_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, status, created_at) VALUES (1, 'done', %s)",
            (old,),
        )
    first = apply_table_retention(tbl_conn, now_utc=_NOW)
    second = apply_table_retention(tbl_conn, now_utc=_NOW)
    assert first.deleted == (("inbound_messages", 1),)
    assert not second.deleted


def test_policy_registry_covers_the_append_only_tables() -> None:
    """Every append-only fact table the audit flagged has a registered policy;
    `agents`/`agents_meta`/`agent_activity` are deliberately absent (permanent
    by design / frozen legacy — see the module docstring)."""
    covered = {p.table for p in TABLE_POLICIES}
    assert covered == {
        "inbound_messages",
        "agent_notices",
        "agent_pages",
        "agent_tasks",
    }


def test_daemon_maintenance_runs_table_retention(
    tbl_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hourly maintenance pass runs the table-retention slice on its own
    connection and logs non-empty deletes."""
    # The table-retention slice is one of the events-archive slices
    # `_run_maintenance` gates on the (default-off) events flag — pin it on.
    monkeypatch.setattr(daemon.settings.daemon, "events_maintenance_enabled", True)
    pool: ConnectionPool = ConnectionPool(tbl_db_url, min_size=1, max_size=2, open=True)
    calls: list[tuple[psycopg.Connection, dict[str, object]]] = []

    def _fake_table_retention(conn: psycopg.Connection, **kwargs: object) -> SimpleNamespace:
        calls.append((conn, kwargs))
        return SimpleNamespace(
            deleted=(("inbound_messages", 3),),
            summary=lambda: "inbound_messages: 3 rows",
        )

    def _fake_retention(_conn: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(dropped=(), pruned=())

    def _fake_rollup(_conn: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(start_day=None)

    def _fake_replay(_conn: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(days_replayed=[], days_failed=[], metrics_rows=0, tokens_rows=0)

    def _fake_partitions(_conn: object, **_kwargs: object) -> list[str]:
        return []

    monkeypatch.setattr(daemon, "apply_table_retention", _fake_table_retention)
    monkeypatch.setattr(daemon, "apply_retention", _fake_retention)
    monkeypatch.setattr(daemon, "compute_rollup", _fake_rollup)
    monkeypatch.setattr(daemon, "replay_gap_days", _fake_replay)
    monkeypatch.setattr(daemon, "ensure_month_partitions", _fake_partitions)
    try:
        with pool:
            daemon._run_maintenance(pool, LoopProgress("dispatch", timeout_s=60.0))
    finally:
        pool.close()
    assert len(calls) == 1
    _conn, kwargs = calls[0]
    assert kwargs["now_utc"] is not None
