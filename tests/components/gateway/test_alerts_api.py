"""POST /api/alerts + GET /api/alerts + GET /api/alerts/stream integration tests
(Task #1224 — the Alert system, separate from Notice).

Same posture as the old test_ops_alerts_api.py: real SQL on the session DB.
Locks the contract: Alertmanager-webhook upsert + (fingerprint, starts_at)
dedup, severity label parsing (critical/warning/error), the fingerprint
computation when the payload omits it, the unresolved-first list + counts
(including the badge's unresolved count), the ingest auth split (webhook
token / loopback), the SSE publish on every
ingest, and the IM-notify gate (the im_bridge fan-out is mocked — the
endpoint's side effect is that it POSTs to the daemon, which has its own
tests).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from base.config import settings
from base.events.live.tests.fakes import record_publishes
from base.telemetry.alerts.native import load_native_group, stamp_native_sent
from gateway.alerts import router as alerts_router
from gateway.tests.agents.gateway_auth import without_credentials
from tests.fixtures.gateway_config import gateway_test_app, gateway_test_client


def native_sender(callback: Callable[[str], bool]) -> Callable[[int], bool]:
    """The policy fixture reports real-send success; acceptance-only tests live with Outbox."""

    def send(group_id: int) -> bool:
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
            source = load_native_group(conn, group_id)
            succeeded = callback(source["text"])
            if succeeded:
                stamp_native_sent(conn, group_id)
            return succeeded

    return send


def alert(
    *,
    status: str = "firing",
    alertname: str = "test-rule",
    severity: str | None = "error",
    starts_at: str = "2026-08-04T10:00:00Z",
    ends_at: str = "",
    summary: str = "test summary",
    fingerprint: str = "abc123",
    notify_im: str | None = None,
) -> dict[str, Any]:
    labels = {"alertname": alertname, "team": "meiva-ops"}
    if severity:
        labels["severity"] = severity
    if notify_im is not None:
        labels["notify_im"] = notify_im
    return {
        "status": status,
        "labels": labels,
        "annotations": {"summary": summary},
        "startsAt": starts_at,
        "endsAt": ends_at,
        "fingerprint": fingerprint,
        "generatorURL": "http://localhost:3002/alerting/xyz/edit",
    }


def webhook(
    status: str = "firing",
    alerts: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": status,
        "alerts": alerts or [alert(status=status, **extra)],
    }
    return payload


def ingest(
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

    with gateway_test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def alerts_auth_and_im(monkeypatch: pytest.MonkeyPatch) -> None:
    """Webhook token set (loopback trust off) + IM fan-out mocked."""

    monkeypatch.setattr(settings.alerts, "webhook_token", SecretStr("test-token"))
    monkeypatch.setattr(settings.alerts, "im_notify_enabled", True)

    def _fake_notify(text: str) -> bool:
        return True

    monkeypatch.setattr(alerts_router, "notify_alert_group", native_sender(_fake_notify))


# -- ingest ------------------------------------------------------------------


def test_ingest_firing_inserts_row_and_notifies(db_conn: psycopg.Connection) -> None:
    """A firing webhook inserts one row (unresolved), notifies IM once, stamps
    notified_at."""
    with gateway_test_client() as client:
        resp = ingest(client, webhook())
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
    with gateway_test_client() as client:
        ingest(client, webhook())
        resp = ingest(client, webhook(summary="updated summary"))
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


def test_ingest_severity_escalation_updates_instance_and_renotifies(
    db_conn: psycopg.Connection,
) -> None:
    """Escalation is a new firing transition on the existing instance."""
    with gateway_test_client() as client:
        warning = ingest(client, webhook(severity="warning"))
        error = ingest(client, webhook(severity="error"))

    assert warning.json()["notified"] == 1
    assert error.json()["notified"] == 1
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*), max(severity) FROM alerts")
        row = cur.fetchone()
    assert row == (1, "error")


def test_ingest_severity_downgrade_does_not_renotify(
    db_conn: psycopg.Connection,
) -> None:
    """A lower class carries no new urgent information for the owner."""
    with gateway_test_client() as client:
        error = ingest(client, webhook(severity="error"))
        warning = ingest(client, webhook(severity="warning"))

    assert error.json()["notified"] == 1
    assert warning.json()["notified"] == 0
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*), max(severity) FROM alerts")
        row = cur.fetchone()
    assert row == (1, "warning")


def test_ingest_resolved_flips_row_and_notifies_recovery(db_conn: psycopg.Connection) -> None:
    """A resolved webhook for a notified firing flips status + sets ends_at
    and notifies the recovery line."""
    with gateway_test_client() as client:
        ingest(client, webhook())
        resp = ingest(
            client,
            webhook(status="resolved", ends_at="2026-08-04T11:00:00Z"),
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
    with gateway_test_client() as client:
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO alerts (status, severity, alertname, labels, annotations,"
                " starts_at, fingerprint) VALUES ('unresolved', 'error', 'test-rule',"
                " '{}', '{}', '2026-08-04T10:00:00+00:00', 'abc123')"
            )
        db_conn.commit()
        resp = ingest(
            client,
            webhook(status="resolved", ends_at="2026-08-04T11:00:00Z"),
        )
        assert resp.json()["notified"] == 0


def test_ingest_severity_parsing(db_conn: psycopg.Connection) -> None:
    """critical/warning/error parse; an unknown/absent severity normalizes to
    warning (the quietest default)."""
    with gateway_test_client() as client:
        ingest(client, webhook(alerts=[alert(severity="critical", fingerprint="f1")]))
        ingest(client, webhook(alerts=[alert(severity="warning", fingerprint="f2")]))
        ingest(client, webhook(alerts=[alert(severity="error", fingerprint="f3")]))
        ingest(client, webhook(alerts=[alert(severity="BOGUS", fingerprint="f8")]))
        ingest(client, webhook(alerts=[alert(severity=None, fingerprint="f9")]))

    with db_conn.cursor() as cur:
        cur.execute("SELECT fingerprint, severity FROM alerts ORDER BY fingerprint")
        rows = cur.fetchall()
    assert dict(rows) == {
        "f1": "critical",
        "f2": "warning",
        "f3": "error",
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

    with gateway_test_client() as client, pytest.MonkeyPatch.context() as mp:
        mp.setattr(alerts_router, "notify_alert_group", native_sender(_capture))
        ingest(client, webhook(alerts=[alert(severity="critical", fingerprint="c1")]))
        ingest(client, webhook(alerts=[alert(severity="warning", fingerprint="w1")]))
        ingest(client, webhook(alerts=[alert(severity="error", fingerprint="e1")]))
    assert len(notified) == 3
    # default template language is zh (user ruling 2026-08-13: IM copy follows
    # user_settings display.language, default zh) — the en path is covered by
    # test_ingest_uses_display_language_setting
    assert (
        "⚠️ \u544a\u8b66 [CRITICAL]" in notified[0]  # emoji-ok: IM alert head format
    )
    assert (
        "⚠️ \u544a\u8b66 [WARNING]" in notified[1]  # emoji-ok: IM alert head format
    )
    assert (
        "⚠️ \u544a\u8b66 [ERROR]" in notified[2]  # emoji-ok: IM alert head format
    )


def test_ingest_notify_im_false_stores_and_publishes_without_im(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """notify_im="false" keeps the alert in the store and SSE stream while
    suppressing only its IM fan-out."""
    notified: list[str] = []
    published: list[tuple[str, str]] = []

    def _capture(text: str) -> bool:
        notified.append(text)
        return True

    monkeypatch.setattr(alerts_router, "notify_alert_group", native_sender(_capture))
    record_publishes(monkeypatch, published)

    with gateway_test_client() as client:
        resp = ingest(client, webhook(notify_im="false"))
    assert resp.json() == {"processed": 1, "inserted": 1, "updated": 0, "notified": 0}
    assert notified == []

    with db_conn.cursor() as cur:
        cur.execute("SELECT status, notified_at FROM alerts")
        row = cur.fetchone()
        assert row is not None
    assert row == ("unresolved", None)

    assert len(published) == 1
    channel, frame = published[0]
    assert channel == "meiva:alerts"
    assert json.loads(frame)["status"] == "unresolved"


def test_ingest_notify_im_false_firing_and_resolution_stay_silent(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gated firing and its resolution both update the row without IM."""
    notified: list[str] = []

    def _capture(text: str) -> bool:
        notified.append(text)
        return True

    monkeypatch.setattr(alerts_router, "notify_alert_group", native_sender(_capture))

    with gateway_test_client() as client:
        firing = ingest(client, webhook(notify_im="false"))
        resolved = ingest(
            client,
            webhook(
                status="resolved",
                ends_at="2026-08-04T11:00:00Z",
                notify_im="false",
            ),
        )
    assert firing.json()["notified"] == 0
    assert resolved.json()["notified"] == 0
    assert notified == []

    with db_conn.cursor() as cur:
        cur.execute("SELECT status, ends_at, notified_at FROM alerts")
        row = cur.fetchone()
        assert row is not None
    assert row == ("resolved", datetime(2026, 8, 4, 11, 0, tzinfo=UTC), None)


def test_ingest_notify_im_non_gating_value_still_notifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the exact string "false" gates IM; "true" keeps the default."""
    notified: list[str] = []

    def _capture(text: str) -> bool:
        notified.append(text)
        return True

    monkeypatch.setattr(alerts_router, "notify_alert_group", native_sender(_capture))

    with gateway_test_client() as client:
        resp = ingest(client, webhook(notify_im="true"))
    assert resp.json()["notified"] == 1
    assert len(notified) == 1


def test_ingest_notify_im_false_resends_and_refire_stay_silent(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gated still-firing re-sends and a new firing episode never retry IM."""
    notified: list[str] = []

    def _capture(text: str) -> bool:
        notified.append(text)
        return True

    monkeypatch.setattr(alerts_router, "notify_alert_group", native_sender(_capture))

    with gateway_test_client() as client:
        firing = ingest(client, webhook(notify_im="false"))
        resend = ingest(client, webhook(notify_im="false"))
        resolved = ingest(
            client,
            webhook(
                status="resolved",
                ends_at="2026-08-04T11:00:00Z",
                notify_im="false",
            ),
        )
        refire = ingest(
            client,
            webhook(
                starts_at="2026-08-04T12:00:00Z",
                notify_im="false",
            ),
        )

    assert [response.json()["notified"] for response in (firing, resend, resolved, refire)] == [
        0,
        0,
        0,
        0,
    ]
    assert resend.json()["updated"] == 1
    assert refire.json()["inserted"] == 1
    assert notified == []

    with db_conn.cursor() as cur:
        cur.execute("SELECT status, notified_at FROM alerts ORDER BY starts_at")
        rows = cur.fetchall()
    assert rows == [("resolved", None), ("unresolved", None)]


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

    with gateway_test_client() as client, pytest.MonkeyPatch.context() as mp:
        mp.setattr(alerts_router, "notify_alert_group", native_sender(_capture))
        ingest(client, webhook())
    assert len(notified) == 1
    assert (
        "⚠️ ALERT [ERROR] test-rule"  # emoji-ok: asserting the user-designated IM format
        in notified[0]
    )


def test_ingest_zero_ends_at_stored_null(db_conn: psycopg.Connection) -> None:
    """Alertmanager's zero time endsAt (0001-01-01T00:00:00Z) is NULL."""

    with gateway_test_client() as client:
        ingest(client, webhook(ends_at="0001-01-01T00:00:00Z"))

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
    monkeypatch.setattr(alerts_router, "notify_alert_group", native_sender(lambda _text: False))  # pyright: ignore[reportUnknownArgumentType]
    with gateway_test_client() as client:
        resp = ingest(client, webhook())
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
    monkeypatch.setattr(alerts_router, "notify_alert_group", native_sender(lambda _text: False))  # pyright: ignore[reportUnknownArgumentType]
    with gateway_test_client() as client:
        ingest(client, webhook())
    monkeypatch.setattr(alerts_router, "notify_alert_group", native_sender(lambda _text: True))  # pyright: ignore[reportUnknownArgumentType]
    with gateway_test_client() as client:
        resp = ingest(client, webhook())
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
    with gateway_test_client() as client:
        ingest(client, webhook())
        ingest(client, webhook(status="resolved", ends_at="2026-08-04T11:00:00Z"))
        resp = ingest(
            client,
            webhook(starts_at="2026-08-04T12:00:00Z", ends_at=""),
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
    with gateway_test_client() as client:
        ingest(client, webhook(alerts=[alert(fingerprint="")]))
        resp = ingest(client, webhook(alerts=[alert(fingerprint="")]))
        assert resp.json()["inserted"] == 0

    with db_conn.cursor() as cur:
        cur.execute("SELECT fingerprint, count(*) FROM alerts GROUP BY fingerprint")
        row = cur.fetchone()
        assert row is not None
        fp, n = row
    assert n == 1
    from base.telemetry.alerts import fingerprint as compute_fp

    assert fp == compute_fp({"alertname": "test-rule", "team": "meiva-ops", "severity": "error"})


def test_ingest_alertmanager_v4_envelope_tolerated(db_conn: psycopg.Connection) -> None:
    """The full Alertmanager v4 envelope (version/groupKey/truncatedAlerts/
    receiver/groupLabels/commonLabels/commonAnnotations/externalURL) is
    accepted — only status + alerts[] matter to the store."""
    payload = {
        "version": "4",
        "groupKey": '{}/{}:{{alertname="test-rule"}}',
        "truncatedAlerts": 0,
        "receiver": "meiva-alerts-webhook",
        "groupLabels": {"alertname": "test-rule"},
        "commonLabels": {"alertname": "test-rule"},
        "commonAnnotations": {"summary": "test summary"},
        "externalURL": "http://localhost:3002/",
        "status": "firing",
        "alerts": [alert()],
    }
    with gateway_test_client() as client:
        resp = ingest(client, payload)
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
    with gateway_test_client() as client:
        resp = ingest(client, payload)
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
    """Every ingested row is published to the meiva:alerts Redis channel as one
    AlertRow JSON frame (the SSE stream's payload)."""

    published: list[tuple[str, str]] = []

    record_publishes(monkeypatch, published)
    with gateway_test_client() as client:
        ingest(
            client,
            webhook(alerts=[alert(fingerprint="p1"), alert(fingerprint="p2", alertname="r2")]),
        )
        ingest(client, webhook())  # duplicate re-send — still publishes (update)

    assert len(published) == 3
    for channel, frame in published:
        assert channel == "meiva:alerts"
        parsed = json.loads(frame)
        assert parsed["id"] > 0
        assert parsed["fingerprint"] in ("p1", "p2", "abc123")


# -- auth --------------------------------------------------------------------


def test_ingest_requires_webhook_token(client: TestClient) -> None:
    """No/wrong token -> 401; correct token -> 200; the legacy header name
    no longer authenticates (removed with task #1173)."""
    payload = webhook()
    without_credentials(client)  # the cluster secret is itself a valid webhook credential
    assert client.post("/api/alerts", json=payload).status_code == 401
    assert (
        client.post(
            "/api/alerts", json=payload, headers={"X-Ops-Alerts-Token": "test-token"}
        ).status_code
        == 401
    )
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


def test_ingest_bearer_cluster_secret_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cluster-secret Bearer is an alternative webhook credential."""
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "supersecret")
    payload = webhook()
    with gateway_test_client() as client:
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
    payload = webhook()
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

    def request(host: str) -> Request:
        return Request(
            {"type": "http", "app": gateway_test_app(), "headers": [], "client": (host, 5000)}
        )

    assert alerts_router.ingest_authorized(request("127.0.0.1"))
    assert alerts_router.ingest_authorized(request("::1"))
    assert not alerts_router.ingest_authorized(request("10.0.0.5"))

    # token set -> loopback alone is not enough
    monkeypatch.setattr(settings.alerts, "webhook_token", SecretStr("t"))
    assert not alerts_router.ingest_authorized(request("127.0.0.1"))


# -- list --------------------------------------------------------------------


def seed_alert_rows(db: psycopg.Connection, rows: list[dict[str, Any]]) -> None:
    with db.cursor() as cur:
        for r in rows:
            cur.execute(
                "INSERT INTO alerts"
                " (status, severity, alertname, labels, annotations, starts_at, ends_at,"
                "  fingerprint, source)"
                " VALUES (%(status)s, %(severity)s, %(alertname)s, '{}'::jsonb, '{}'::jsonb,"
                "         %(starts_at)s, %(ends_at)s, %(fingerprint)s, %(source)s)",
                {
                    **r,
                    "source": r.get("source", "grafana"),
                    "labels": None,
                    "annotations": None,
                },
            )
    db.commit()


# -- sources -----------------------------------------------------------------


# -- one IM per notification group --------------------------------------------


def capture_im(monkeypatch: pytest.MonkeyPatch, *, ok: bool = True) -> list[str]:
    sent: list[str] = []

    def _capture(text: str) -> bool:
        sent.append(text)
        return ok

    monkeypatch.setattr(alerts_router, "notify_alert_group", native_sender(_capture))
    return sent


def test_native_groups_recover_original_group_and_new_members_without_sent_fact(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native retries recover their original group rather than re-rendering expanded POSTs."""
    sent: list[str] = []

    def unavailable(text: str) -> bool:
        sent.append(text)
        return False

    monkeypatch.setattr(alerts_router, "notify_alert_group", native_sender(unavailable))
    a, b, c = [alert(fingerprint=fp, summary=fp) for fp in ("a", "b", "c")]
    with gateway_test_client() as client:
        first = ingest(client, webhook(alerts=[a, b]))
        second = ingest(client, webhook(alerts=[a, b, c]))
        assert first.json() == {"processed": 2, "inserted": 2, "updated": 0, "notified": 0}
        assert second.json() == {"processed": 3, "inserted": 1, "updated": 2, "notified": 0}
    assert len(sent) == 3
    assert sent[0] == sent[1]
    assert sent[2] != sent[0]  # C is a distinct source operation.
    rows = db_conn.execute(
        "SELECT text,origin FROM alert_notification_groups ORDER BY id"
    ).fetchall()
    assert rows[0] == (sent[0], "native-v1")
    assert rows[1] == (sent[2], "native-v1")
    assert db_conn.execute("SELECT count(*) FROM alert_notification_members").fetchone() == (3,)
    assert db_conn.execute(
        "SELECT count(*) FROM alerts WHERE notified_at IS NOT NULL"
    ).fetchone() == (0,)


def test_shadow_keeps_input_order_missing_start_resolution(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later entry still finds the same instance inserted earlier in this POST."""
    sent: list[str] = []

    def unavailable(text: str) -> bool:
        sent.append(text)
        return False

    monkeypatch.setattr(alerts_router, "notify_alert_group", native_sender(unavailable))
    first = alert(fingerprint="same")
    later = alert(fingerprint="same", starts_at="", summary="later")
    with gateway_test_client() as client:
        response = ingest(client, webhook(alerts=[first, later]))
    assert response.json() == {"processed": 2, "inserted": 1, "updated": 1, "notified": 0}
    assert len(sent) == 1
    assert "later" not in sent[0]  # The original source body is immutable.
    assert db_conn.execute("SELECT count(*) FROM alert_notification_members").fetchone() == (1,)
    assert db_conn.execute("SELECT annotations->>'summary' FROM alerts").fetchone() == ("later",)
