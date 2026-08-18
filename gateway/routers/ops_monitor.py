"""Ops monitor panel — `GET /api/ops/monitor`.

One round trip backs the whole Insights Ops section: time-bucketed series
for the three MVP metric groups (SSE/event-log backlog, LLM latency + TPS,
process restart counts) plus whole-window totals and breakdowns. Series come
from the LGTM stack (Loki + Prometheus — the ops-monitor migration, task
#1197): the old pre-aggregated `ops_metrics` PG table was retired with it.
Each call fans out a handful of small range/instant queries in parallel
(`gateway/ops_series_lgtm`); LLM p50/p95 are `histogram_quantile`
approximations over the latency histogram, max latency is exact (Loki
`unwrap`), counts are Prometheus `increase` extrapolations rounded to int.
Window is capped at 7d.

Adding a metric: new event emissions at the collection point + a read-out in
`gateway/ops_series_lgtm` + one schema here + one frontend panel.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request

from gateway.ops_series_lgtm import fetch_ops_series
from gateway.schemas.ops import OpsMonitorReport

router = APIRouter()


@router.get("/api/ops/monitor")
def get_ops_monitor(
    request: Request,
    window: Annotated[Literal["1h", "6h", "24h", "7d"], Query()] = "24h",
) -> OpsMonitorReport:
    """Time-bucketed ops series over `window` (default 24h). Bucket width is
    derived from the window (1h→60s, 6h→300s, 24h→1800s, 7d→3600s); every
    bucket in the window is present (zero-filled when empty), aligned to
    `meta.bucket_starts`."""
    with request.app.state.db_pool.connection() as conn, conn.cursor() as cur:
        data = fetch_ops_series(cur, window)
    return OpsMonitorReport(**data)
