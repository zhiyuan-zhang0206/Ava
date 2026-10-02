"""`telemetry_rows` answers the Loki reader's filters from `telemetry_events`."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import psycopg

from gateway.events import telemetry_rows

_NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def _put(
    db: psycopg.Connection,
    name: str,
    *,
    seconds: int,
    agent_id: int | None = 7,
    level: str = "info",
    category: str = "telemetry",
    cluster: str = "c1",
    machine: str = "m1",
    trace_id: str | None = None,
    source: str = "test",
    attributes: dict[str, Any] | None = None,
) -> int:
    uid = uuid.uuid4().int % (1 << 62)
    db.execute(
        "INSERT INTO telemetry_events (event_uid, ts, trace_id, agent_id, machine, cluster, process, "
        "category, event_name, level, source, attributes) "
        "VALUES (%s, %s + (%s * interval '1 second'), %s, %s, %s, %s, 'p', %s, %s, %s, %s, %s::jsonb)",
        (
            uid,
            _NOW,
            seconds,
            trace_id,
            agent_id,
            machine,
            cluster,
            category,
            name,
            level,
            source,
            json.dumps(attributes or {}),
        ),
    )
    db.commit()
    return uid


def _names(db: psycopg.Connection, **filters: Any) -> list[str]:
    rows, _ = telemetry_rows.query_events(db, **filters)
    return [row["event_name"] for row in rows]


def test_rows_come_back_newest_first_with_the_event_row_shape(db_conn: psycopg.Connection) -> None:
    older = _put(db_conn, "llm_usage", seconds=1, attributes={"model": "m"})
    newer = _put(db_conn, "turn_end", seconds=2, trace_id="ab12")

    rows, has_more = telemetry_rows.query_events(db_conn, agent_id=7)

    assert [row["id"] for row in rows] == [newer, older]
    assert has_more is False
    assert set(rows[0]) == {
        "id",
        "line_sha256",
        "ts",
        "trace_id",
        "span_id",
        "agent_id",
        "machine",
        "process",
        "category",
        "event_name",
        "level",
        "source",
        "target_agent_id",
        "attributes",
    }
    assert rows[1]["attributes"] == {"model": "m"}
    forward, _ = telemetry_rows.query_events(db_conn, agent_id=7, direction="forward")
    assert [row["id"] for row in forward] == [older, newer]


def test_paging_reports_more_and_the_count_matches(db_conn: psycopg.Connection) -> None:
    for second in range(5):
        _put(db_conn, "llm_usage", seconds=second)

    first, more_after_first = telemetry_rows.query_events(db_conn, agent_id=7, limit=2)
    last, more_after_last = telemetry_rows.query_events(db_conn, agent_id=7, limit=2, offset=4)

    assert (len(first), more_after_first) == (2, True)
    assert (len(last), more_after_last) == (1, False)
    assert telemetry_rows.count_events(db_conn, agent_id=7) == 5


def test_agent_scope_levels_categories_and_dimensions(db_conn: psycopg.Connection) -> None:
    _put(db_conn, "llm_usage", seconds=1, agent_id=7, cluster="c1", machine="m1")
    _put(db_conn, "log", seconds=2, agent_id=8, level="warning", category="log", cluster="c2")
    _put(db_conn, "gateway_latency", seconds=3, agent_id=None, level="error", machine="m2")

    assert _names(db_conn, agent_id=7) == ["llm_usage"]
    assert _names(db_conn, service_only=True) == ["gateway_latency"]
    assert sorted(_names(db_conn, exclude_agent_ids=[7])) == ["gateway_latency", "log"]
    assert sorted(_names(db_conn, level_min="warning")) == ["gateway_latency", "log"]
    assert _names(db_conn, level="WARNING") == ["log"]
    assert _names(db_conn, categories=["log"]) == ["log"]
    assert sorted(_names(db_conn, cluster="c1")) == ["gateway_latency", "llm_usage"]
    assert _names(db_conn, cluster="c2") == ["log"]
    assert _names(db_conn, machine="m2") == ["gateway_latency"]


def test_a_window_is_inclusive_at_both_ends(db_conn: psycopg.Connection) -> None:
    for second in (1, 2, 3):
        _put(db_conn, "llm_usage", seconds=second)
    start = datetime(2026, 10, 2, 12, 0, 1, tzinfo=UTC)
    stop = datetime(2026, 10, 2, 12, 0, 3, tzinfo=UTC)

    assert telemetry_rows.count_events(db_conn, agent_id=7, from_=start, to=stop) == 3
    assert telemetry_rows.count_events(db_conn, agent_id=7, from_=start, to=start) == 1


def test_attribute_filters_compare_text_with_not_equal_and_missing(
    db_conn: psycopg.Connection,
) -> None:
    _put(db_conn, "turn_end", seconds=1, attributes={"ok": True, "cost_usd": "0.5"})
    _put(db_conn, "turn_end", seconds=2, attributes={"ok": False})
    _put(db_conn, "turn_end", seconds=3, attributes={"impersonation_session": "7:1"})

    assert telemetry_rows.count_events(db_conn, attribute_filters={"ok": "true"}) == 1
    assert telemetry_rows.count_events(db_conn, attribute_filters={"ok": "!=true"}) == 2
    assert telemetry_rows.count_events(db_conn, attribute_filters={"cost_usd": "!="}) == 1
    assert (
        telemetry_rows.count_events(db_conn, attribute_filters={"impersonation_session": "7:1"})
        == 1
    )


def test_grep_matches_the_name_source_and_payload_text_ignoring_case(
    db_conn: psycopg.Connection,
) -> None:
    _put(db_conn, "log", seconds=1, category="log", attributes={"msg": "Disk almost FULL"})
    _put(db_conn, "log", seconds=2, category="log", source="disk-guard")
    _put(db_conn, "llm_usage", seconds=3)

    assert len(_names(db_conn, grep="disk")) == 2
    assert _names(db_conn, grep="almost full") == ["log"]


def test_tiers_follow_the_declared_tier_and_severity(db_conn: psycopg.Connection) -> None:
    _put(db_conn, "llm_usage", seconds=1)  # observation
    _put(db_conn, "llm_provider_error", seconds=2)  # declared anomaly
    _put(db_conn, "log", seconds=3, category="log", level="warning")  # anomaly by level
    _put(db_conn, "hook_timing", seconds=4)  # declared noise

    assert sorted(_names(db_conn, tiers=["anomaly"])) == ["llm_provider_error", "log"]
    assert _names(db_conn, tiers=["noise"]) == ["hook_timing"]
    assert _names(db_conn, tiers=["observation"]) == ["llm_usage"]
    assert _names(db_conn, tiers=["business"]) == []
    assert telemetry_rows.count_events(db_conn, tiers=["business"]) == 0
