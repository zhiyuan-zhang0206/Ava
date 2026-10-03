"""The telemetry_events table: monthly partitions, append-only, idempotent identity."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

_INSERT = (
    "INSERT INTO telemetry_events (event_uid, ts, machine, cluster, process, category, "
    "event_name, level, source) VALUES (%s, %s, 'm', 'c', 'p', 'telemetry', %s, 'info', 'system')"
)


def test_a_row_lands_in_its_months_partition_and_repeats_are_ignored(
    db_conn: psycopg.Connection[Any],
) -> None:
    now = datetime.now(UTC)
    db_conn.execute(_INSERT, (7001, now, "table_probe"))
    db_conn.execute(_INSERT + " ON CONFLICT (event_uid, ts) DO NOTHING", (7001, now, "table_probe"))
    rows = db_conn.execute(
        "SELECT tableoid::regclass::text FROM telemetry_events WHERE event_uid = 7001"
    ).fetchall()
    assert rows == [(f"telemetry_events_{now:%Y%m}",)]


def test_rows_are_never_rewritten(db_conn: psycopg.Connection[Any]) -> None:
    db_conn.execute(_INSERT, (7002, datetime.now(UTC), "table_probe"))
    for statement in (
        "UPDATE telemetry_events SET level = 'error' WHERE event_uid = 7002",
        "DELETE FROM telemetry_events WHERE event_uid = 7002",
    ):
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            db_conn.execute(statement)
        db_conn.rollback()
        db_conn.execute(
            _INSERT + " ON CONFLICT (event_uid, ts) DO NOTHING", (7002, datetime.now(UTC), "x")
        )


def test_ensure_partitions_is_idempotent_and_covers_the_months_ahead(
    db_conn: psycopg.Connection[Any],
) -> None:
    db_conn.execute("SELECT ensure_telemetry_event_partitions(3)")
    db_conn.execute("SELECT ensure_telemetry_event_partitions(3)")
    names = {
        row[0]
        for row in db_conn.execute(
            "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
            "WHERE i.inhparent = 'telemetry_events'::regclass"
        ).fetchall()
    }
    now = datetime.now(UTC)
    assert "telemetry_events_default" in names
    assert f"telemetry_events_{now:%Y%m}" in names
    assert len(names) >= 5
