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

import httpx
from fastapi import APIRouter, Query, Request

from gateway import loki_query_budget, prom_metrics
from gateway.ops_series_lgtm import fetch_ops_series
from gateway.routers._backend_failure import raise_backend_unavailable
from gateway.schemas.ops import OpsMonitorReport

router = APIRouter()


def _lookup_agent_labels(request: Request, agent_ids: list[int]) -> dict[int, str | None]:
    """Read the small agents-label projection after the LGTM fan-out completes."""
    with request.app.state.db_pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, label FROM agents WHERE id = ANY(%s)", (agent_ids,))
        return dict(cur.fetchall())


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
        data = fetch_ops_series(
            window,
            label_lookup=lambda agent_ids: _lookup_agent_labels(request, agent_ids),
        )
    except (loki_query_budget.LokiQueryBudgetError, prom_metrics.PromQueryBudgetError):
        raise
    except httpx.HTTPError as exc:
        raise_backend_unavailable(exc, backend="observability")
    return OpsMonitorReport(**data)
