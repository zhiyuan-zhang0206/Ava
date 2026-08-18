"""Aggregate metrics report over events (category=telemetry/log) for the settings Metrics tab.

`/api/metrics` mirrors `scripts/metrics.py`: both run the same `shared.metrics`
aggregates over the Loki windowed fetch, so the CLI digest and the API never
drift. The fetch is Loki-side aggregated (`shared.metrics_aggregate.fetch_aggregate`
over `gateway.loki_events`; task #1197 A3) — the SQL path it replaces
materialized 430K+ rows/day into gateway memory (+47MB RSS per call, finding
F-s1-4) and the metric units reduce that stream to a few hundred aggregate
rows anyway. `/api/metrics/agents` is the per-agent breakdown of the same
fetch (one headline-counter row per agent). Both are window-selected +
manual-refresh, so no caching — each call re-aggregates from the append-only
event stream.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request

from gateway import loki_events
from gateway.schemas import AgentMetricsItem, AgentMetricsReport, MetricsMeta, MetricsReport
from shared.metrics_aggregate import (
    agent_rollups_from_aggregate,
    build_report_from_aggregate,
    fetch_aggregate,
)

router = APIRouter()


@router.get("/api/metrics")
def get_metrics(
    days: Annotated[int, Query(ge=1, le=30)] = 1,
    agent: Annotated[int | None, Query()] = None,
    since_compact: Annotated[bool, Query()] = False,  # noqa: FBT002 — FastAPI query param
) -> MetricsReport:
    """Aggregate report over the last `days` of events (all agents, or a
    single one via `agent`). `days` is capped at 30 to bound the scan.
    `since_compact=true` additionally narrows each agent's events to those at
    or after its latest compact halt (echoed in `meta.since_compact`).
    `meta.total_events` counts every telemetry/log event in the window —
    including service-level rows (agent_id NULL) from every process, a scope
    widened by the W9 events-table switch (it was agent-kernel lines only
    before); audit events are excluded."""
    agg = fetch_aggregate(days, agent, since_compact=since_compact, loki=loki_events)
    _, data = build_report_from_aggregate(agg, days, agent, since_compact=since_compact)
    return MetricsReport(**data)


@router.get("/api/metrics/agents")
def get_metrics_agents(
    request: Request,
    days: Annotated[int, Query(ge=1, le=30)] = 1,
    since_compact: Annotated[bool, Query()] = False,  # noqa: FBT002 — FastAPI query param
) -> AgentMetricsReport:
    """Per-agent breakdown of the last `days` of events — one
    headline-counter row per agent (cost / tokens / cache hit / turn + exec
    outcomes), sorted by cost descending. `since_compact=true` narrows each
    agent's events to those at or after its latest compact halt. Service-level
    events (no agent_id) count toward `meta.total_events` but produce no row —
    and the count covers every telemetry/log event in the window (all
    processes), the W9-widened scope documented on `get_metrics`."""
    agg = fetch_aggregate(days, None, since_compact=since_compact, loki=loki_events)
    rollups = agent_rollups_from_aggregate(agg)
    labels: dict[int, str | None] = {}
    if rollups:
        with request.app.state.db_pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, label FROM agents WHERE id = ANY(%s)", (list(rollups),))
            labels = dict(cur.fetchall())
    items = [
        # events.agent_id is a FK onto agents, so labels[aid] always exists.
        AgentMetricsItem(agent_id=aid, label=labels[aid], **rollup)
        for aid, rollup in rollups.items()
    ]
    items.sort(key=lambda item: (-item.cost_usd, item.agent_id))
    meta = MetricsMeta(
        window_days=days,
        agent_filter=None,
        generated_at=datetime.now(UTC).isoformat(),
        total_events=agg.total_events,
        distinct_agents=len(rollups),
        since_compact=since_compact,
    )
    return AgentMetricsReport(meta=meta, agents=items)
