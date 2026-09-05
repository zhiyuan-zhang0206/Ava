"""Alerts — the system→human alert store + UI API (Task #1224, user design 2026-08-12).

Alert is fully separate from Notice: own table, own UI section, own IM
channel — nothing here touches agent_notices.

- ``POST /api/alerts`` — the alert webhook. Grafana's embedded Alertmanager
  contact point delivers the Alertmanager standard webhook payload here; the
  cluster health probe posts its edge alerts through the same endpoint with
  ``source="health-probe"``. Each alert instance is stored in ``alerts``
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
  itself: ``X-Alerts-Token`` (or the legacy ``X-Ops-Alerts-Token``) matching
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

from gateway.routers._webhook_auth import authenticate_webhook
from gateway.schemas.alerts import (
    AlertIngestResult,
    AlertRow,
    AlertSeverity,
    AlertsListMeta,
    AlertsListResponse,
    AlertStatus,
    AlertWebhookPayload,
)
from gateway.sse import event_stream
from shared.alerts import (
    AlertKey,
    display_language,
    notify_im,
    notify_text,
    stamp_notified,
    upsert_alert,
)
from shared.config import settings
from shared.db_transaction import write_transaction
from shared.redis_client import sync_redis

router = APIRouter()
_log = logging.getLogger(__name__)

# The Redis pub/sub channel every ingest publishes to and the SSE stream
# subscribes to.
ALERTS_CHANNEL = "ava:alerts"

_WINDOWS = {
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
}

# Underscored aliases of the shared core — the ingest endpoint resolves them
# at call time, so tests can monkeypatch the router's notify/upsert internals.
_upsert_alert = upsert_alert
_notify_im = notify_im
_notify_text = notify_text
_stamp_notified = stamp_notified

# Each SSE frame is one AlertRow JSON — validate what we forward so a bad
# publish degrades to a dropped frame, never a crashed stream.
_alert_frame_validator = TypeAdapter(AlertRow)


# -- ingest auth -------------------------------------------------------------


def _ingest_authorized(request: Request) -> bool:
    """Webhook-token header, else cluster-secret Bearer, else loopback trust.

    Both the new ``X-Alerts-Token`` and the legacy ``X-Ops-Alerts-Token``
    header are accepted — the Grafana host's launchd env still carries the
    old header name in its contact point until the provisioning PR lands.
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
    pending: list[tuple[AlertKey, str]] = []  # (key, text)

    with write_transaction(request.app.state.db_pool) as conn:
        lang = display_language(conn)
        for alert in body.flattened():
            key, did_insert, should_notify, row = _upsert_alert(conn, alert, source=body.source)
            if not row:
                continue
            if did_insert:
                inserted += 1
            else:
                updated += 1
            rows.append(row)
            if should_notify:
                pending.append((key, _notify_text(alert, lang)))
        conn.commit()

    publish_alert_rows(rows)

    if pending:
        with write_transaction(request.app.state.db_pool) as conn:
            for key, text in pending:
                if _notify_im(text):
                    notified += 1
                    _stamp_notified(conn, [key])
            conn.commit()

    return AlertIngestResult(
        processed=len(body.alerts), inserted=inserted, updated=updated, notified=notified
    )


def publish_alert_rows(rows: list[dict[str, Any]]) -> None:
    """Publish each upserted row to the SSE channel (best-effort).

    A Redis outage must not fail the ingest — the SSE stream is a live tail
    and the UI's initial fetch carries the same rows."""

    if not rows:
        return
    try:
        with sync_redis() as client:
            for row in rows:
                frame = AlertRow(**row).model_dump_json()
                client.publish(ALERTS_CHANNEL, frame)  # pyright: ignore[reportUnknownMemberType] — redis-py from_url kwargs typed Unknown (same pattern as shared/redis_client.py)
    except Exception:
        _log.warning("alerts: SSE publish failed (Redis unreachable?)", exc_info=True)


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
            settings.data_plane.redis_url,
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
    limit: int = Query(default=100, ge=1, le=500),
) -> AlertsListResponse:
    """Unresolved-first alert history for the alert section.

    Unresolved instances float above resolved ones (2026-08-05 user ruling);
    within a status class, newest starts come first.
    ``meta.unresolved_count`` backs the top-bar badge and ``meta.total`` is
    the full match count.
    """

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
        cur.execute(select_sql, (*params, limit))
        rows = [AlertRow(**r) for r in cur.fetchall()]

    return AlertsListResponse(
        alerts=rows,
        meta=AlertsListMeta(
            window=window,
            total=total,
            unresolved_count=unresolved_count,
        ),
    )
