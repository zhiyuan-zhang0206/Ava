"""Ops monitor panel — `GET /api/ops/monitor`.

One round trip backs the whole Insights Ops section: time-bucketed series
for the three MVP metric groups (SSE/event-log backlog, LLM latency + TPS,
process restart counts) plus whole-window totals and breakdowns. Series are
computed from `telemetry_events` (`gateway/cluster/ops_series`): counts and sums
are exact, LLM p50/p95/max latency are exact over the rows of each bucket.
Window is capped at 7d.

Adding a metric: new event emissions at the collection point + a read-out in
`gateway/cluster/ops_series` + one schema here + one frontend panel.
"""

from __future__ import annotations

from typing import Annotated, Literal

import psycopg
from fastapi import APIRouter, HTTPException, Query, Request

from gateway.cluster.ops_series import fetch_ops_series
from gateway.cluster.schemas import OpsMonitorReport

router = APIRouter()


def _lookup_agent_labels(conn: psycopg.Connection, agent_ids: list[int]) -> dict[int, str | None]:
    """Read the small agents-label projection."""
    rows = conn.execute("SELECT id, label FROM agents WHERE id = ANY(%s)", (agent_ids,)).fetchall()
    return dict(rows)


@router.get("/api/ops/monitor")
def get_ops_monitor(
    request: Request,
    window: Annotated[Literal["1h", "6h", "24h", "7d"], Query()] = "24h",
) -> OpsMonitorReport:
    """Time-bucketed ops series over `window` (default 24h). Bucket width is
    derived from the window (1h→60s, 6h→300s, 24h→1800s, 7d→3600s); every
    bucket in the window is present (zero-filled when empty), aligned to
    `meta.bucket_starts`."""
    try:
        with request.app.state.db_pool.connection() as conn:
            conn.execute("SET LOCAL statement_timeout = '8s'")
            data = fetch_ops_series(
                conn, window, label_lookup=lambda agent_ids: _lookup_agent_labels(conn, agent_ids)
            )
    except psycopg.errors.QueryCanceled as exc:
        raise HTTPException(status_code=503, detail="ops monitor read timed out; retry") from exc
    return OpsMonitorReport(**data)
