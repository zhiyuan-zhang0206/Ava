"""Lifecycle markers come from `audit_events` — spawn, fork, restart, terminate only, this agent only."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import LiteralString

import psycopg
import pytest

from base.db import Database
from services.derived.insights.run_timeline import _lifecycle

_INSERT: LiteralString = (
    "INSERT INTO audit_events (event_uid, ts, machine, process, event_name, level, source, "
    "agent_id) VALUES (%s, now() - (%s * interval '1 hour'), 'm', 'p', %s, 'info', %s, %s)"
)


def record(
    conn: psycopg.Connection, agent_id: int, name: str, hours_ago: float, source: str = "test"
) -> None:
    conn.execute(_INSERT, (uuid.uuid4().int % (1 << 62), hours_ago, name, source, agent_id))
    conn.commit()


def test_only_lifecycle_events_of_the_agent_in_the_window(db_conn: psycopg.Connection) -> None:
    record(db_conn, 405, "spawn", 30)
    record(db_conn, 405, "restart_completed", 20)
    record(db_conn, 405, "terminate", 10)
    record(db_conn, 405, "compact", 15)  # an audit event, but not a lifecycle marker
    record(db_conn, 406, "spawn", 15)  # another agent
    record(db_conn, 405, "spawn", 100)  # outside the window
    now = datetime.now(UTC)
    events = _lifecycle.read(Database.from_settings(), 405, now - timedelta(hours=48), now)
    assert [e.kind for e in events] == ["spawn", "restart_completed", "terminate"]


def test_fork_and_restart_are_markers_and_carry_who_caused_them(
    db_conn: psycopg.Connection,
) -> None:
    record(db_conn, 405, "fork", 3, "agent:7")
    record(db_conn, 405, "restart", 2, "user")
    now = datetime.now(UTC)
    events = _lifecycle.read(Database.from_settings(), 405, now - timedelta(hours=48), now)
    assert [(e.kind, e.source) for e in events] == [("fork", "agent:7"), ("restart", "user")]


def test_a_window_longer_than_a_page_is_read_completely(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_lifecycle, "_PAGE_SIZE", 2)
    for hours in (5, 4, 3, 2, 1):
        record(db_conn, 405, "resurrect", hours)
    now = datetime.now(UTC)
    events = _lifecycle.read(Database.from_settings(), 405, now - timedelta(hours=24), now)
    assert len(events) == 5
