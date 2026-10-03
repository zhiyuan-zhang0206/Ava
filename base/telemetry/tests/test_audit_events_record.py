"""`audit_events` — the Postgres system of record for category=audit events.

`record_audit` writes in the caller's transaction, `record_audit_standalone`
in its own short one, and the table itself rejects every rewrite. The per-test
TRUNCATE cannot clear the table (the trigger refuses it by design), so every
test keys its rows on a unique `source` and never assumes an empty table.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any, LiteralString, cast

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from base import telemetry
from base.db.transaction import async_write_transaction
from base.telemetry import audit_events
from base.telemetry.audit_events import record_audit, record_audit_standalone


def _marker() -> str:
    return f"test:{uuid.uuid4().hex}"


def _event(marker: str, **overrides: Any) -> telemetry.Event:
    fields: dict[str, Any] = {
        "agent_id": 7001,
        "source": marker,
        "target_agent_id": 7002,
        "attributes": {"content": "héllo ✓", "inbound_id": 3},
    }
    fields.update(overrides)
    return telemetry.prepare_event("audit", "send_message", **fields)


def _rows(conn: psycopg.Connection, marker: str) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT event_uid, agent_id, target_agent_id, event_name, level, source, machine, "
        "process, attributes, imported_from FROM audit_events WHERE source = %s",
        (marker,),
    ).fetchall()


def test_record_audit_writes_the_event_columns(db_conn: psycopg.Connection) -> None:
    marker = _marker()
    event = _event(marker)

    returned = record_audit(db_conn, event)
    db_conn.commit()

    assert returned is event
    ((uid, agent_id, target, name, level, source, machine, process, attributes, imported),) = _rows(
        db_conn, marker
    )
    assert uid == audit_events.audit_event_uid(event)
    assert (agent_id, target, name, level, source) == (7001, 7002, "send_message", "info", marker)
    assert (machine, process) == (event.machine, event.process)
    assert attributes == {"content": "héllo ✓", "inbound_id": 3}
    assert imported is None


def test_record_audit_is_idempotent_on_the_event_id(db_conn: psycopg.Connection) -> None:
    marker = _marker()
    event = _event(marker)

    record_audit(db_conn, event)
    record_audit(db_conn, event)
    db_conn.commit()

    assert len(_rows(db_conn, marker)) == 1


def test_record_audit_commits_and_rolls_back_with_the_business_transaction(
    db_conn: psycopg.Connection,
) -> None:
    rolled_back, committed = _marker(), _marker()

    record_audit(db_conn, _event(rolled_back))
    db_conn.rollback()
    record_audit(db_conn, _event(committed))
    db_conn.commit()

    assert _rows(db_conn, rolled_back) == []
    assert len(_rows(db_conn, committed)) == 1


def test_record_audit_refuses_an_event_that_is_not_registered_audit(
    db_conn: psycopg.Connection,
) -> None:
    marker = _marker()
    telemetry_event = telemetry.prepare_event("telemetry", "turn_end", source=marker)
    wrong_category = replace(_event(marker), category="telemetry")
    undeclared = replace(_event(marker), event_name="turn_end", category="audit")

    for event in (telemetry_event, wrong_category, undeclared):
        with pytest.raises(ValueError, match="registered category=audit"):
            record_audit(db_conn, event)

    assert _rows(db_conn, marker) == []


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_events SET level = 'error' WHERE source = %s",
        "DELETE FROM audit_events WHERE source = %s",
        "TRUNCATE audit_events",
    ],
)
def test_audit_events_reject_every_rewrite(
    db_conn: psycopg.Connection, statement: LiteralString
) -> None:
    marker = _marker()
    record_audit(db_conn, _event(marker))
    db_conn.commit()

    params = (marker,) if "%s" in statement else None
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        db_conn.execute(statement, params)
    db_conn.rollback()

    assert len(_rows(db_conn, marker)) == 1


@pytest.mark.parametrize(
    ("stream_id", "stored"),
    [(0, 0), (5, 5), ((1 << 63) - 1, (1 << 63) - 1), (1 << 63, -(1 << 63)), ((1 << 64) - 1, -1)],
)
def test_the_stream_id_maps_onto_a_signed_bigint(
    monkeypatch: pytest.MonkeyPatch, stream_id: int, stored: int
) -> None:
    def fixed_id(_line: str, _ts_ns: int) -> int:
        return stream_id

    monkeypatch.setattr(telemetry, "event_id", fixed_id)

    assert audit_events.audit_event_uid(_event(_marker())) == stored


def test_standalone_commits_the_row_before_it_emits(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = _marker()
    seen_at_emit: list[int] = []

    def emit(event: telemetry.Event) -> None:
        seen_at_emit.append(len(_rows(db_conn, event.source)))

    monkeypatch.setattr(telemetry, "emit_prepared", emit)

    record_audit_standalone(_event(marker))

    assert seen_at_emit == [1]


def test_standalone_failure_raises_and_emits_nothing(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = _marker()
    emitted: list[telemetry.Event] = []
    monkeypatch.setattr(telemetry, "emit_prepared", emitted.append)
    bad_level = replace(_event(marker), level=cast(Any, "loud"))

    with pytest.raises(psycopg.errors.CheckViolation):
        record_audit_standalone(bad_level)

    assert emitted == []
    assert _rows(db_conn, marker) == []


async def test_async_record_commits_with_the_callers_transaction(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    rolled_back, committed = _marker(), _marker()

    with pytest.raises(RuntimeError, match="business write failed"):
        async with async_write_transaction(aops_pool) as conn:
            await audit_events.record_audit_async(conn, _event(rolled_back))
            raise RuntimeError("business write failed")
    async with async_write_transaction(aops_pool) as conn:
        await audit_events.record_audit_async(conn, _event(committed))

    assert _rows(db_conn, rolled_back) == []
    assert len(_rows(db_conn, committed)) == 1


async def test_async_standalone_commits_the_row_before_it_emits(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = _marker()
    seen_at_emit: list[int] = []

    def emit(event: telemetry.Event) -> None:
        seen_at_emit.append(len(_rows(db_conn, event.source)))

    monkeypatch.setattr(telemetry, "emit_prepared", emit)

    await audit_events.record_audit_standalone_async(aops_pool, _event(marker))

    assert seen_at_emit == [1]


def test_reported_recording_does_not_raise_but_reports_and_still_projects(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = _marker()
    emitted: list[telemetry.Event] = []
    reports: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(telemetry, "emit_prepared", emitted.append)

    def report(_category: str, name: str, **kwargs: Any) -> None:
        reports.append((name, kwargs["attributes"]))

    monkeypatch.setattr(telemetry, "emit", report)
    bad_level = replace(_event(marker), level=cast(Any, "loud"))

    audit_events.record_audit_reported(bad_level)

    assert _rows(db_conn, marker) == []
    assert emitted == [bad_level]
    [(name, attributes)] = reports
    assert name == "audit_write_failed"
    assert (attributes["event_name"], attributes["error_class"]) == (
        "send_message",
        "CheckViolation",
    )


def test_reported_recording_of_a_healthy_write_reports_nothing(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = _marker()
    reports: list[str] = []

    def emit_prepared(_event: telemetry.Event) -> None:
        return None

    def emit(_category: str, name: str, **_kwargs: Any) -> None:
        reports.append(name)

    monkeypatch.setattr(telemetry, "emit_prepared", emit_prepared)
    monkeypatch.setattr(telemetry, "emit", emit)

    audit_events.record_audit_reported(_event(marker))

    assert len(_rows(db_conn, marker)) == 1
    assert reports == []


def test_standalone_many_commits_every_row_together_and_emits_after(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = _marker(), _marker()
    seen_at_emit: list[int] = []

    def emit(event: telemetry.Event) -> None:
        seen_at_emit.append(len(_rows(db_conn, event.source)))

    monkeypatch.setattr(telemetry, "emit_prepared", emit)

    audit_events.record_audit_standalone_many([_event(first), _event(second)])

    assert seen_at_emit == [1, 1]


def test_standalone_many_with_one_refused_event_records_and_emits_none(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    good, bad = _marker(), _marker()
    emitted: list[telemetry.Event] = []
    monkeypatch.setattr(telemetry, "emit_prepared", emitted.append)

    with pytest.raises(psycopg.errors.CheckViolation):
        audit_events.record_audit_standalone_many(
            [_event(good), replace(_event(bad), level=cast(Any, "loud"))]
        )

    assert emitted == []
    assert _rows(db_conn, good) == []
