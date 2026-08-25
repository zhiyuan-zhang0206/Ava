"""Repair Grafana alert rows whose resolution webhook was lost.

Grafana's notification policy repeats an unchanged firing only every four
hours, so webhook silence is not current state. This service reads the embedded
Alertmanager's active instances at gateway startup and every five minutes,
then resolves stored Grafana-owned instances absent from that truth set.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from shared.alerts import AlertKey, parse_ts
from shared.config import settings

_log = logging.getLogger(__name__)

_GRAFANA_ALERTS_PATH = "/api/alertmanager/grafana/api/v2/alerts"
_RECONCILE_INTERVAL_S = 300.0
_RECONCILIATION_NOTE = "reconciled: no longer firing"
_GRAFANA_API_TIMEOUT = httpx.Timeout(10.0)

PublishRows = Callable[[list[dict[str, Any]]], None]


@dataclass(frozen=True)
class GrafanaAlertReconciler:
    """Owned background task plus the event that drains it before shutdown."""

    task: asyncio.Task[None]
    stop: asyncio.Event


def grafana_reconciliation_configured() -> bool:
    """Whether this gateway has the credential needed to read Grafana truth."""

    password = settings.alerts.grafana_admin_password
    return password is not None and bool(password.get_secret_value())


def _grafana_active_alert_keys(payload: object) -> set[AlertKey]:
    """Validate Grafana's Alertmanager response into instance identities.

    Reject the whole snapshot if any entry lacks its stable identity. Silently
    dropping a malformed entry would make a currently firing alert look absent
    and resolve it, so partial truth is not usable truth here.
    """

    if not isinstance(payload, list):
        raise TypeError("Grafana active-alert response must be a list")
    keys: set[AlertKey] = set()
    for raw_object in cast("list[object]", payload):
        if not isinstance(raw_object, dict):
            raise TypeError("Grafana active-alert entry must be an object")
        raw = cast("dict[str, object]", raw_object)
        fingerprint = raw.get("fingerprint")
        starts_at_raw = raw.get("startsAt")
        if not isinstance(fingerprint, str) or not fingerprint:
            raise ValueError("Grafana active-alert entry has no fingerprint")
        if not isinstance(starts_at_raw, str):
            raise TypeError("Grafana active-alert entry has no startsAt")
        starts_at = parse_ts(starts_at_raw)
        if starts_at is None:
            raise ValueError("Grafana active-alert entry has an invalid startsAt")
        keys.add((fingerprint, starts_at))
    return keys


def reconcile_open_grafana_alerts(
    conn: psycopg.Connection,
    active_keys: set[AlertKey],
    *,
    snapshot_started_at: datetime,
    resolved_at: datetime,
) -> list[dict[str, Any]]:
    """Resolve stored Grafana instances absent from one complete snapshot.

    Only Grafana webhook rows participate: health/machine probes are
    edge-triggered direct writers and do not appear in Grafana. Rows touched
    after the snapshot began are excluded, closing the race where a new firing
    webhook lands after Grafana produced the response but before this UPDATE.
    """

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, fingerprint, starts_at FROM alerts"
            " WHERE source = 'grafana' AND status = 'unresolved' AND updated_at <= %s",
            (snapshot_started_at,),
        )
        stale_ids = [
            row["id"]
            for row in cur.fetchall()
            if (row["fingerprint"], row["starts_at"]) not in active_keys
        ]
        if not stale_ids:
            return []
        cur.execute(
            "UPDATE alerts SET"
            " status = 'resolved', ends_at = %s,"
            " annotations = annotations || %s, updated_at = %s"
            " WHERE id = ANY(%s) AND status = 'unresolved' AND updated_at <= %s"
            " RETURNING id, status, severity, alertname, labels, annotations, starts_at,"
            "           ends_at, fingerprint, generator_url, source, notified_at,"
            "           created_at, updated_at",
            (
                resolved_at,
                Jsonb({"reconciliation": _RECONCILIATION_NOTE}),
                resolved_at,
                stale_ids,
                snapshot_started_at,
            ),
        )
        return list(cur.fetchall())


def _reconcile_from_pool(
    db_pool: ConnectionPool,
    active_keys: set[AlertKey],
    snapshot_started_at: datetime,
    resolved_at: datetime,
) -> list[dict[str, Any]]:
    """Borrow + commit wrapper kept off the event loop by the caller."""

    with db_pool.connection() as conn:
        rows = reconcile_open_grafana_alerts(
            conn,
            active_keys,
            snapshot_started_at=snapshot_started_at,
            resolved_at=resolved_at,
        )
        conn.commit()
    return rows


async def _reconcile_once(
    db_pool: ConnectionPool,
    grafana_client: httpx.AsyncClient,
    publish_rows: PublishRows,
) -> int:
    """Fetch one complete Grafana snapshot, resolve omissions, publish rows."""

    password = settings.alerts.grafana_admin_password
    if password is None or not password.get_secret_value():
        return 0
    # This timestamp precedes the upstream read: any webhook racing with or
    # following the snapshot has a newer updated_at and is ineligible.
    snapshot_started_at = datetime.now(UTC)
    host = settings.gateway.grafana_host
    port = settings.gateway.grafana_port
    response = await grafana_client.get(
        f"http://{host}:{port}{_GRAFANA_ALERTS_PATH}",
        auth=httpx.BasicAuth("admin", password.get_secret_value()),
        timeout=_GRAFANA_API_TIMEOUT,
    )
    response.raise_for_status()
    payload: object = response.json()
    active_keys = _grafana_active_alert_keys(payload)
    resolved_at = datetime.now(UTC)
    rows = await asyncio.to_thread(
        _reconcile_from_pool,
        db_pool,
        active_keys,
        snapshot_started_at,
        resolved_at,
    )
    if rows:
        await asyncio.to_thread(publish_rows, rows)
    return len(rows)


async def reconciliation_loop(
    db_pool: ConnectionPool,
    grafana_client: httpx.AsyncClient,
    publish_rows: PublishRows,
    stop: asyncio.Event,
) -> None:
    """Reconcile immediately at startup, then every five minutes.

    Every upstream/validation/DB failure is fail-closed: log it, leave all
    rows untouched, and retry on the next interval.
    """

    while not stop.is_set():
        try:
            resolved = await _reconcile_once(db_pool, grafana_client, publish_rows)
            if resolved:
                _log.info("alerts: reconciled %d alert(s) against Grafana", resolved)
        except Exception:
            _log.warning("alerts: Grafana reconciliation failed", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=_RECONCILE_INTERVAL_S)
        except TimeoutError:
            continue


def start_grafana_alert_reconciler(
    db_pool: ConnectionPool,
    grafana_client: httpx.AsyncClient,
    publish_rows: PublishRows,
) -> GrafanaAlertReconciler | None:
    """Start the configured reconciler, or return None when Grafana auth is absent."""

    if not grafana_reconciliation_configured():
        return None
    stop = asyncio.Event()
    task = asyncio.create_task(reconciliation_loop(db_pool, grafana_client, publish_rows, stop))
    return GrafanaAlertReconciler(task=task, stop=stop)


async def stop_grafana_alert_reconciler(reconciler: GrafanaAlertReconciler | None) -> None:
    """Drain a bounded in-flight pass before its shared client/pool are closed."""

    if reconciler is None:
        return
    # Cancelling during asyncio.to_thread would leave its DB worker alive while
    # the gateway closes the pool. The event wakes sleeps and lets one pass drain.
    reconciler.stop.set()
    with suppress(asyncio.CancelledError):
        await reconciler.task
