"""The emitter's telemetry_events sink: what lands, how it fails, how it recovers."""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from base.db.connections import NoDatabaseAuthorityError
from base.telemetry import event_store
from base.telemetry.emitter import Event, category_for_kind


@pytest.fixture(autouse=True)
def _fresh_sink() -> Iterator[None]:
    event_store.set_enabled(enabled=True)
    event_store.reset_store()
    yield
    event_store.reset_store()
    event_store.set_enabled(enabled=False)


def _event(name: str, marker: str, **attributes: Any) -> Event:
    return Event(
        ts=datetime.now(UTC),
        trace_id=None,
        span_id=None,
        agent_id=None,
        machine=marker,
        cluster="store-cluster",
        process="store-proc",
        category=category_for_kind(name),
        event_name=name,
        level="info",
        source="system",
        target_agent_id=None,
        attributes=attributes,
    )


def _rows(db_conn: psycopg.Connection[Any], marker: str) -> list[tuple[Any, ...]]:
    return db_conn.execute(
        "SELECT event_name, category, cluster, attributes FROM telemetry_events "
        "WHERE machine = %s ORDER BY event_name",
        (marker,),
    ).fetchall()


def test_telemetry_and_log_events_land_and_audit_events_do_not(
    db_conn: psycopg.Connection[Any],
) -> None:
    marker = uuid4().hex
    batch = [_event("llm_usage", marker), _event("log", marker), _event("spawn", marker)]
    event_store.store_events(batch)
    event_store.store_events(batch)  # a redelivered batch adds nothing

    assert [(r[0], r[1], r[2]) for r in _rows(db_conn, marker)] == [
        ("llm_usage", "telemetry", "store-cluster"),
        ("log", "log", "store-cluster"),
    ]


def test_a_nul_character_is_replaced_not_fatal(db_conn: psycopg.Connection[Any]) -> None:
    marker = uuid4().hex
    event_store.store_events([_event("exec", marker, body="a\x00b")])

    [row] = _rows(db_conn, marker)
    assert row[3]["body"] == "a�b"


def test_a_failed_batch_is_reported_once_then_backs_off_and_recovers(
    db_conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = uuid4().hex
    emitted: list[dict[str, Any]] = []
    attempts: list[int] = []
    real_write = event_store._write

    def failing_write(records: list[dict[str, Any]]) -> None:
        attempts.append(len(records))
        raise psycopg.OperationalError("database is down")

    monkeypatch.setattr(event_store, "_write", failing_write)

    def capture_emit(*_args: object, **kwargs: Any) -> None:
        emitted.append(kwargs["attributes"])

    monkeypatch.setattr("base.telemetry.emit", capture_emit)
    event_store.store_events([_event("llm_usage", marker)])
    event_store.store_events([_event("llm_usage", marker)])  # inside the backoff window

    assert attempts == [1]
    assert [(e["rows"], e["consecutive_failures"], e["error_class"]) for e in emitted] == [
        (1, 1, "OperationalError")
    ]
    assert _rows(db_conn, marker) == []

    monkeypatch.setattr(event_store, "_write", real_write)
    event_store._retry_at = time.monotonic() - 1
    event_store.store_events([_event("llm_usage", marker)])

    assert len(_rows(db_conn, marker)) == 1
    assert event_store._failures == 0


def test_a_process_without_database_authority_turns_the_sink_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[int] = []

    def refused(records: list[dict[str, Any]]) -> None:
        attempts.append(len(records))
        raise NoDatabaseAuthorityError("no login for this home")

    monkeypatch.setattr(event_store, "_write", refused)
    event_store.store_events([_event("llm_usage", "x")])
    event_store.store_events([_event("llm_usage", "x")])

    assert attempts == [1]
