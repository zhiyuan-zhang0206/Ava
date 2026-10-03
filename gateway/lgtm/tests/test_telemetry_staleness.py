"""Read-side telemetry heartbeat staleness tests."""

from __future__ import annotations

import json
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from base import telemetry
from gateway.lgtm import telemetry_staleness


@pytest.fixture(autouse=True)
def _isolated_source_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(telemetry_staleness, "_source_states", {})
    monkeypatch.setattr(telemetry_staleness, "_check_state", telemetry_staleness._CheckState())
    monkeypatch.setattr(telemetry_staleness, "CHECK_INTERVAL_S", 0, raising=False)


class _Pool:
    """The one connection of a test, behind the pool's `connection()` door."""

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
    # A stretch of time of this test's own: rows of other tests never fall into its window.
    now = datetime(2001, 3, 4, 12, 0, tzinfo=UTC) + timedelta(seconds=uuid.uuid4().int % 100000)
    pool = _Pool(db_conn)

    assert telemetry_staleness.heartbeat_age(pool, now=now) is None

    _heartbeat(db_conn, now - timedelta(seconds=90))
    _heartbeat(db_conn, now - timedelta(seconds=30))
    _heartbeat(db_conn, now - timedelta(seconds=5), event="llm_usage")  # not the heartbeat
    assert telemetry_staleness.heartbeat_age(pool, now=now) == 30.0

    # Older than twice the threshold reads as missing, however many rows exist.
    later = now + timedelta(seconds=2 * telemetry_staleness.STALENESS_THRESHOLD_S + 31)
    assert telemetry_staleness.heartbeat_age(pool, now=later) is None


def test_check_reports_stale_rate_limits_and_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    ages: dict[str, float | None] = {"postgres": 30.0}
    emitted: list[tuple[str, dict[str, Any]]] = []

    def postgres_age(_pool: object, *, now: datetime) -> float | None:
        del now
        return ages["postgres"]

    monkeypatch.setattr(telemetry_staleness, "heartbeat_age", postgres_age)

    def capture_emit(
        _category: str, event_name: str, *, attributes: dict[str, Any], **_kwargs: Any
    ) -> None:
        emitted.append((event_name, attributes))

    monkeypatch.setattr(telemetry, "emit", capture_emit)
    started = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)

    assert telemetry_staleness.check_and_report(None, now=started) is False
    assert emitted == []

    ages["postgres"] = None
    assert telemetry_staleness.check_and_report(None, now=started) is True
    assert emitted == [
        (
            "telemetry_read_stale",
            {
                "source": "postgres",
                "signal": "gateway_latency",
                "threshold_s": 300,
                "age_s": None,
                "action": "entered",
                "reason": "heartbeat missing",
            },
        )
    ]

    assert telemetry_staleness.check_and_report(
        None, now=datetime(2026, 8, 23, 12, 4, 59, tzinfo=UTC)
    )
    assert len(emitted) == 1

    assert telemetry_staleness.check_and_report(None, now=datetime(2026, 8, 23, 12, 5, tzinfo=UTC))
    assert emitted[-1][0] == "telemetry_read_stale"
    assert emitted[-1][1]["action"] == "ongoing"

    ages["postgres"] = 30.0
    assert (
        telemetry_staleness.check_and_report(None, now=datetime(2026, 8, 23, 12, 6, tzinfo=UTC))
        is False
    )
    assert emitted[-1] == (
        "telemetry_read_recovered",
        {
            "source": "postgres",
            "signal": "gateway_latency",
            "stale_duration_s": 360.0,
        },
    )


def test_check_throttles_heartbeat_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    monotonic_times = iter((100.0, 100.1, 160.1))

    def heartbeat_age(_pool: object, *, now: datetime) -> float:
        nonlocal calls
        del now
        calls += 1
        return 30.0

    monkeypatch.setattr(telemetry_staleness, "CHECK_INTERVAL_S", 60)
    monkeypatch.setattr(telemetry_staleness.time, "monotonic", lambda: next(monotonic_times))
    monkeypatch.setattr(telemetry_staleness, "heartbeat_age", heartbeat_age)

    assert telemetry_staleness.check_and_report(None) is False
    assert telemetry_staleness.check_and_report(None) is False
    assert calls == 1

    assert telemetry_staleness.check_and_report(None) is False
    assert calls == 2


def test_check_fail_open_when_heartbeat_query_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    emitted: list[str] = []

    def boom(_pool: object, *, now: datetime) -> float | None:
        raise RuntimeError(f"backend failed at {now}")

    monkeypatch.setattr(telemetry_staleness, "heartbeat_age", boom)

    def capture_emit(_category: str, event_name: str, **_kwargs: Any) -> None:
        emitted.append(event_name)

    monkeypatch.setattr(telemetry, "emit", capture_emit)

    assert telemetry_staleness.check_and_report(None) is False
    assert emitted == []
