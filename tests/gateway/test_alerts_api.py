"""POST /api/alerts + GET /api/alerts + GET /api/alerts/stream + PATCH /api/alerts/read
integration tests (Task #1224 — the Alert system, separate from Notice).

Same posture as the old test_ops_alerts_api.py: real SQL on the session DB.
Locks the contract: Alertmanager-webhook upsert + (fingerprint, starts_at)
dedup, severity label parsing (critical/warning/error), the fingerprint
computation when the payload omits it, the unresolved-first list + counts
(unresolved for the floating bar, unread for the badge), mark-as-read, the
ingest auth split (webhook token / loopback), the SSE publish on every
ingest, and the IM-notify gate (the im_bridge fan-out is mocked — the
endpoint's side effect is that it POSTs to the daemon, which has its own
tests).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from gateway.app import app
from gateway.routers import alerts as alerts_router
from shared.config import settings


def _alert(
    *,
    status: str = "firing",
    alertname: str = "test-rule",
    severity: str | None = "error",
    starts_at: str = "2026-08-04T10:00:00Z",
    ends_at: str = "",
    summary: str = "test summary",
    fingerprint: str = "abc123",
) -> dict[str, Any]:
    labels = {"alertname": alertname, "team": "ava-ops"}
    if severity:
        labels["severity"] = severity
    return {
        "status": status,
        "labels": labels,
        "annotations": {"summary": summary},
        "startsAt": starts_at,
        "endsAt": ends_at,
        "fingerprint": fingerprint,
        "generatorURL": "http://localhost:3002/alerting/xyz/edit",
    }


def _webhook(
    status: str = "firing",
    alerts: list[dict[str, Any]] | None = None,
    source: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": status,
        "alerts": alerts or [_alert(status=status, **extra)],
    }
    if source is not None:
        payload["source"] = source
    return payload


def _ingest(
    client: TestClient,
    payload: dict[str, Any],
    token: str = "test-token",  # noqa: S107 — test token fixture value
) -> Any:
    return client.post(
        "/api/alerts",
        json=payload,
        headers={"X-Alerts-Token": token},
    )


@pytest.fixture
def client() -> Any:
    """TestClient with the gateway app (lifespan: db pool + scheduler)."""

    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _alerts_auth_and_im(monkeypatch: pytest.MonkeyPatch) -> None:
    """Webhook token set (loopback trust off) + IM fan-out mocked."""

    monkeypatch.setattr(settings.alerts, "webhook_token", SecretStr("test-token"))
    monkeypatch.setattr(settings.alerts, "im_notify_enabled", True)

    def _fake_notify(text: str) -> bool:
        return True

    monkeypatch.setattr(alerts_router, "_notify_im", _fake_notify)


# -- ingest ------------------------------------------------------------------


def test_ingest_firing_inserts_row_and_notifies(db_conn: psycopg.Connection) -> None:
    """A firing webhook inserts one row (unresolved), notifies IM once, stamps
    notified_at."""
    with TestClient(app) as client:
        resp = _ingest(client, _webhook())
        assert resp.status_code == 200
        assert resp.json() == {"processed": 1, "inserted": 1, "updated": 0, "notified": 1}

    with db_conn.cursor() as cur:
        cur.execute("SELECT status, severity, alertname, fingerprint, notified_at FROM alerts")
        rows = cur.fetchall()
    assert len(rows) == 1
    status, severity, alertname, fp, notified_at = rows[0]
    assert status == "unresolved"
    assert severity == "error"
    assert alertname == "test-rule"
    assert fp == "abc123"
    assert notified_at is not None


def test_ingest_dedups_same_instance(db_conn: psycopg.Connection) -> None:
    """Re-sends of the same (fingerprint, starts_at) update the row, not
    duplicate it — and a still-firing instance that already notified stays
    silent on IM."""
    with TestClient(app) as client:
        _ingest(client, _webhook())
        resp = _ingest(client, _webhook(summary="updated summary"))
        assert resp.json()["inserted"] == 0
        assert resp.json()["updated"] == 1
        assert resp.json()["notified"] == 0

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*), max(annotations->>'summary') FROM alerts")
        row = cur.fetchone()
        assert row is not None
        n, summary = row
    assert n == 1
    assert summary == "updated summary"


def test_ingest_resolved_flips_row_and_notifies_recovery(db_conn: psycopg.Connection) -> None:
    """A resolved webhook for a notified firing flips status + sets ends_at
    and notifies the recovery line."""
    with TestClient(app) as client:
        _ingest(client, _webhook())
        resp = _ingest(
            client,
            _webhook(status="resolved", ends_at="2026-08-04T11:00:00Z"),
        )
        assert resp.json()["notified"] == 1

    with db_conn.cursor() as cur:
        cur.execute("SELECT status, ends_at FROM alerts")
        row = cur.fetchone()
        assert row is not None
    assert row[0] == "resolved"
    assert row[1] == datetime(2026, 8, 4, 11, 0, tzinfo=UTC)


def test_ingest_resolved_without_prior_notify_is_silent(db_conn: psycopg.Connection) -> None:
    """An instance whose firing IM never landed resolves silently (the user
    never heard the firing; a bare recovery line would be noise)."""
    with TestClient(app) as client:
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO alerts (status, severity, alertname, labels, annotations,"
                " starts_at, fingerprint) VALUES ('unresolved', 'error', 'test-rule',"
                " '{}', '{}', '2026-08-04T10:00:00+00:00', 'abc123')"
            )
        db_conn.commit()
        resp = _ingest(
            client,
            _webhook(status="resolved", ends_at="2026-08-04T11:00:00Z"),
        )
        assert resp.json()["notified"] == 0


def test_ingest_severity_parsing(db_conn: psycopg.Connection) -> None:
    """critical/warning/error parse; the legacy P0-P3 labels map onto the new
    classes (compat shim); an unknown/absent severity normalizes to warning
    (the quietest default)."""
    with TestClient(app) as client:
        _ingest(client, _webhook(alerts=[_alert(severity="critical", fingerprint="f1")]))
        _ingest(client, _webhook(alerts=[_alert(severity="warning", fingerprint="f2")]))
        _ingest(client, _webhook(alerts=[_alert(severity="error", fingerprint="f3")]))
        _ingest(client, _webhook(alerts=[_alert(severity="P0", fingerprint="f4")]))
        _ingest(client, _webhook(alerts=[_alert(severity="P1", fingerprint="f5")]))
        _ingest(client, _webhook(alerts=[_alert(severity="P2", fingerprint="f6")]))
        _ingest(client, _webhook(alerts=[_alert(severity="P3", fingerprint="f7")]))
        _ingest(client, _webhook(alerts=[_alert(severity="BOGUS", fingerprint="f8")]))
        _ingest(client, _webhook(alerts=[_alert(severity=None, fingerprint="f9")]))

    with db_conn.cursor() as cur:
        cur.execute("SELECT fingerprint, severity FROM alerts ORDER BY fingerprint")
        rows = cur.fetchall()
    assert dict(rows) == {
        "f1": "critical",
        "f2": "warning",
        "f3": "error",
        "f4": "critical",
        "f5": "error",
        "f6": "warning",
        "f7": "warning",
        "f8": "warning",
        "f9": "warning",
    }


def test_ingest_every_severity_notifies() -> None:
    """User design 2026-08-12: ALL three severities push to IM — there is no
    severity gate anymore."""
    notified: list[str] = []

    def _capture(text: str) -> bool:
        notified.append(text)
        return True

    with TestClient(app) as client, pytest.MonkeyPatch.context() as mp:
        mp.setattr(alerts_router, "_notify_im", _capture)
        _ingest(client, _webhook(alerts=[_alert(severity="critical", fingerprint="c1")]))
        _ingest(client, _webhook(alerts=[_alert(severity="warning", fingerprint="w1")]))
        _ingest(client, _webhook(alerts=[_alert(severity="error", fingerprint="e1")]))
    assert len(notified) == 3
    # default template language is zh (user ruling 2026-08-13: IM copy follows
    # user_settings display.language, default zh) — the en path is covered by
    # test_ingest_uses_display_language_setting
    assert "⚠️ 告警 [CRITICAL]" in notified[0]  # emoji-ok: asserting the user-designated IM format
    assert "⚠️ 告警 [WARNING]" in notified[1]  # emoji-ok: asserting the user-designated IM format
    assert "⚠️ 告警 [ERROR]" in notified[2]  # emoji-ok: asserting the user-designated IM format


def test_ingest_uses_display_language_setting(db_conn: psycopg.Connection) -> None:
    """IM template language follows user_settings display.language — an "en"
    row selects the English template set through the full ingest path (the
    default zh path is covered by test_ingest_every_severity_notifies)."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO user_settings (key, value) VALUES ('display.language', %s)",
            (Jsonb("en"),),
        )
    db_conn.commit()

    notified: list[str] = []

    def _capture(text: str) -> bool:
        notified.append(text)
        return True

    with TestClient(app) as client, pytest.MonkeyPatch.context() as mp:
        mp.setattr(alerts_router, "_notify_im", _capture)
        _ingest(client, _webhook())
    assert len(notified) == 1
    assert (
        "⚠️ ALERT [ERROR] test-rule"  # emoji-ok: asserting the user-designated IM format
        in notified[0]
    )


def test_ingest_zero_ends_at_stored_null(db_conn: psycopg.Connection) -> None:
    """Alertmanager's zero time endsAt (0001-01-01T00:00:00Z) is NULL."""

    with TestClient(app) as client:
        _ingest(client, _webhook(ends_at="0001-01-01T00:00:00Z"))

    with db_conn.cursor() as cur:
        cur.execute("SELECT ends_at FROM alerts")
        row = cur.fetchone()
        assert row is not None
        assert row[0] is None


def test_ingest_im_failure_does_not_fail_ingest(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """im_bridge down -> the ingest still stores the row (notified_at stays
    NULL) and answers 200."""
    monkeypatch.setattr(alerts_router, "_notify_im", lambda _text: False)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    with TestClient(app) as client:
        resp = _ingest(client, _webhook())
        assert resp.status_code == 200
        assert resp.json()["notified"] == 0

    with db_conn.cursor() as cur:
        cur.execute("SELECT notified_at FROM alerts")
        row = cur.fetchone()
        assert row is not None
        assert row[0] is None


def test_ingest_firing_retries_notify_after_failed_attempt(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """notified_at NULL keeps the firing gate open — the next re-send retries
    the IM."""
    monkeypatch.setattr(alerts_router, "_notify_im", lambda _text: False)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    with TestClient(app) as client:
        _ingest(client, _webhook())
    monkeypatch.setattr(alerts_router, "_notify_im", lambda _text: True)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    with TestClient(app) as client:
        resp = _ingest(client, _webhook())
        assert resp.json()["notified"] == 1

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*), max(notified_at) IS NOT NULL FROM alerts")
        row = cur.fetchone()
        assert row is not None
        n, stamped = row
    assert n == 1 and stamped


def test_ingest_refire_after_resolution_notifies_again(db_conn: psycopg.Connection) -> None:
    """A resolved row that fires again (new starts_at) is a new episode — a
    fresh row + a fresh IM."""
    with TestClient(app) as client:
        _ingest(client, _webhook())
        _ingest(client, _webhook(status="resolved", ends_at="2026-08-04T11:00:00Z"))
        resp = _ingest(
            client,
            _webhook(starts_at="2026-08-04T12:00:00Z", ends_at=""),
        )
        assert resp.json()["inserted"] == 1
        assert resp.json()["notified"] == 1

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM alerts")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 2


def test_ingest_computes_fingerprint_when_absent(db_conn: psycopg.Connection) -> None:
    """A payload without a fingerprint gets the Alertmanager-standard hash of
    its labels — stable across sends (dedup holds), and equal to Grafana's
    hash for the same label set."""
    with TestClient(app) as client:
        _ingest(client, _webhook(alerts=[_alert(fingerprint="")]))
        resp = _ingest(client, _webhook(alerts=[_alert(fingerprint="")]))
        assert resp.json()["inserted"] == 0

    with db_conn.cursor() as cur:
        cur.execute("SELECT fingerprint, count(*) FROM alerts GROUP BY fingerprint")
        row = cur.fetchone()
        assert row is not None
        fp, n = row
    assert n == 1
    from shared.alerts import fingerprint as compute_fp

    assert fp == compute_fp({"alertname": "test-rule", "team": "ava-ops", "severity": "error"})


def test_ingest_alertmanager_v4_envelope_tolerated(db_conn: psycopg.Connection) -> None:
    """The full Alertmanager v4 envelope (version/groupKey/truncatedAlerts/
    receiver/groupLabels/commonLabels/commonAnnotations/externalURL) is
    accepted — only status + alerts[] matter to the store."""
    payload = {
        "version": "4",
        "groupKey": '{}/{}:{{alertname="test-rule"}}',
        "truncatedAlerts": 0,
        "receiver": "ava-alerts-webhook",
        "groupLabels": {"alertname": "test-rule"},
        "commonLabels": {"alertname": "test-rule"},
        "commonAnnotations": {"summary": "test summary"},
        "externalURL": "http://localhost:3002/",
        "status": "firing",
        "alerts": [_alert()],
    }
    with TestClient(app) as client:
        resp = _ingest(client, payload)
        assert resp.status_code == 200
        assert resp.json()["inserted"] == 1


def test_ingest_grafana_flat_payload_tolerated(db_conn: psycopg.Connection) -> None:
    """The slimmer Grafana-managed shape (top-level status only, no per-alert
    status) is still accepted — status falls back from the top level."""
    payload: dict[str, Any] = {
        "status": "firing",
        "alerts": [
            {
                "labels": {"alertname": "r", "severity": "warning"},
                "annotations": {"summary": "s"},
                "startsAt": "2026-08-04T10:00:00Z",
                "fingerprint": "gf1",
            }
        ],
    }
    with TestClient(app) as client:
        resp = _ingest(client, payload)
        assert resp.status_code == 200
        assert resp.json()["inserted"] == 1
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM alerts")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "unresolved"


def test_ingest_publishes_sse_frames(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every ingested row is published to the ava:alerts Redis channel as one
    AlertRow JSON frame (the SSE stream's payload)."""

    published: list[tuple[str, str]] = []

    class _FakeRedis:
        def __enter__(self) -> _FakeRedis:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def publish(self, channel: str, frame: str) -> None:
            published.append((channel, frame))

    monkeypatch.setattr(alerts_router, "sync_redis", _FakeRedis)
    with TestClient(app) as client:
        _ingest(
            client,
            _webhook(alerts=[_alert(fingerprint="p1"), _alert(fingerprint="p2", alertname="r2")]),
        )
        _ingest(client, _webhook())  # duplicate re-send — still publishes (update)

    assert len(published) == 3
    for channel, frame in published:
        assert channel == "ava:alerts"
        parsed = json.loads(frame)
        assert parsed["id"] > 0
        assert parsed["fingerprint"] in ("p1", "p2", "abc123")


# -- auth --------------------------------------------------------------------


def test_ingest_requires_webhook_token(client: TestClient) -> None:
    """No/wrong token -> 401; correct token -> 200; the legacy header name
    still works."""
    payload = _webhook()
    assert client.post("/api/alerts", json=payload).status_code == 401
    assert (
        client.post("/api/alerts", json=payload, headers={"X-Alerts-Token": "wrong"}).status_code
        == 401
    )
    assert (
        client.post(
            "/api/alerts", json=payload, headers={"X-Alerts-Token": "test-token"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/alerts", json=payload, headers={"X-Ops-Alerts-Token": "test-token"}
        ).status_code
        == 200
    )


def test_ingest_bearer_cluster_secret_accepted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cluster-secret Bearer is an alternative webhook credential."""
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "supersecret")
    payload = _webhook()
    resp = client.post(
        "/api/alerts",
        json=payload,
        headers={"Authorization": "Bearer supersecret"},
    )
    assert resp.status_code == 200


def test_ingest_bearer_webhook_token_accepted(client: TestClient) -> None:
    """The webhook token also works as a Bearer credential — Grafana 13
    webhook contact points can only authenticate via the notifier-native
    Authorization fields (custom headers are stored in plaintext by the 13
    provisioning schema), so the gateway accepts the scoped webhook token on
    the Bearer path."""
    payload = _webhook()
    resp = client.post(
        "/api/alerts",
        json=payload,
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status_code == 200


def test_ingest_loopback_trust_when_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no webhook token configured, loopback callers are trusted and
    remote ones rejected."""

    monkeypatch.setattr(settings.alerts, "webhook_token", None)

    class _FakeClient:
        def __init__(self, host: str) -> None:
            self.host = host

    class _FakeRequest:
        def __init__(self, host: str) -> None:
            self.client = _FakeClient(host)
            self.headers: dict[str, str] = {}

    assert alerts_router._ingest_authorized(_FakeRequest("127.0.0.1"))  # type: ignore[arg-type]
    assert alerts_router._ingest_authorized(_FakeRequest("::1"))  # type: ignore[arg-type]
    assert not alerts_router._ingest_authorized(_FakeRequest("10.0.0.5"))  # type: ignore[arg-type]

    # token set -> loopback alone is not enough
    monkeypatch.setattr(settings.alerts, "webhook_token", SecretStr("t"))
    assert not alerts_router._ingest_authorized(_FakeRequest("127.0.0.1"))  # type: ignore[arg-type]


# -- list --------------------------------------------------------------------


def _seed(db: psycopg.Connection, rows: list[dict[str, Any]]) -> None:
    with db.cursor() as cur:
        for r in rows:
            cur.execute(
                "INSERT INTO alerts"
                " (status, severity, alertname, labels, annotations, starts_at, ends_at,"
                "  fingerprint, source, read_at)"
                " VALUES (%(status)s, %(severity)s, %(alertname)s, '{}'::jsonb, '{}'::jsonb,"
                "         %(starts_at)s, %(ends_at)s, %(fingerprint)s, %(source)s, %(read_at)s)",
                {
                    **r,
                    "source": r.get("source", "grafana"),
                    "labels": None,
                    "annotations": None,
                },
            )
    db.commit()


def test_list_unread_first_and_filters(db_conn: psycopg.Connection, client: TestClient) -> None:
    """Default list = unread only, unread_count under the same filters, order
    unread-first then recency; include_read brings read rows back; the
    counts ride meta."""
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
                "read_at": None,
            },
            {
                "status": "resolved",
                "severity": "warning",
                "alertname": "r2",
                "starts_at": now - timedelta(hours=2),
                "ends_at": now - timedelta(hours=1, minutes=50),
                "fingerprint": "f2",
                "read_at": now - timedelta(minutes=30),
            },
            {
                "status": "unresolved",
                "severity": "error",
                "alertname": "r3",
                "starts_at": now - timedelta(minutes=5),
                "ends_at": None,
                "fingerprint": "f3",
                "read_at": None,
            },
        ],
    )

    resp = client.get("/api/alerts")
    assert resp.status_code == 200
    body = resp.json()
    assert [a["fingerprint"] for a in body["alerts"]] == ["f3", "f1"]
    assert body["meta"]["unread_count"] == 2
    assert body["meta"]["unresolved_count"] == 2
    assert body["meta"]["total"] == 2
    assert body["meta"]["include_read"] is False

    resp = client.get("/api/alerts?include_read=true")
    body = resp.json()
    assert [a["fingerprint"] for a in body["alerts"]] == ["f3", "f1", "f2"]
    assert body["meta"]["unread_count"] == 2
    assert body["meta"]["total"] == 3

    resp = client.get("/api/alerts?severity=warning&include_read=true")
    assert [a["fingerprint"] for a in resp.json()["alerts"]] == ["f2"]

    resp = client.get("/api/alerts?status=unresolved")
    assert [a["fingerprint"] for a in resp.json()["alerts"]] == ["f3", "f1"]

    resp = client.get("/api/alerts?status=resolved&include_read=true")
    body = resp.json()
    assert [a["fingerprint"] for a in body["alerts"]] == ["f2"]
    assert body["meta"]["unresolved_count"] == 0  # scoped to resolved -> bar count 0

    resp = client.get("/api/alerts?window=1h")
    assert [a["fingerprint"] for a in resp.json()["alerts"]] == ["f3"]

    resp = client.get("/api/alerts?limit=1")
    assert [a["fingerprint"] for a in resp.json()["alerts"]] == ["f3"]
    assert resp.json()["meta"]["total"] == 2  # limit does not shrink total


def test_list_unresolved_before_resolved_unread(
    db_conn: psycopg.Connection, client: TestClient
) -> None:
    """A resolved-but-unread alert must not bury an unresolved one (2026-08-05
    user ruling): the list is unresolved-first, then unread, then recency."""
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
                "read_at": None,
            },
            {
                "status": "unresolved",
                "severity": "error",
                "alertname": "fire-old",
                "starts_at": now - timedelta(hours=1),
                "ends_at": None,
                "fingerprint": "f2",
                "read_at": None,
            },
        ],
    )

    resp = client.get("/api/alerts")
    assert resp.status_code == 200
    body = resp.json()
    # unresolved (older) sorts above resolved (newer, unread)
    assert [a["fingerprint"] for a in body["alerts"]] == ["f2", "f1"]
    assert body["meta"]["unread_count"] == 2


# -- read --------------------------------------------------------------------


def test_mark_read_ids_and_all(db_conn: psycopg.Connection, client: TestClient) -> None:
    """PATCH /api/alerts/read by ids and by all; idempotent."""
    now = datetime.now(UTC)
    _seed(
        db_conn,
        [
            {
                "status": "unresolved",
                "severity": "error",
                "alertname": "a",
                "starts_at": now - timedelta(minutes=1),
                "ends_at": None,
                "fingerprint": "fa",
                "read_at": None,
            },
            {
                "status": "unresolved",
                "severity": "error",
                "alertname": "b",
                "starts_at": now - timedelta(minutes=2),
                "ends_at": None,
                "fingerprint": "fb",
                "read_at": None,
            },
        ],
    )
    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM alerts WHERE fingerprint = 'fa'")
        row = cur.fetchone()
        assert row is not None
        id_a = row[0]

    resp = client.patch("/api/alerts/read", json={"ids": [id_a]})
    assert resp.json() == {"updated": 1}
    # repeat is a no-op (already read)
    resp = client.patch("/api/alerts/read", json={"ids": [id_a]})
    assert resp.json() == {"updated": 0}

    resp = client.patch("/api/alerts/read", json={"all": True})
    assert resp.json() == {"updated": 1}
    resp = client.patch("/api/alerts/read", json={"all": True})
    assert resp.json() == {"updated": 0}


def test_mark_read_requires_target(client: TestClient) -> None:
    """Neither ids nor all -> 422."""
    assert client.patch("/api/alerts/read", json={}).status_code == 422


# -- sources -----------------------------------------------------------------


def test_ingest_health_probe_source_stored_and_notified(db_conn: psycopg.Connection) -> None:
    """source="health-probe" rides into the row; the firing still notifies."""
    payload = _webhook(source="health-probe", alerts=[_alert(fingerprint="hp1")])
    with TestClient(app) as client:
        resp = _ingest(client, payload)
        assert resp.status_code == 200
        assert resp.json()["notified"] == 1
    with db_conn.cursor() as cur:
        cur.execute("SELECT source FROM alerts")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "health-probe"


def test_ingest_conflict_preserves_original_source(db_conn: psycopg.Connection) -> None:
    """A re-send under a different source does not rewrite provenance."""
    with TestClient(app) as client:
        _ingest(client, _webhook(source="machine-probe", alerts=[_alert(fingerprint="s1")]))
        _ingest(client, _webhook(alerts=[_alert(fingerprint="s1")]))
    with db_conn.cursor() as cur:
        cur.execute("SELECT source FROM alerts")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "machine-probe"


def test_ingest_resolved_health_probe_notifies_recovery(db_conn: psycopg.Connection) -> None:
    """A health-probe firing + resolved pair lands as one row + two IMs."""
    with TestClient(app) as client:
        r1 = _ingest(client, _webhook(source="health-probe", alerts=[_alert(fingerprint="hp2")]))
        r2 = _ingest(
            client,
            _webhook(
                source="health-probe",
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
                "read_at": None,
                "source": "machine-probe",
            }
        ],
    )
    body = client.get("/api/alerts").json()
    assert body["alerts"][0]["source"] == "machine-probe"


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
