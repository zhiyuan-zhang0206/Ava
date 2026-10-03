"""GET /api/events reads `audit_events` and `telemetry_events` and merges them.

`category=audit` (and an audit-only event name) is answered from `audit_events`; telemetry and
log from `telemetry_events`; everything else merges both newest-first, with one page slice over
the merged rows.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import psycopg
from fastapi.testclient import TestClient

from gateway.app import app

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


def _telemetry(
    db: psycopg.Connection,
    name: str,
    *,
    minutes_ago: float,
    category: str = "telemetry",
    agent_id: int | None = 7,
) -> int:
    uid = uuid.uuid4().int % (1 << 62)
    db.execute(
        "INSERT INTO telemetry_events (event_uid, ts, agent_id, machine, cluster, process, "
        "category, event_name, level, source) "
        "VALUES (%s, now() - (%s * interval '1 minute'), %s, 'm1', 'c', 'test', %s, %s, 'info', "
        "'test')",
        (uid, minutes_ago, agent_id, category, name),
    )
    db.commit()
    return uid


def _get(**params: Any) -> dict[str, Any]:
    with TestClient(app) as client:
        response = client.get("/api/events", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_the_audit_category_is_served_from_audit_events_alone(db_conn: psycopg.Connection) -> None:
    older = _audit(db_conn, "send_message", minutes_ago=30)
    newer = _audit(db_conn, "spawn", minutes_ago=10)

    body = _get(category="audit")

    assert [item["event_name"] for item in body["items"]] == ["spawn", "send_message"]
    assert [item["id"] for item in body["items"]] == [newer, older]
    assert {item["category"] for item in body["items"]} == {"audit"}
    assert {item["tier"] for item in body["items"]} == {"business"}


def test_an_audit_only_event_name_skips_telemetry_and_a_telemetry_name_skips_audit(
    db_conn: psycopg.Connection,
) -> None:
    _audit(db_conn, "spawn", minutes_ago=5)
    _telemetry(db_conn, "llm_usage", minutes_ago=3)

    spawn = _get(event_name="spawn")
    assert [item["event_name"] for item in spawn["items"]] == ["spawn"]

    usage = _get(event_name="llm_usage")
    assert [item["event_name"] for item in usage["items"]] == ["llm_usage"]


def test_without_a_category_both_tables_merge_newest_first(db_conn: psycopg.Connection) -> None:
    _audit(db_conn, "send_message", minutes_ago=40)
    _audit(db_conn, "spawn", minutes_ago=20)
    _telemetry(db_conn, "llm_usage", minutes_ago=30)
    _telemetry(db_conn, "turn_end", minutes_ago=10)

    body = _get()

    assert [item["event_name"] for item in body["items"]] == [
        "turn_end",
        "spawn",
        "llm_usage",
        "send_message",
    ]


def test_a_merged_page_slices_after_the_merge_and_reports_more(db_conn: psycopg.Connection) -> None:
    for minutes in (10, 30, 50):
        _audit(db_conn, "spawn", minutes_ago=minutes)
    _telemetry(db_conn, "llm_usage", minutes_ago=20)
    _telemetry(db_conn, "llm_usage", minutes_ago=40)

    first = _get(limit=2)
    second = _get(limit=2, offset=2)
    last = _get(limit=2, offset=4)

    assert [item["event_name"] for item in first["items"]] == ["spawn", "llm_usage"]
    assert first["meta"]["has_more"] is True
    assert [item["event_name"] for item in second["items"]] == ["spawn", "llm_usage"]
    assert second["meta"]["has_more"] is True
    assert [item["event_name"] for item in last["items"]] == ["spawn"]
    assert last["meta"]["has_more"] is False


def test_the_total_sums_both_tables(db_conn: psycopg.Connection) -> None:
    _audit(db_conn, "spawn", minutes_ago=5)
    _audit(db_conn, "send_message", minutes_ago=6)
    for minutes in range(3):
        _telemetry(db_conn, "llm_usage", minutes_ago=minutes + 1)

    assert _get(with_total=1)["meta"]["total"] == 5
    assert _get(category="audit", with_total=1)["meta"]["total"] == 2


def test_audit_rows_honour_the_agent_level_machine_trace_and_window_filters(
    db_conn: psycopg.Connection,
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


def test_tiers_select_audit_rows_by_level(db_conn: psycopg.Connection) -> None:
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
    db_conn: psycopg.Connection,
) -> None:
    _audit(db_conn, "send_message", minutes_ago=5, attributes='{"impersonation_session": "7:1"}')
    _audit(db_conn, "send_message", minutes_ago=6)

    items = _get(category="audit", impersonation_session="7:1")["items"]

    assert len(items) == 1
