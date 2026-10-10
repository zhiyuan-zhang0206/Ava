"""Aggregate metrics report over events (category=telemetry/log) for the settings Metrics tab.

`/api/metrics` mirrors `scripts/metrics.py`: both run the same `base.telemetry.metrics`
aggregates over the `telemetry_events` table, so the CLI digest and the API never drift. The
fetch reduces the window in SQL (`base.telemetry.metrics.aggregate.fetch_aggregate`; a few
statements in one connection, nothing materialized per row). `/api/metrics/agents` is the
per-agent breakdown of the same window (one headline-counter row per agent). Both are
window-selected + manual-refresh, so no caching — each call re-aggregates from the append-only
table, under a statement timeout (503 on timeout).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import psycopg
from fastapi import APIRouter, HTTPException, Query, Request

from base.config import settings
from base.telemetry.metrics.aggregate import (
    build_report_from_aggregate,
    fetch_agent_rollups,
    fetch_aggregate,
)
from gateway.events.schemas import AgentMetricsItem, AgentMetricsReport, MetricsMeta, MetricsReport

router = APIRouter()

_READ_TIMEOUT = "SET LOCAL statement_timeout = '20s'"


@router.get("/api/metrics")
def get_metrics(
    request: Request,
    # `days`'s range stays a protective constant (import-time Query bound;
    # task #3696 exception inventory); the default *window* is
    # display.metrics_default_window_days.
    days: Annotated[int | None, Query(ge=1, le=30)] = None,
    agent: Annotated[int | None, Query()] = None,
    since_compact: Annotated[bool, Query()] = False,  # noqa: FBT002 — FastAPI query param
) -> MetricsReport:
    """Aggregate report over the last `days` of events (all agents, or a
    single one via `agent`). Omitted `days` returns the configured default
    (``display.metrics_default_window_days`` - 1 out of the box); the 30-day cap
    stays a protective constant (it bounds the scan).
    `since_compact=true` additionally narrows each agent's events to those at
    or after its latest compact halt (echoed in `meta.since_compact`).
    `meta.total_events` counts every telemetry/log event in the window —
    including service-level rows (agent_id NULL) from every process, a scope
    widened by the W9 events-table switch (it was agent-kernel lines only
    before); audit events are excluded."""
    if days is None:
        days = settings.display.metrics_default_window_days
    try:
        with request.app.state.db_pool.connection() as conn:
            conn.execute(_READ_TIMEOUT)
            agg = fetch_aggregate(conn, days, agent, since_compact=since_compact)
    except psycopg.errors.QueryCanceled as exc:
        raise HTTPException(status_code=503, detail="metrics read timed out; retry") from exc
    _, data = build_report_from_aggregate(
        agg, days, agent, since_compact=since_compact, prices=request.app.state.catalog.prices
    )
    return MetricsReport(**data)


@router.get("/api/metrics/agents")
def get_metrics_agents(
    request: Request,
    # `days`'s range stays a protective constant (import-time Query bound;
    # task #3696 exception inventory); the default *window* is
    # display.metrics_default_window_days.
    days: Annotated[int | None, Query(ge=1, le=30)] = None,
    since_compact: Annotated[bool, Query()] = False,  # noqa: FBT002 — FastAPI query param
) -> AgentMetricsReport:
    """Per-agent breakdown of the last `days` of events — one
    headline-counter row per agent (cost / tokens / cache hit / turn + exec
    outcomes), sorted by cost descending. `since_compact=true` narrows each
    agent's events to those at or after its latest compact halt. Service-level
    events (no agent_id) count toward `meta.total_events` but produce no row —
    and the count covers every telemetry/log event in the window (all
    processes), the W9-widened scope documented on `get_metrics`."""
    if days is None:
        days = settings.display.metrics_default_window_days
    try:
        with request.app.state.db_pool.connection() as conn:
            conn.execute(_READ_TIMEOUT)
            total_events, rollups = fetch_agent_rollups(
                conn, days, since_compact=since_compact, prices=request.app.state.catalog.prices
            )
            labels: dict[int, str | None] = {}
            if rollups:
                rows = conn.execute(
                    "SELECT id, label FROM agents WHERE id = ANY(%s)", (list(rollups),)
                ).fetchall()
                labels = dict(rows)
    except psycopg.errors.QueryCanceled as exc:
        raise HTTPException(status_code=503, detail="metrics read timed out; retry") from exc
    items = [
        # telemetry_events.agent_id has no FK: a deleted agent's rows keep their id, label None.
        AgentMetricsItem(agent_id=aid, label=labels.get(aid), **rollup)
        for aid, rollup in rollups.items()
    ]
    items.sort(key=lambda item: (-item.cost_usd, item.agent_id))
    meta = MetricsMeta(
        window_days=days,
        agent_filter=None,
        generated_at=datetime.now(UTC).isoformat(),
        total_events=total_events,
        distinct_agents=len(rollups),
        since_compact=since_compact,
    )
    return AgentMetricsReport(meta=meta, agents=items)
