"""The run timeline reads audit facts from `audit_events` and the rest from `telemetry_events`.

Both records are permanent, so an older marker or turn is never lost to a retention window.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import LiteralString

import psycopg

from base.db import Database
from gateway.run_timeline import _events as reads

_INSERT_AUDIT: LiteralString = (
    "INSERT INTO audit_events (event_uid, ts, machine, process, event_name, level, source, "
    "agent_id) VALUES (%s, now() - (%s * interval '1 hour'), 'm', 'p', %s, 'info', 'test', %s)"
)
_INSERT_TELEMETRY: LiteralString = (
    "INSERT INTO telemetry_events (event_uid, ts, machine, process, category, cluster, "
    "event_name, level, source, agent_id) VALUES (%s, now() - (%s * interval '1 hour'), 'm', 'p', "
    "'telemetry', 'c', %s, 'info', 'test', %s)"
)


def _record(
    db: psycopg.Connection, table: str, agent_id: int, name: str, *, hours_ago: float
) -> None:
    query = _INSERT_TELEMETRY if table == "telemetry_events" else _INSERT_AUDIT
    db.execute(query, (uuid.uuid4().int % (1 << 62), hours_ago, name, agent_id))
    db.commit()


def test_audit_names_come_from_audit_events_and_the_rest_from_telemetry_events(
    db_conn: psycopg.Connection,
) -> None:
    _record(db_conn, "audit_events", 405, "compact", hours_ago=200)
    _record(db_conn, "audit_events", 405, "spawn", hours_ago=300)
    _record(db_conn, "audit_events", 406, "compact", hours_ago=200)
    _record(db_conn, "telemetry_events", 405, "turn_end", hours_ago=250)
    _record(db_conn, "telemetry_events", 406, "turn_end", hours_ago=250)
    now = datetime.now(UTC)

    events = reads.query_all_events(
        Database.from_settings(),
        405,
        now - timedelta(days=30),
        now,
        event_names=("compact", "spawn", "turn_end"),
    )

    assert sorted(str(event["event_name"]) for event in events) == ["compact", "spawn", "turn_end"]
    assert {event["agent_id"] for event in events} == {405}


def test_an_audit_only_read_never_asks_for_telemetry_rows(db_conn: psycopg.Connection) -> None:
    _record(db_conn, "audit_events", 405, "terminate", hours_ago=1)
    _record(db_conn, "telemetry_events", 405, "terminate", hours_ago=1)
    now = datetime.now(UTC)

    events = reads.query_all_events(
        Database.from_settings(), 405, now - timedelta(days=1), now, event_names=("terminate",)
    )

    assert [event["category"] for event in events] == ["audit"]
