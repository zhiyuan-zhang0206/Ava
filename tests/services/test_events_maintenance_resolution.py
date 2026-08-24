"""Regression coverage for immutable event-class resolution (task #1468)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from services.events_maintenance import resolution


class _Pool:
    """Minimal pool seam over the per-test real Postgres connection."""

    class _BorrowedConnection:
        def __init__(self, conn: psycopg.Connection[Any]) -> None:
            self._conn = conn

        def __enter__(self) -> psycopg.Connection[Any]:
            return self._conn

        def __exit__(self, *_args: object) -> None:
            return None

    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def connection(self) -> _Pool._BorrowedConnection:
        return self._BorrowedConnection(self._conn)


def _event_class(
    *,
    category: str = "telemetry",
    level: str = "warning",
    event_name: str = "x",
    source: str = "test",
) -> resolution.EventClass:
    return resolution.EventClass(
        category=category, level=level, event_name=event_name, source=source
    )


def _insert_dismissal(conn: psycopg.Connection[Any], event_class: resolution.EventClass) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO event_dismissals (category, level, event_name, source, dismissed_by)
            VALUES (%s, %s, %s, %s, 0)
            """,
            (
                event_class.category,
                event_class.level,
                event_class.event_name,
                event_class.source,
            ),
        )
    conn.commit()


@pytest.fixture(autouse=True)
def _clear_dismissals(db_conn: psycopg.Connection[Any]) -> None:
    with db_conn.cursor() as cur:
        cur.execute("TRUNCATE event_dismissals")
    db_conn.commit()


def _capture_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, object]]]:
    emitted: list[tuple[str, str, dict[str, object]]] = []

    def emit(category: str, event_name: str, **kwargs: object) -> None:
        emitted.append((category, event_name, cast(dict[str, object], kwargs["attributes"])))

    monkeypatch.setattr(resolution.telemetry, "emit", emit)
    return emitted


def test_resolution_query_uses_json_fields_until_the_legacy_expiry() -> None:
    """The fixed window aggregates classes without unsafe stream-label filters."""

    assert resolution._grouped_count_query("6h") == (
        "sum by (category, level, event_name, source) "
        '(count_over_time({service_name="unknown_service"} | json | '
        'category=~"telemetry|log" | level=~"warning|error|critical" [6h]))'
    )


def test_unresolved_math_excludes_active_classes(
    db_conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    dismissed_warning = _event_class(event_name="dismissed-warning")
    dismissed_error = _event_class(level="error", event_name="dismissed-error")
    remaining_warning = _event_class(event_name="remaining-warning")
    remaining_critical = _event_class(level="critical", event_name="remaining-critical")
    _insert_dismissal(db_conn, dismissed_warning)
    _insert_dismissal(db_conn, dismissed_error)
    counts = {
        dismissed_warning: 7,
        dismissed_error: 3,
        remaining_warning: 2,
        remaining_critical: 4,
    }

    def query_class_counts(window: str, _at: datetime) -> dict[resolution.EventClass, int]:
        return counts if window == "6h" else {}

    monkeypatch.setattr(resolution, "_query_class_counts", query_class_counts)
    emitted = _capture_events(monkeypatch)

    result = resolution.run_resolution_slice(cast(ConnectionPool, _Pool(db_conn)))

    assert result == resolution.ResolutionResult(2, 4, reopened=0, auto_dismissed=0)
    assert emitted == [
        (
            "telemetry",
            "resolution_status",
            {"unresolved_warnings": 2, "unresolved_errors": 4, "window": "6h"},
        )
    ]


def test_burst_reopens_only_above_the_configured_threshold(
    db_conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    hit = _event_class(event_name="hit")
    below = _event_class(event_name="below")
    _insert_dismissal(db_conn, hit)
    _insert_dismissal(db_conn, below)
    counts = {hit: 6, below: 5}

    def query_class_counts(_window: str, _at: datetime) -> dict[resolution.EventClass, int]:
        return counts

    monkeypatch.setattr(resolution, "_query_class_counts", query_class_counts)
    monkeypatch.setattr(resolution.settings.daemon, "events_resolution_burst_threshold", 5)
    emitted = _capture_events(monkeypatch)

    result = resolution.run_resolution_slice(cast(ConnectionPool, _Pool(db_conn)))

    assert result == resolution.ResolutionResult(6, 0, reopened=1, auto_dismissed=0)
    assert emitted[0] == (
        "telemetry",
        "warning_reopened",
        {
            "category": "telemetry",
            "level": "warning",
            "event_name": "hit",
            "source": "test",
            "agent_id": None,
            "dismissed_by": 0,
            "note": "auto:burst",
            "reopened_by": "system:burst",
            "triggered_by_count": 6,
        },
    )
    assert emitted[-1][2]["unresolved_warnings"] == 6
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT event_name, status, burst_count FROM event_dismissals ORDER BY event_name"
        )
        assert cur.fetchall() == [("below", "dismissed", None), ("hit", "reopened", 6)]


def test_empty_or_failed_loki_read_never_emits_a_stale_zero(
    db_conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    emitted = _capture_events(monkeypatch)

    def no_class_counts(_window: str, _at: datetime) -> dict[resolution.EventClass, int]:
        return {}

    monkeypatch.setattr(resolution, "_query_class_counts", no_class_counts)

    assert resolution.run_resolution_slice(cast(ConnectionPool, _Pool(db_conn))) is None
    assert emitted == []

    def boom(_window: str, _at: datetime) -> dict[resolution.EventClass, int]:
        raise RuntimeError("Loki unavailable")

    monkeypatch.setattr(resolution, "_query_class_counts", boom)
    assert resolution.run_resolution_slice(cast(ConnectionPool, _Pool(db_conn))) is None
    assert emitted == []


def test_auto_dismiss_is_off_by_default(
    db_conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    stable = _event_class(event_name="stable-warning")
    monkeypatch.setattr(resolution.settings.daemon, "events_auto_dismiss_enabled", False)

    def stable_class_counts(_window: str, _at: datetime) -> dict[resolution.EventClass, int]:
        return {stable: 1}

    monkeypatch.setattr(resolution, "_query_class_counts", stable_class_counts)
    emitted = _capture_events(monkeypatch)

    result = resolution.run_resolution_slice(cast(ConnectionPool, _Pool(db_conn)))

    assert result == resolution.ResolutionResult(1, 0, reopened=0, auto_dismissed=0)
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM event_dismissals")
        assert cur.fetchone() == (0,)
    assert [event_name for _category, event_name, _attrs in emitted] == ["resolution_status"]
