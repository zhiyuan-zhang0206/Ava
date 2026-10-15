from __future__ import annotations

import json
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import psycopg

from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from gateway.lgtm import telemetry_staleness
from gateway.lgtm.telemetry_staleness import TelemetryStaleness


class _Pool:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    @contextmanager
    def connection(self) -> Generator[psycopg.Connection]:
        yield self._conn


def _heartbeat(conn: psycopg.Connection, at: datetime, *, event: str = "gateway_latency") -> None:
    conn.execute(
        "INSERT INTO telemetry_events (event_uid, ts, machine, cluster, process, category, "
        "event_name, level, source, attributes) VALUES (%s, %s, 'm', 'c', 'p', 'telemetry', %s, "
        "'info', 'test', %s::jsonb)",
        (uuid.uuid4().int % (1 << 62), at, event, json.dumps({})),
    )


def test_heartbeat_age_reads_the_newest_heartbeat_row(db_conn: psycopg.Connection) -> None:
    db_conn.autocommit = True
    now = datetime(2001, 3, 4, 12, 0, tzinfo=UTC) + timedelta(seconds=uuid.uuid4().int % 100000)
    pool = _Pool(db_conn)

    assert telemetry_staleness.heartbeat_age(pool, now=now) is None

    _heartbeat(db_conn, now - timedelta(seconds=90))
    _heartbeat(db_conn, now - timedelta(seconds=30))
    _heartbeat(db_conn, now - timedelta(seconds=5), event="llm_usage")
    assert telemetry_staleness.heartbeat_age(pool, now=now) == 30.0

    later = now + timedelta(seconds=2 * telemetry_staleness.STALENESS_THRESHOLD_S + 31)
    assert telemetry_staleness.heartbeat_age(pool, now=later) is None


def test_check_reports_stale_rate_limits_and_recovers(emitted_events: EmittedEvents) -> None:
    ages: dict[str, float | None] = {"postgres": 30.0}

    def postgres_age(_pool: object, *, now: datetime) -> float | None:
        del now
        return ages["postgres"]

    guard = TelemetryStaleness(check_interval_s=0, read_heartbeat_age=postgres_age)

    started = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)

    assert guard.check_and_report(None, now=started) is False
    assert emitted_events.names() == []

    ages["postgres"] = None
    assert guard.check_and_report(None, now=started) is True
    assert emitted_events.names() == ["telemetry_read_stale"]
    assert emitted_events.attributes("telemetry_read_stale") == [
        {
            "source": "postgres",
            "signal": "gateway_latency",
            "threshold_s": 300,
            "age_s": None,
            "action": "entered",
            "reason": "heartbeat missing",
        }
    ]

    assert guard.check_and_report(None, now=datetime(2026, 8, 23, 12, 4, 59, tzinfo=UTC))
    assert len(emitted_events) == 1

    assert guard.check_and_report(None, now=datetime(2026, 8, 23, 12, 5, tzinfo=UTC))
    assert emitted_events.names() == ["telemetry_read_stale"] * 2
    assert emitted_events.attributes("telemetry_read_stale")[-1]["action"] == "ongoing"

    ages["postgres"] = 30.0
    assert guard.check_and_report(None, now=datetime(2026, 8, 23, 12, 6, tzinfo=UTC)) is False
    assert emitted_events.names()[-1] == "telemetry_read_recovered"
    assert emitted_events.attributes("telemetry_read_recovered") == [
        {
            "source": "postgres",
            "signal": "gateway_latency",
            "stale_duration_s": 360.0,
        }
    ]


def test_check_throttles_heartbeat_queries() -> None:
    calls = 0
    monotonic_times = iter((100.0, 100.1, 160.1))

    def heartbeat_age(_pool: object, *, now: datetime) -> float:
        nonlocal calls
        del now
        calls += 1
        return 30.0

    guard = TelemetryStaleness(
        check_interval_s=60,
        read_heartbeat_age=heartbeat_age,
        monotonic=lambda: next(monotonic_times),
    )

    assert guard.check_and_report(None) is False
    assert guard.check_and_report(None) is False
    assert calls == 1

    assert guard.check_and_report(None) is False
    assert calls == 2


def test_check_fail_open_when_heartbeat_query_raises(emitted_events: EmittedEvents) -> None:
    def boom(_pool: object, *, now: datetime) -> float | None:
        raise RuntimeError(f"backend failed at {now}")

    guard = TelemetryStaleness(check_interval_s=0, read_heartbeat_age=boom)

    assert guard.check_and_report(None) is False
    assert emitted_events.names() == []
