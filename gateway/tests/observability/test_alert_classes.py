"""Integration tests for the alert-class reads behind the sidebar's warning/error card.

Real SQL on the throwaway test database: the events are INSERTed into `telemetry_events`, the
dismissals into `event_dismissals`, and the HTTP surface reads them back.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.telemetry.observability import cluster_label
from gateway.app import app
from gateway.cluster import alert_classes


@pytest.fixture
def client(db_conn: psycopg.Connection) -> Any:
    with db_conn.cursor() as cur:
        cur.execute("TRUNCATE event_dismissals RESTART IDENTITY")
    db_conn.commit()
    with TestClient(app) as test_client:
        yield test_client


def _event(
    db: psycopg.Connection,
    event_name: str,
    *,
    level: str = "warning",
    source: str = "svc",
    process: str = "gateway",
    category: str = "telemetry",
    minutes_ago: float = 1,
    cluster: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> None:
    db.execute(
        "INSERT INTO telemetry_events (event_uid, ts, machine, cluster, process, category, "
        "event_name, level, source, attributes) "
        "VALUES (%s, %s, 'm1', %s, %s, %s, %s, %s, %s, %s::jsonb)",
        (
            uuid.uuid4().int % (1 << 62),
            datetime.now(UTC) - timedelta(minutes=minutes_ago),
            cluster_label() if cluster is None else cluster,
            process,
            category,
            event_name,
            level,
            source,
            json.dumps(attributes or {}),
        ),
    )


def _dismiss(
    db: psycopg.Connection, event_name: str, *, level: str = "warning", process: str = ""
) -> int:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO event_dismissals (category, level, event_name, source, process, "
            "dismissed_by) VALUES ('log', %s, %s, 'svc', %s, 0) RETURNING id",
            (level, event_name, process),
        )
        row = cur.fetchone()
    assert row is not None
    return row[0]


def test_classes_are_grouped_with_counts_times_and_most_frequent_first(
    db_conn: psycopg.Connection, client: TestClient
) -> None:
    for minutes_ago in (50, 20, 5):
        _event(db_conn, "disk_pressure", minutes_ago=minutes_ago)
    _event(db_conn, "disk_pressure", process="agent-host")
    _event(db_conn, "turn_failed", level="error", source="runner")
    _event(db_conn, "turn_failed", level="error", source="runner")
    _event(db_conn, "ignored_info", level="info")
    _event(db_conn, "elsewhere", cluster="another-cluster")
    _event(db_conn, "too_old", minutes_ago=60 * 30)
    db_conn.commit()

    body = client.get("/api/stats/alert-classes").json()

    assert body["window_hours"] == 24
    assert body["total_classes"] == 3
    assert body["total_events"] == 6
    rows = body["classes"]
    assert [(r["event_name"], r["process"], r["count"]) for r in rows] == [
        ("disk_pressure", "gateway", 3),
        ("turn_failed", "gateway", 2),
        ("disk_pressure", "agent-host", 1),
    ]
    first = rows[0]
    assert (first["level"], first["source"], first["category"]) == ("warning", "svc", "telemetry")
    assert first["dismissal_id"] is None
    assert datetime.fromisoformat(first["last_seen"]) - datetime.fromisoformat(
        first["first_seen"]
    ) == pytest.approx(timedelta(minutes=45), abs=timedelta(seconds=1))


def test_the_window_selects_which_events_form_classes(
    db_conn: psycopg.Connection, client: TestClient
) -> None:
    _event(db_conn, "recent", minutes_ago=3)
    _event(db_conn, "hours_old", minutes_ago=180)
    db_conn.commit()

    last_hour = client.get("/api/stats/alert-classes", params={"hours": 1}).json()
    last_day = client.get("/api/stats/alert-classes", params={"hours": 24}).json()

    assert [r["event_name"] for r in last_hour["classes"]] == ["recent"]
    assert {r["event_name"] for r in last_day["classes"]} == {"recent", "hours_old"}
    assert client.get("/api/stats/alert-classes", params={"hours": 5}).status_code == 422


def test_dismissed_classes_carry_the_cancelling_dismissal(
    db_conn: psycopg.Connection, client: TestClient
) -> None:
    """A wildcard row cancels every process of its class; an exact row only its own process,
    and the exact row is the one reported when both match. A reopened row cancels nothing."""
    wildcard = _dismiss(db_conn, "wild")
    exact = _dismiss(db_conn, "scoped", process="agent-host")
    _event(db_conn, "wild", process="gateway")
    _event(db_conn, "wild", process="agent-host", category="log")
    _event(db_conn, "scoped", process="agent-host")
    _event(db_conn, "scoped", process="gateway")
    reopened = _dismiss(db_conn, "reopened")
    db_conn.execute("UPDATE event_dismissals SET status = 'reopened' WHERE id = %s", (reopened,))
    _event(db_conn, "reopened")
    db_conn.commit()

    rows = client.get("/api/stats/alert-classes").json()["classes"]

    by_identity = {(r["event_name"], r["process"]): r["dismissal_id"] for r in rows}
    assert by_identity == {
        ("wild", "gateway"): wildcard,
        ("wild", "agent-host"): wildcard,
        ("scoped", "agent-host"): exact,
        ("scoped", "gateway"): None,
        ("reopened", "gateway"): None,
    }


def test_dismissing_through_the_api_moves_the_class_out_of_the_active_count(
    db_conn: psycopg.Connection, client: TestClient
) -> None:
    """The create call the console makes from a class row, then the reopen call from its
    dismissal id, round-trip through both the list and the dashboard count."""
    _event(db_conn, "noisy", process="gateway", category="log", minutes_ago=2)
    _event(db_conn, "noisy", process="gateway", category="log", minutes_ago=1)
    db_conn.commit()
    row = client.get("/api/stats/alert-classes").json()["classes"][0]
    assert client.get("/api/stats/dashboard").json()["alert_classes_active"] == 1

    created = client.post(
        "/api/event-resolutions",
        json={key: row[key] for key in ("category", "level", "event_name", "source", "process")},
    )
    assert created.status_code == 201

    after = client.get("/api/stats/alert-classes").json()["classes"][0]
    assert after["dismissal_id"] == created.json()["id"]
    dashboard = client.get("/api/stats/dashboard").json()
    assert (dashboard["alert_classes_active"], dashboard["alert_classes_dismissed"]) == (0, 1)
    assert dashboard["warnings"] == 2  # the event total does not move; the class does

    reopened = client.post(f"/api/event-resolutions/{after['dismissal_id']}/reopen")
    assert reopened.status_code == 200
    assert client.get("/api/stats/alert-classes").json()["classes"][0]["dismissal_id"] is None
    assert client.get("/api/stats/dashboard").json()["alert_classes_active"] == 1


def test_the_class_list_is_capped_but_reports_its_full_size(
    db_conn: psycopg.Connection, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(alert_classes, "ALERT_CLASSES_LIMIT", 2)
    for name, repeats in (("a", 3), ("b", 2), ("c", 1)):
        for _ in range(repeats):
            _event(db_conn, name)
    db_conn.commit()

    body = client.get("/api/stats/alert-classes").json()

    assert [r["event_name"] for r in body["classes"]] == ["a", "b"]
    assert (body["total_classes"], body["total_events"]) == (3, 6)


def test_samples_are_the_newest_events_of_exactly_one_class(
    db_conn: psycopg.Connection, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(alert_classes, "SAMPLES_LIMIT", 2)
    _event(db_conn, "boom", minutes_ago=30, attributes={"msg": "oldest"})
    _event(db_conn, "boom", minutes_ago=20, attributes={"msg": "middle", "detail": 7})
    _event(db_conn, "boom", minutes_ago=10, attributes={"msg": "newest"})
    _event(db_conn, "boom", process="agent-host", attributes={"msg": "other process"})
    _event(db_conn, "boom", level="error", attributes={"msg": "other level"})
    _event(db_conn, "quiet", attributes={"msg": "other class"})
    db_conn.commit()

    samples = client.get(
        "/api/stats/alert-classes/samples",
        params={"level": "warning", "event_name": "boom", "source": "svc", "process": "gateway"},
    ).json()["samples"]

    assert [s["message"] for s in samples] == ["newest", "middle"]
    assert samples[0]["machine"] == "m1"
    assert samples[1]["attributes"] == {"msg": "middle", "detail": 7}
    assert samples[0]["agent_id"] is None


def test_samples_without_a_message_attribute_have_a_null_message(
    db_conn: psycopg.Connection, client: TestClient
) -> None:
    _event(db_conn, "structured", process="", attributes={"reason": "x"})
    db_conn.commit()

    samples = client.get(
        "/api/stats/alert-classes/samples",
        params={"level": "warning", "event_name": "structured", "source": "svc"},
    ).json()["samples"]

    assert len(samples) == 1
    assert samples[0]["message"] is None
    assert samples[0]["attributes"] == {"reason": "x"}


def test_samples_reject_an_unknown_level(client: TestClient) -> None:
    response = client.get(
        "/api/stats/alert-classes/samples",
        params={"level": "info", "event_name": "x", "source": "svc"},
    )
    assert response.status_code == 422
