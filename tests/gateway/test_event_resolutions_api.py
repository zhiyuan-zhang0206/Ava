"""Authenticated class-level event-resolution API tests (task #1468)."""

from __future__ import annotations

from typing import Any, cast

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.routers import event_resolutions
from shared.config import settings


@pytest.fixture
def client(db_conn: psycopg.Connection) -> Any:
    with db_conn.cursor() as cur:
        cur.execute("TRUNCATE event_dismissals RESTART IDENTITY")
    db_conn.commit()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def emitted(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, object]]]:
    out: list[tuple[str, str, dict[str, object]]] = []

    def emit(category: str, event_name: str, **kwargs: object) -> None:
        out.append((category, event_name, cast(dict[str, object], kwargs["attributes"])))

    monkeypatch.setattr(event_resolutions.telemetry, "emit", emit)
    return out


def _body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "category": "telemetry",
        "level": "warning",
        "event_name": "test_warning",
        "source": "events-maintenance",
        "note": "investigated by ops",
    }
    body.update(overrides)
    return body


def test_create_emits_marker_and_duplicate_conflicts(
    client: TestClient, emitted: list[tuple[str, str, dict[str, object]]]
) -> None:
    created = client.post("/api/event-resolutions", json=_body())

    assert created.status_code == 201
    row = created.json()
    assert row["status"] == "dismissed"
    assert row["dismissed_by"] == 0
    assert emitted == [
        (
            "telemetry",
            "warning_resolved",
            {
                "category": "telemetry",
                "level": "warning",
                "event_name": "test_warning",
                "source": "events-maintenance",
                "agent_id": None,
                "dismissed_by": 0,
                "note": "investigated by ops",
            },
        )
    ]

    duplicate = client.post("/api/event-resolutions", json=_body())
    assert duplicate.status_code == 409
    assert emitted[0][1] == "warning_resolved"  # no duplicate transition marker
    assert len(emitted) == 1


def test_list_filter_and_manual_reopen_emit_marker(
    client: TestClient, emitted: list[tuple[str, str, dict[str, object]]]
) -> None:
    first = client.post("/api/event-resolutions", json=_body()).json()
    second = client.post(
        "/api/event-resolutions",
        json=_body(level="critical", event_name="test_critical"),
    ).json()

    active = client.get("/api/event-resolutions", params={"status": "dismissed"})
    assert active.status_code == 200
    assert {row["id"] for row in active.json()["resolutions"]} == {first["id"], second["id"]}

    reopened = client.post(f"/api/event-resolutions/{first['id']}/reopen")
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "reopened"
    assert emitted[-1] == (
        "telemetry",
        "warning_reopened",
        {
            "category": "telemetry",
            "level": "warning",
            "event_name": "test_warning",
            "source": "events-maintenance",
            "agent_id": None,
            "dismissed_by": 0,
            "note": "investigated by ops",
            "reopened_by": "user/operator",
            "triggered_by_count": None,
        },
    )
    history = client.get("/api/event-resolutions", params={"status": "reopened"})
    assert [row["id"] for row in history.json()["resolutions"]] == [first["id"]]


def test_agent_specific_dismissal_is_rejected(client: TestClient) -> None:
    response = client.post("/api/event-resolutions", json=_body(agent_id=1818))

    assert response.status_code == 422
    assert "agent_id-specific" in response.text


def test_gateway_auth_protects_resolution_routes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.gateway, "auth_middleware_enabled", True)
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "resolution-test-secret")

    response = client.post("/api/event-resolutions", json=_body())

    assert response.status_code == 401
