"""GET /api/events reads the audit record from Postgres and the rest from Loki.

`category=audit` (and an audit-only event name) is answered from `audit_events`; telemetry and
log from Loki; everything else merges both newest-first, with one page slice over the merged
rows. Postgres is real; Loki is replaced by a recorder with canned rows.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.lgtm import loki_events

_NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def _audit(
    db: psycopg.Connection,
    name: str,
    *,
    minutes_ago: float,
    agent_id: int | None = 7,
    level: str = "info",
    machine: str = "m1",
    trace_id: str | None = None,
    attributes: str = "{}",
) -> int:
    uid = uuid.uuid4().int % (1 << 62)
    db.execute(
        "INSERT INTO audit_events (event_uid, ts, trace_id, agent_id, machine, process, "
        "event_name, level, source, attributes) "
        "VALUES (%s, now() - (%s * interval '1 minute'), %s, %s, %s, 'test', %s, %s, 'test', "
        "%s::jsonb)",
        (uid, minutes_ago, trace_id, agent_id, machine, name, level, attributes),
    )
    db.commit()
    return uid


def _loki_row(name: str, *, minutes_ago: float, row_id: int, category: str = "telemetry") -> dict:
    return {
        "id": row_id,
        "line_sha256": "a" * 64,
        "ts": datetime.now(UTC) - timedelta(minutes=minutes_ago),
        "trace_id": None,
        "span_id": None,
        "agent_id": 7,
        "machine": "m1",
        "process": "test",
        "category": category,
        "event_name": name,
        "level": "info",
        "source": "test",
        "target_agent_id": None,
        "attributes": {},
    }


class _Loki:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.total = 0


@pytest.fixture
def loki(monkeypatch: pytest.MonkeyPatch) -> _Loki:
    fake = _Loki()

    def query(**kwargs: Any) -> tuple[list[dict[str, Any]], bool]:
        fake.calls.append(kwargs)
        ordered = sorted(fake.rows, key=lambda row: row["ts"], reverse=True)
        window = ordered[kwargs["offset"] : kwargs["offset"] + kwargs["limit"] + 1]
        return window[: kwargs["limit"]], len(window) > kwargs["limit"]

    def count(**kwargs: Any) -> int:
        fake.calls.append(kwargs)
        return fake.total

    monkeypatch.setattr(loki_events, "query_events", query)
    monkeypatch.setattr(loki_events, "count_events", count)
    return fake


def _get(**params: Any) -> dict[str, Any]:
    with TestClient(app) as client:
        response = client.get("/api/events", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_the_audit_category_is_served_from_postgres_not_loki(
    db_conn: psycopg.Connection, loki: _Loki
) -> None:
    older = _audit(db_conn, "send_message", minutes_ago=30)
    newer = _audit(db_conn, "spawn", minutes_ago=10)

    body = _get(category="audit")

    assert [item["event_name"] for item in body["items"]] == ["spawn", "send_message"]
    assert [item["id"] for item in body["items"]] == [newer, older]
    assert {item["category"] for item in body["items"]} == {"audit"}
    assert {item["tier"] for item in body["items"]} == {"business"}
    assert loki.calls == []


def test_an_audit_only_event_name_skips_loki_and_a_telemetry_name_skips_postgres(
    db_conn: psycopg.Connection, loki: _Loki
) -> None:
    _audit(db_conn, "spawn", minutes_ago=5)
    loki.rows = [_loki_row("llm_usage", minutes_ago=3, row_id=11)]

    spawn = _get(event_name="spawn")
    assert [item["event_name"] for item in spawn["items"]] == ["spawn"]
    assert loki.calls == []

    usage = _get(event_name="llm_usage")
    assert [item["event_name"] for item in usage["items"]] == ["llm_usage"]


def test_without_a_category_both_stores_merge_newest_first(
    db_conn: psycopg.Connection, loki: _Loki
) -> None:
    _audit(db_conn, "send_message", minutes_ago=40)
    _audit(db_conn, "spawn", minutes_ago=20)
    loki.rows = [
        _loki_row("llm_usage", minutes_ago=30, row_id=1),
        _loki_row("turn_end", minutes_ago=10, row_id=2),
    ]

    body = _get()

    assert [item["event_name"] for item in body["items"]] == [
        "turn_end",
        "spawn",
        "llm_usage",
        "send_message",
    ]
    assert loki.calls[0]["categories"] == ["telemetry", "log"]


def test_a_merged_page_slices_after_the_merge_and_reports_more(
    db_conn: psycopg.Connection, loki: _Loki
) -> None:
    for minutes in (10, 30, 50):
        _audit(db_conn, "spawn", minutes_ago=minutes)
    loki.rows = [
        _loki_row("llm_usage", minutes_ago=20, row_id=1),
        _loki_row("llm_usage", minutes_ago=40, row_id=2),
    ]

    first = _get(limit=2)
    second = _get(limit=2, offset=2)
    last = _get(limit=2, offset=4)

    assert [item["event_name"] for item in first["items"]] == ["spawn", "llm_usage"]
    assert first["meta"]["has_more"] is True
    assert [item["event_name"] for item in second["items"]] == ["spawn", "llm_usage"]
    assert second["meta"]["has_more"] is True
    assert [item["event_name"] for item in last["items"]] == ["spawn"]
    assert last["meta"]["has_more"] is False
    # Each store is asked for its newest `offset + limit` rows, never past them.
    assert loki.calls[0]["limit"] == 2 and loki.calls[0]["offset"] == 0
    assert loki.calls[2]["limit"] == 6 and loki.calls[2]["offset"] == 0


def test_the_total_sums_both_stores(db_conn: psycopg.Connection, loki: _Loki) -> None:
    _audit(db_conn, "spawn", minutes_ago=5)
    _audit(db_conn, "send_message", minutes_ago=6)
    loki.total = 41

    assert _get(with_total=1)["meta"]["total"] == 43
    assert _get(category="audit", with_total=1)["meta"]["total"] == 2


def test_audit_rows_honour_the_agent_level_machine_trace_and_window_filters(
    db_conn: psycopg.Connection, loki: _Loki
) -> None:
    _audit(db_conn, "spawn", minutes_ago=5, agent_id=1, machine="m1", trace_id="ab12")
    _audit(db_conn, "spawn", minutes_ago=5, agent_id=2, machine="m2")
    _audit(db_conn, "env_unauthorized_write", minutes_ago=5, agent_id=1, level="warning")
    _audit(db_conn, "spawn", minutes_ago=3000, agent_id=1)  # outside the default 24h window

    assert len(_get(category="audit", agent_id=1)["items"]) == 2
    assert len(_get(category="audit", agent_id=1, hours=100)["items"]) == 3
    assert len(_get(category="audit", machine="m2")["items"]) == 1
    assert len(_get(category="audit", trace_id="AB12")["items"]) == 1
    assert [i["event_name"] for i in _get(category="audit", level="WARNING")["items"]] == [
        "env_unauthorized_write"
    ]


def test_tiers_select_audit_rows_by_level(db_conn: psycopg.Connection, loki: _Loki) -> None:
    _audit(db_conn, "spawn", minutes_ago=5)
    _audit(db_conn, "env_unauthorized_write", minutes_ago=6, level="warning")

    business = _get(category="audit", tier="business")["items"]
    anomaly = _get(category="audit", tier="anomaly")["items"]
    observation = _get(category="audit", tier="observation")["items"]

    assert [item["event_name"] for item in business] == ["spawn"]
    assert [item["event_name"] for item in anomaly] == ["env_unauthorized_write"]
    assert {item["tier"] for item in anomaly} == {"anomaly"}
    assert observation == []


def test_the_impersonation_session_filter_matches_the_recorded_attribute(
    db_conn: psycopg.Connection, loki: _Loki
) -> None:
    _audit(db_conn, "send_message", minutes_ago=5, attributes='{"impersonation_session": "7:1"}')
    _audit(db_conn, "send_message", minutes_ago=6)

    items = _get(category="audit", impersonation_session="7:1")["items"]

    assert len(items) == 1
