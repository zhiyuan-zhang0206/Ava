"""Alerts api cases: list unresolved first and filters."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.config import settings
from gateway.alerts import router as alerts_router
from gateway.app import app
from tests.components.gateway.test_alerts_api import (
    _alert,
    _capture_im,
    _ingest,
    _seed,
    _webhook,
    native_sender,
)
from tests.components.gateway.test_alerts_api import (
    _alerts_auth_and_im as _alerts_auth_and_im,
)
from tests.components.gateway.test_alerts_api import (
    client as client,
)


def test_list_unresolved_first_and_filters(db_conn: psycopg.Connection, client: TestClient) -> None:
    """List unresolved rows first, then recency; filters and counts ride meta."""
    now = datetime.now(UTC)
    _seed(
        db_conn,
        [
            {
                "status": "unresolved",
                "severity": "error",
                "alertname": "r1",
                "starts_at": now - timedelta(hours=1),
                "ends_at": None,
                "fingerprint": "f1",
            },
            {
                "status": "resolved",
                "severity": "warning",
                "alertname": "r2",
                "starts_at": now - timedelta(hours=2),
                "ends_at": now - timedelta(hours=1, minutes=50),
                "fingerprint": "f2",
            },
            {
                "status": "unresolved",
                "severity": "error",
                "alertname": "r3",
                "starts_at": now - timedelta(minutes=5),
                "ends_at": None,
                "fingerprint": "f3",
            },
        ],
    )

    resp = client.get("/api/alerts")
    assert resp.status_code == 200
    body = resp.json()
    assert [a["fingerprint"] for a in body["alerts"]] == ["f3", "f1", "f2"]
    assert set(body["meta"]) == {"window", "total", "unresolved_count"}
    assert body["meta"]["unresolved_count"] == 2
    assert body["meta"]["total"] == 3

    resp = client.get("/api/alerts?severity=warning")
    assert [a["fingerprint"] for a in resp.json()["alerts"]] == ["f2"]

    resp = client.get("/api/alerts?status=unresolved")
    assert [a["fingerprint"] for a in resp.json()["alerts"]] == ["f3", "f1"]

    resp = client.get("/api/alerts?status=resolved")
    body = resp.json()
    assert [a["fingerprint"] for a in body["alerts"]] == ["f2"]
    assert body["meta"]["unresolved_count"] == 0  # scoped to resolved -> badge count 0

    resp = client.get("/api/alerts?window=1h")
    assert [a["fingerprint"] for a in resp.json()["alerts"]] == ["f3"]

    resp = client.get("/api/alerts?limit=1")
    assert [a["fingerprint"] for a in resp.json()["alerts"]] == ["f3"]
    assert resp.json()["meta"]["total"] == 3  # limit does not shrink total


def test_list_default_limit_comes_from_display_config(
    db_conn: psycopg.Connection, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The implicit window is ``settings.display.alerts_default_limit``
    (``AVA_ALERTS_DEFAULT_LIMIT``); the literal 100 is only that field's
    default, not a hard-coded page size."""
    now = datetime.now(UTC)
    _seed(
        db_conn,
        [
            {
                "status": "unresolved",
                "severity": "error",
                "alertname": f"r{i}",
                "starts_at": now - timedelta(minutes=i),
                "ends_at": None,
                "fingerprint": f"f{i}",
            }
            for i in range(3)
        ],
    )
    monkeypatch.setattr(settings.display, "alerts_default_limit", 2)
    resp = client.get("/api/alerts")
    assert resp.status_code == 200
    assert len(resp.json()["alerts"]) == 2


def test_list_unresolved_before_resolved(db_conn: psycopg.Connection, client: TestClient) -> None:
    """A resolved alert must not bury an unresolved one (2026-08-05 ruling)."""
    now = datetime.now(UTC)
    _seed(
        db_conn,
        [
            {
                "status": "resolved",
                "severity": "error",
                "alertname": "res-new",
                "starts_at": now - timedelta(minutes=5),
                "ends_at": now,
                "fingerprint": "f1",
            },
            {
                "status": "unresolved",
                "severity": "error",
                "alertname": "fire-old",
                "starts_at": now - timedelta(hours=1),
                "ends_at": None,
                "fingerprint": "f2",
            },
        ],
    )

    resp = client.get("/api/alerts")
    assert resp.status_code == 200
    body = resp.json()
    # unresolved (older) sorts above resolved (newer)
    assert [a["fingerprint"] for a in body["alerts"]] == ["f2", "f1"]


def test_ingest_rows_are_grafana_sourced(db_conn: psycopg.Connection) -> None:
    """The webhook is the only writer: its rows carry source="grafana", and a re-send keeps it."""
    with TestClient(app) as client:
        _ingest(client, _webhook(alerts=[_alert(fingerprint="s1")]))
        _ingest(client, _webhook(alerts=[_alert(fingerprint="s1")]))
    with db_conn.cursor() as cur:
        cur.execute("SELECT source FROM alerts")
        assert cur.fetchall() == [("grafana",)]


def test_ingest_resolved_notifies_recovery(db_conn: psycopg.Connection) -> None:
    """A firing + resolved pair lands as one row + two IMs."""
    with TestClient(app) as client:
        r1 = _ingest(client, _webhook(alerts=[_alert(fingerprint="hp2")]))
        r2 = _ingest(
            client,
            _webhook(
                status="resolved",
                alerts=[
                    _alert(status="resolved", fingerprint="hp2", ends_at="2026-08-04T11:00:00Z")
                ],
            ),
        )
        assert r1.json()["notified"] == 1
        assert r2.json()["notified"] == 1
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM alerts")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 1


def test_list_returns_source(db_conn: psycopg.Connection, client: TestClient) -> None:
    """The list exposes source (provenance) per row."""
    now = datetime.now(UTC)
    _seed(
        db_conn,
        [
            {
                "status": "unresolved",
                "severity": "error",
                "alertname": "m",
                "starts_at": now - timedelta(minutes=1),
                "ends_at": None,
                "fingerprint": "fm",
                "source": "grafana",
            }
        ],
    )
    body = client.get("/api/alerts").json()
    assert body["alerts"][0]["source"] == "grafana"


def test_stream_endpoint_is_sse(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/alerts/stream answers text/event-stream with the SSE headers
    and subscribes the ava:alerts channel in broadcast mode."""

    seen: dict[str, Any] = {}

    async def fake_stream(*args: object, **kwargs: object) -> AsyncIterator[bytes]:
        seen["args"] = args
        seen["kwargs"] = kwargs
        yield b": stream open\n\n"

    monkeypatch.setattr(alerts_router, "event_stream", fake_stream)

    with TestClient(app) as client, client.stream("GET", "/api/alerts/stream") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["cache-control"] == "no-cache"
        assert resp.headers["x-accel-buffering"] == "no"
    kwargs = seen["kwargs"]
    assert kwargs["channel"] == "ava:alerts"
    assert kwargs["broadcast"] is True


def test_a_group_of_instances_is_one_im_and_one_row_each(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Grafana posts the instances of one rule together: the store keeps a row per instance,
    the user hears one message with the count."""
    sent = _capture_im(monkeypatch)
    alerts = [
        _alert(alertname="machine offline", fingerprint=f"g{n}", summary=f"machine m{n} down")
        for n in range(4)
    ]
    with TestClient(app) as client:
        resp = _ingest(client, _webhook(alerts=alerts))
    assert resp.json() == {"processed": 4, "inserted": 4, "updated": 0, "notified": 4}
    assert len(sent) == 1
    assert "×4" in sent[0] and "machine m0 down" in sent[0] and "machine m2 down" in sent[0]
    assert "machine m3 down" not in sent[0]
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*), count(notified_at) FROM alerts")
        assert cur.fetchone() == (4, 4)


def test_instances_of_different_rules_in_one_post_are_separate_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _capture_im(monkeypatch)
    alerts = [
        _alert(alertname="rule-a", fingerprint="a1"),
        _alert(alertname="rule-a", fingerprint="a2"),
        _alert(alertname="rule-b", fingerprint="b1"),
    ]
    with TestClient(app) as client:
        resp = _ingest(client, _webhook(alerts=alerts))
    assert resp.json()["notified"] == 3
    assert len(sent) == 2
    assert sum("×2" in text for text in sent) == 1


def test_a_failed_group_send_leaves_every_instance_unnotified_for_the_next_resend(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _capture_im(monkeypatch, ok=False)
    alerts = [_alert(fingerprint=f"r{n}") for n in range(2)]
    with TestClient(app) as client:
        assert _ingest(client, _webhook(alerts=alerts)).json()["notified"] == 0
        sent.clear()

        def _ok(text: str) -> bool:
            sent.append(text)
            return True

        monkeypatch.setattr(alerts_router, "notify_alert_group", native_sender(_ok))
        assert _ingest(client, _webhook(alerts=alerts)).json()["notified"] == 2
    assert len(sent) == 1


def test_a_resolved_group_is_one_recovery_message(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _capture_im(monkeypatch)
    firing = [_alert(fingerprint=f"x{n}") for n in range(3)]
    resolved = [
        _alert(status="resolved", fingerprint=f"x{n}", ends_at="2026-08-04T11:00:00Z")
        for n in range(3)
    ]
    with TestClient(app) as client:
        _ingest(client, _webhook(alerts=firing))
        sent.clear()
        assert _ingest(client, _webhook(status="resolved", alerts=resolved)).json()["notified"] == 3
    assert len(sent) == 1 and "×3" in sent[0]
