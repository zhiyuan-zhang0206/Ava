"""Grafana-truth reconciliation for alert webhooks whose RESOLVE was lost."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import psycopg
import pytest
from psycopg.types.json import Jsonb
from pydantic import SecretStr

import shared.db
from gateway.alert_reconciliation import (
    _grafana_active_alert_keys,
    _reconcile_once,
    reconcile_open_grafana_alerts,
)
from shared.config import settings


def _insert_alert(
    conn: psycopg.Connection,
    *,
    fingerprint: str,
    starts_at: datetime,
    updated_at: datetime,
    source: str = "grafana",
    status: str = "unresolved",
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO alerts"
            " (status, severity, alertname, labels, annotations, starts_at, ends_at,"
            "  fingerprint, source, updated_at)"
            " VALUES (%s, 'warning', 'test-alert', '{}', %s, %s, %s, %s, %s, %s)"
            " RETURNING id",
            (
                status,
                Jsonb({"summary": fingerprint}),
                starts_at,
                starts_at + timedelta(minutes=10) if status == "resolved" else None,
                fingerprint,
                source,
                updated_at,
            ),
        )
        row = cur.fetchone()
    assert row is not None
    return row[0]


def test_grafana_active_alert_keys_uses_instance_identity() -> None:
    """The same fingerprint can have several episodes; startsAt is part of truth."""

    payload: list[dict[str, Any]] = [
        {
            "fingerprint": "same-fingerprint",
            "startsAt": "2026-08-23T10:00:00Z",
            "status": {"state": "active"},
        },
        {
            "fingerprint": "same-fingerprint",
            "startsAt": "2026-08-23T19:00:00+08:00",
            "status": {"state": "active"},
        },
    ]

    assert _grafana_active_alert_keys(payload) == {
        ("same-fingerprint", datetime(2026, 8, 23, 10, tzinfo=UTC)),
        ("same-fingerprint", datetime(2026, 8, 23, 11, tzinfo=UTC)),
    }


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [{}],
        [{"fingerprint": "fp", "startsAt": "not-a-time"}],
    ],
)
def test_grafana_active_alert_keys_rejects_incomplete_snapshots(payload: object) -> None:
    """Malformed upstream truth must abort the sweep, never resolve by omission."""

    with pytest.raises((TypeError, ValueError)):
        _grafana_active_alert_keys(payload)


def test_reconcile_open_grafana_alerts_resolves_only_snapshot_misses(
    db_conn: psycopg.Connection,
) -> None:
    snapshot_started_at = datetime(2026, 8, 23, 12, tzinfo=UTC)
    resolved_at = snapshot_started_at + timedelta(seconds=5)
    old = snapshot_started_at - timedelta(hours=1)
    current_start = snapshot_started_at - timedelta(minutes=30)

    stale_episode_id = _insert_alert(
        db_conn,
        fingerprint="same-fingerprint",
        starts_at=old,
        updated_at=old,
    )
    active_episode_id = _insert_alert(
        db_conn,
        fingerprint="same-fingerprint",
        starts_at=current_start,
        updated_at=old,
    )
    missing_id = _insert_alert(
        db_conn,
        fingerprint="missing",
        starts_at=old,
        updated_at=old,
    )
    direct_probe_id = _insert_alert(
        db_conn,
        fingerprint="direct-probe",
        starts_at=old,
        updated_at=old,
        source="health-probe",
    )
    concurrent_ingest_id = _insert_alert(
        db_conn,
        fingerprint="arrived-after-snapshot",
        starts_at=current_start,
        updated_at=snapshot_started_at + timedelta(seconds=1),
    )
    already_resolved_id = _insert_alert(
        db_conn,
        fingerprint="already-resolved",
        starts_at=old,
        updated_at=old,
        status="resolved",
    )
    db_conn.commit()

    rows = reconcile_open_grafana_alerts(
        db_conn,
        {("same-fingerprint", current_start)},
        snapshot_started_at=snapshot_started_at,
        resolved_at=resolved_at,
    )
    db_conn.commit()

    assert {row["id"] for row in rows} == {stale_episode_id, missing_id}
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT id, status, ends_at, annotations->>'reconciliation', updated_at"
            " FROM alerts ORDER BY id"
        )
        by_id = {row[0]: row[1:] for row in cur.fetchall()}

    assert by_id[stale_episode_id] == (
        "resolved",
        resolved_at,
        "reconciled: no longer firing",
        resolved_at,
    )
    assert by_id[missing_id] == (
        "resolved",
        resolved_at,
        "reconciled: no longer firing",
        resolved_at,
    )
    assert by_id[active_episode_id][0] == "unresolved"
    assert by_id[direct_probe_id][0] == "unresolved"
    assert by_id[concurrent_ingest_id][0] == "unresolved"
    assert by_id[already_resolved_id][0] == "resolved"


@pytest.mark.asyncio
async def test_reconcile_once_fetches_truth_and_publishes_resolved_rows(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = datetime.now(UTC) - timedelta(hours=1)
    missing_id = _insert_alert(
        db_conn,
        fingerprint="missing-from-grafana",
        starts_at=old,
        updated_at=old,
    )
    db_conn.commit()
    monkeypatch.setattr(
        settings.alerts,
        "grafana_admin_password",
        SecretStr("grafana-test-password"),
    )

    def _grafana(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/alertmanager/grafana/api/v2/alerts"
        assert request.headers["Authorization"].startswith("Basic ")
        return httpx.Response(200, json=[])

    published: list[dict[str, Any]] = []
    db_pool = shared.db.pool()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_grafana)) as client:
            resolved = await _reconcile_once(db_pool, client, published.extend)
    finally:
        db_pool.close()

    assert resolved == 1
    assert [row["id"] for row in published] == [missing_id]
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM alerts WHERE id = %s", (missing_id,))
        assert cur.fetchone() == ("resolved",)
