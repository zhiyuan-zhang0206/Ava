"""Recoverable observations: recovery from telemetry_events commutes with the live projection."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from base.telemetry.loki_index_labels import ARCHIVE_FREEZE_AT
from base.telemetry.metrics.observed_metrics import observe_row, write_observations
from services.events_maintenance import observed_metrics as replay


def _agent(db: psycopg.Connection) -> int:
    row = db.execute("INSERT INTO agents(label) VALUES ('replay') RETURNING id").fetchone()
    assert row is not None
    db.commit()
    return int(row[0])


def _record(
    db: psycopg.Connection,
    agent_id: int | None,
    *,
    uid: int,
    name: str = "llm_usage",
    attributes: dict[str, Any] | None = None,
    ts: datetime | None = None,
) -> None:
    db.execute(
        "INSERT INTO telemetry_events (event_uid, ts, agent_id, machine, cluster, process, "
        "category, event_name, level, source, attributes) VALUES (%s, %s, %s, 'm', 'c', 'p', "
        "'telemetry', %s, 'info', 'test', %s::jsonb)",
        (
            uid,
            ts or datetime.now(UTC) - timedelta(minutes=5),
            agent_id,
            name,
            json.dumps(attributes or {"in_total": 5, "out_total": 2, "cost_usd": 0.25}),
        ),
    )
    db.commit()


def _calls(db: psycopg.Connection) -> int:
    row = db.execute("SELECT COALESCE(sum(usage_calls),0) FROM agent_metric_days").fetchone()
    assert row is not None
    db.commit()
    return int(row[0])


def _stream_id(uid: int) -> int:
    return uid + (1 << 64) if uid < 0 else uid


def test_recovery_commutes_with_the_live_projection_and_repeats_harmlessly(
    db_conn: psycopg.Connection,
) -> None:
    agent = _agent(db_conn)
    first = {
        "id": 1,
        "agent_id": agent,
        "ts": datetime.now(UTC).isoformat(),
        "event_name": "llm_usage",
        "category": "telemetry",
        "attributes": {"in_total": 5, "out_total": 2, "cost_usd": 0.25},
    }
    observation = observe_row(first)
    assert observation is not None
    write_observations([observation], db=db_conn)
    db_conn.commit()
    _record(db_conn, agent, uid=1)  # the same event, already projected live

    assert replay.recover_observations(db_conn) == 0
    assert _calls(db_conn) == 1

    _record(db_conn, agent, uid=2)  # an event the live projection missed
    assert replay.recover_observations(db_conn) == 1
    assert _calls(db_conn) == 2
    assert replay.recover_observations(db_conn) == 0


def test_the_signed_event_uid_maps_to_the_unsigned_observation_identity(
    db_conn: psycopg.Connection,
) -> None:
    agent = _agent(db_conn)
    _record(db_conn, agent, uid=-5)

    assert replay.recover_observations(db_conn) == 1

    stored = db_conn.execute("SELECT event_id FROM agent_metric_observations").fetchone()
    assert stored is not None and int(stored[0]) == _stream_id(-5)


def test_archive_owned_unknown_agent_and_malformed_rows_are_skipped_without_blocking_the_rest(
    db_conn: psycopg.Connection,
) -> None:
    agent = _agent(db_conn)
    _record(db_conn, agent, uid=10, ts=ARCHIVE_FREEZE_AT - timedelta(seconds=1))
    _record(db_conn, None, uid=11)
    _record(db_conn, agent + 9999, uid=12)
    _record(db_conn, agent, uid=13, attributes={"cost_usd": "not a price"})
    _record(db_conn, agent, uid=14, name="turn_end", attributes={"ok": True, "duration_seconds": 2})
    _record(db_conn, agent, uid=15, name="log")
    _record(db_conn, agent, uid=16, name="exec_envelope")  # a wrapper, not an outcome

    assert replay.recover_observations(db_conn) == 1

    rows = db_conn.execute("SELECT kind FROM agent_metric_observations").fetchall()
    assert rows == [("turn",)]


def test_pages_advance_past_every_row(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _agent(db_conn)
    for uid in range(20, 25):
        _record(db_conn, agent, uid=uid, ts=datetime.now(UTC) - timedelta(minutes=30 - uid))
    monkeypatch.setattr(replay, "_PAGE_LIMIT", 2)

    assert replay.recover_observations(db_conn) == 5
    assert _calls(db_conn) == 5
