"""`base.events.audit_rows` — reads of the audit record in the event stream's row shape."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import psycopg

from base.events import audit_rows


def _record(
    db: psycopg.Connection,
    name: str,
    *,
    hours_ago: float = 0.0,
    uid: int | None = None,
    agent_id: int | None = 5,
    target: int | None = None,
    level: str = "info",
) -> int:
    event_uid = uuid.uuid4().int % (1 << 62) if uid is None else uid
    db.execute(
        "INSERT INTO audit_events (event_uid, ts, machine, process, event_name, level, source, "
        "agent_id, target_agent_id) "
        "VALUES (%s, now() - (%s * interval '1 hour'), 'm', 'p', %s, %s, 'test', %s, %s)",
        (event_uid, hours_ago, name, level, agent_id, target),
    )
    db.commit()
    return event_uid


def test_rows_carry_the_stream_shape_and_the_unsigned_stream_id(
    db_conn: psycopg.Connection,
) -> None:
    _record(db_conn, "spawn", uid=-1)

    [row], more = audit_rows.query_events(db_conn)

    assert more is False
    assert row["id"] == (1 << 64) - 1  # the signed column value -1, as the stream carries it
    assert row["category"] == "audit"
    assert len(row["line_sha256"]) == 64
    assert row["ts"].tzinfo is not None
    assert set(row) == {
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


def test_paging_uses_a_lookahead_and_either_direction(db_conn: psycopg.Connection) -> None:
    for hours in (3, 2, 1):
        _record(db_conn, "spawn", hours_ago=hours)

    newest, more = audit_rows.query_events(db_conn, limit=2)
    oldest, _ = audit_rows.query_events(db_conn, limit=2, direction="forward")
    tail, tail_more = audit_rows.query_events(db_conn, limit=2, offset=2)

    assert more is True
    assert newest[0]["ts"] > newest[1]["ts"]
    assert oldest[0]["ts"] < oldest[1]["ts"]
    assert len(tail) == 1 and tail_more is False


def test_tier_filters_split_audit_rows_by_level(db_conn: psycopg.Connection) -> None:
    _record(db_conn, "spawn")
    _record(db_conn, "env_unauthorized_write", level="warning")

    business, _ = audit_rows.query_events(db_conn, tiers=["business"])
    anomaly, _ = audit_rows.query_events(db_conn, tiers=["anomaly"])
    both, _ = audit_rows.query_events(db_conn, tiers=["business", "anomaly"])
    none, _ = audit_rows.query_events(db_conn, tiers=["observation", "noise"])

    assert [row["event_name"] for row in business] == ["spawn"]
    assert [row["event_name"] for row in anomaly] == ["env_unauthorized_write"]
    assert len(both) == 2
    assert none == []
    assert audit_rows.count_events(db_conn, tiers=["observation"]) == 0
    assert audit_rows.count_events(db_conn, tiers=["business"]) == 1


def test_the_window_bounds_are_inclusive(db_conn: psycopg.Connection) -> None:
    _record(db_conn, "spawn", hours_ago=10)
    _record(db_conn, "spawn", hours_ago=2)
    now = datetime.now(UTC)

    rows, _ = audit_rows.query_events(db_conn, from_=now - timedelta(hours=3), to=now)

    assert len(rows) == 1


def test_edge_counts_group_per_agent_target_and_name_and_skip_incomplete_edges(
    db_conn: psycopg.Connection,
) -> None:
    _record(db_conn, "spawn", agent_id=1, target=2)
    _record(db_conn, "send_message", agent_id=1, target=2)
    _record(db_conn, "send_message", agent_id=1, target=2, hours_ago=5)
    _record(db_conn, "send_message", agent_id=None, target=2)
    _record(db_conn, "terminate", agent_id=1, target=2)  # not an edge event

    counts = {(a, t, name): count for a, t, name, count, _last in audit_rows.edge_counts(db_conn)}

    assert counts == {(1, 2, "spawn"): 1, (1, 2, "send_message"): 2}


def test_attribute_filters_compare_as_text_so_a_json_number_matches_its_string_form(
    db_conn: psycopg.Connection,
) -> None:
    for task_id in (42, 43):
        db_conn.execute(
            "INSERT INTO audit_events (event_uid, ts, machine, process, event_name, level, source, "
            "attributes) VALUES (%s, now(), 'm', 'p', 'computer_action', 'info', 'test', %s::jsonb)",
            (uuid.uuid4().int % (1 << 62), f'{{"task_id": {task_id}, "path": null}}'),
        )
    db_conn.commit()

    assert audit_rows.count_events(db_conn, attribute_filters={"task_id": "42"}) == 1
    assert audit_rows.count_events(db_conn, attribute_filters={"task_id": "!=42"}) == 1
    assert audit_rows.count_events(db_conn, attribute_filters={"nonexistent": "!="}) == 0
