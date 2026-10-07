"""Alerts — the system→human alert store + UI API (Task #1224, user design 2026-08-12).

Alert is fully separate from Notice: own table, own UI section, own IM
channel — nothing here touches agent_notices.

- ``POST /api/alerts`` — the alert webhook. Grafana's embedded Alertmanager
  contact point delivers the Alertmanager standard webhook payload here — the
  one way into the store. Each alert instance is stored in ``alerts``
  (deduped by fingerprint x starts_at), published on the ``ava:alerts`` Redis
  channel for the SSE stream, and fanned out to the user's connected IM
  channels via the local im_bridge daemon — every severity pushes
  (critical/warning/error).
- ``GET /api/alerts`` — unresolved-first history list + unresolved count for
  the top-bar badge.
- ``GET /api/alerts/stream`` — SSE tail of every ingest (reuses the
  agent_events SSE machinery; broadcast mode, no agent filter).

Auth split by consumer:
- ``POST /api/alerts`` — the Grafana webhook. It bypasses the session/bearer
  middleware (Grafana does not hold the cluster secret) and authenticates
  itself: ``X-Alerts-Token`` matching
  the configured webhook token (constant-time), or the webhook token as a
  ``Bearer`` credential (Grafana 13 webhook contact points only support the
  notifier-native Authorization fields — custom headers are stored in
  plaintext), else the cluster-secret Bearer, else — when no webhook token is
  configured — loopback trust (Grafana is co-located with the gateway on the
  single-box posture). A remote caller without the token is always rejected.
- ``GET`` / ``/stream`` — the UI and the SDK: normal
  session/Bearer auth via the app middleware, untouched here.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from psycopg.rows import dict_row
from pydantic import TypeAdapter

from base.config import settings
from base.db.transaction import write_transaction
from base.telemetry.alerts import (
    display_language,
    upsert_alert,
)
from base.telemetry.alerts.native import native_sent_count, notify_alert_group
from base.telemetry.alerts.shadow import AlertShadowBatch
from gateway.alerts.publish import ALERTS_CHANNEL, publish_alert_rows
from gateway.alerts.schemas import (
    AlertIngestResult,
    AlertRow,
    AlertSeverity,
    AlertsListMeta,
    AlertsListResponse,
    AlertStatus,
    AlertWebhookPayload,
)
from gateway.auth.webhook import authenticate_webhook
from gateway.events.sse import event_stream

router = APIRouter()
_log = logging.getLogger(__name__)

_WINDOWS = {
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
}

# Each SSE frame is one AlertRow JSON — validate what we forward so a bad
# publish degrades to a dropped frame, never a crashed stream.
_alert_frame_validator = TypeAdapter(AlertRow)


# -- ingest auth -------------------------------------------------------------


def _ingest_authorized(request: Request) -> bool:
    """Webhook-token header, else cluster-secret Bearer, else loopback trust.

    ``X-Alerts-Token`` carries the webhook token; the Grafana contact point
    sends it as a notifier-native Bearer instead (see ``gateway.auth.webhook``).
    Loopback trust only applies when no webhook token is configured — the
    single-box default, where Grafana (127.0.0.1:3003) is the only caller and
    the gateway binds everything anyway. With a token set, loopback is not
    enough: the token is the contract.
    """

    return authenticate_webhook(request, provider="alerts").authorized


# -- ingest ------------------------------------------------------------------


@router.post("/api/alerts")
def ingest_alerts(body: AlertWebhookPayload, request: Request) -> AlertIngestResult:
    """Alertmanager webhook endpoint — store + SSE publish + IM notify.

    Body is the standard Alertmanager webhook payload (``status`` +
    ``alerts[]``). One row per (fingerprint, starts_at); re-sends while
    firing update the row, resolution flips status + sets ends_at. Every
    firing transition notifies IM (all severities) and every ingested row
    is published to the SSE stream.
    """

    if not _ingest_authorized(request):
        raise HTTPException(status_code=401, detail="unauthorized webhook caller")

    inserted = updated = notified = 0
    rows: list[dict[str, Any]] = []
    with write_transaction(request.app.state.db_pool) as conn:
        lang = display_language(conn)
        alerts = body.flattened()
        shadow = AlertShadowBatch(conn, alerts, lang, native=settings.alerts.im_notify_enabled)
        for alert in shadow.items:
            instance_key, previous = shadow.observe(alert)
            if instance_key is None:
                continue
            _key, did_insert, should_notify, row = upsert_alert(
                conn, alert, source=body.source, instance_key=instance_key
            )
            if not row:
                continue
            if did_insert:
                inserted += 1
            else:
                updated += 1
            rows.append(row)
            shadow.record(alert, row, previous, should_notify=should_notify)
        shadow.freeze()
        conn.commit()

    publish_alert_rows(request.app.state.bus, rows)

    for group_id in sorted(shadow.native_ids):
        notify_alert_group(group_id)
    with request.app.state.db_pool.connection() as conn:
        notified = native_sent_count(conn, shadow.native_ids)

    return AlertIngestResult(
        processed=len(body.alerts), inserted=inserted, updated=updated, notified=notified
    )


# -- SSE stream ---------------------------------------------------------------


@router.get("/api/alerts/stream")
async def get_alerts_stream(request: Request) -> StreamingResponse:
    """SSE endpoint — subscribe to the ``ava:alerts`` Redis channel, forward.

    Client ``EventSource`` receives ``data: {json}\n\n`` frames, one
    ``AlertRow`` JSON per frame, on every ingest. Same machinery as the
    agent-events stream (heartbeat frames, error frames, reconnection-safe);
    this is the live tail — the initial fetch (GET /api/alerts) covers rows
    ingested before the subscription opened.
    """

    return StreamingResponse(
        event_stream(
            request.app.state.bus,
            0,
            request,
            channel=ALERTS_CHANNEL,
            broadcast=True,
            validator=_alert_frame_validator,
        ),
        media_type="text/event-stream",
        headers={
            # Reverse proxies like nginx / cloudflare buffer text responses
            # by default — these two headers tell them to pass bytes through.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# -- list ---------------------------------------------------------------------


@router.get("/api/alerts")
def list_alerts(
    request: Request,
    window: str = Query(default="24h", pattern="^(1h|6h|24h|7d)$"),
    status: AlertStatus | None = None,
    severity: AlertSeverity | None = None,
    # `limit`'s range stays a protective constant (import-time Query bound;
    # task #3696 exception inventory); the default *window* is
    # display.alerts_default_limit.
    limit: int | None = Query(default=None, ge=1, le=500),
) -> AlertsListResponse:
    """Unresolved-first alert history for the alert section.

    Unresolved instances float above resolved ones (2026-08-05 user ruling);
    within a status class, newest starts come first.
    ``meta.unresolved_count`` backs the top-bar badge and ``meta.total`` is
    the full match count.
    """

    effective_limit = limit if limit is not None else settings.display.alerts_default_limit
    since = datetime.now(UTC) - _WINDOWS[window]
    params: list[Any] = [since]
    where = ["starts_at > %s"]
    if status is not None:
        where.append("status = %s")
        params.append(status)
    if severity is not None:
        where.append("severity = %s")
        params.append(severity)
    where_sql = " AND ".join(where)

    # meta counts (one connection, two cheap queries):
    # - unresolved: same window/severity scope, always unresolved. Trivially
    #   0 when the caller already scopes to resolved.
    # - total: rows matching the filters before the limit.
    with request.app.state.db_pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        if status == "resolved":
            unresolved_count = 0
        else:
            base = [w for w in where if w != "status = %s"]
            base.append("status = 'unresolved'")
            base_params: list[Any] = [since]
            if severity is not None:
                base_params.append(severity)
            cur.execute(
                f"SELECT count(*) AS n FROM alerts WHERE {' AND '.join(base)}",  # noqa: S608 — fixed fragments
                base_params,
            )
            unresolved_count = cur.fetchone()["n"]
        cur.execute(f"SELECT count(*) AS n FROM alerts WHERE {where_sql}", params)  # noqa: S608 — fixed fragments
        total = cur.fetchone()["n"]

        select_sql = (
            "SELECT id, status, severity, alertname, labels, annotations, starts_at, ends_at,"  # noqa: S608 — where_sql built from fixed fragments only
            "       fingerprint, generator_url, source, notified_at, created_at, updated_at"
            f"  FROM alerts WHERE {where_sql}"
            " ORDER BY (status = 'unresolved') DESC, starts_at DESC LIMIT %s"
        )
        cur.execute(select_sql, (*params, effective_limit))
        rows = [AlertRow(**r) for r in cur.fetchall()]

    return AlertsListResponse(
        alerts=rows,
        meta=AlertsListMeta(
            window=window,
            total=total,
            unresolved_count=unresolved_count,
        ),
    )
