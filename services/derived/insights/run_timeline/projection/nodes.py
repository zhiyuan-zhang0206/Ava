# meiva:ref services/derived/insights/run_timeline/docs/range-projection.meiva.okf.md#indexed-history-windows
from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from base.agents.history.hierarchy.store import load_generation_costs
from base.db import Database
from services.derived.insights.run_timeline.projection.model import VERSION, Fact, Totals
from services.derived.insights.run_timeline.schemas import (
    RunTimelineGeneration,
    RunTimelineNode,
    RunTimelineUsage,
)


def _bounds(
    conn: Connection[Any],
    agent_id: int,
    sessions: int,
    tail: list[Fact],
    start: datetime,
    end: datetime,
) -> tuple[int, int] | None:
    low = conn.execute(
        "SELECT idx FROM run_timeline_message_facts WHERE agent_id = %s AND version = %s"
        " AND session < %s AND end_ts >= %s ORDER BY end_ts, idx LIMIT 1",
        (agent_id, VERSION, sessions, start),
    ).fetchone()
    high = conn.execute(
        "SELECT idx FROM run_timeline_message_facts WHERE agent_id = %s AND version = %s"
        " AND session < %s AND start_ts <= %s ORDER BY start_ts DESC, idx DESC LIMIT 1",
        (agent_id, VERSION, sessions, end),
    ).fetchone()
    lows = [f.idx for f in tail if f.end_ts is not None and f.end_ts >= start]
    highs = [f.idx for f in tail if f.start_ts is not None and f.start_ts <= end]
    if low:
        lows.append(low[0])
    if high:
        highs.append(high[0])
    return (min(lows), max(highs)) if lows and highs and min(lows) <= max(highs) else None


_NODES = """
WITH tail AS MATERIALIZED (
    SELECT * FROM jsonb_to_recordset(%s) AS f(
        idx integer, start_ts timestamptz, end_ts timestamptz,
        before_totals jsonb, after_totals jsonb
    )
)
SELECT n.id, n.depth, n.parent_id, n.span_start, n.span_end, n.text, n.job_id, n.check_key,
       coalesce(a.start_ts, ta.start_ts) AS start,
       coalesce(b.end_ts, tb.end_ts) AS end,
       coalesce(a.before_totals, ta.before_totals) AS before,
       coalesce(b.after_totals, tb.after_totals) AS after
FROM understanding_nodes n
LEFT JOIN run_timeline_message_facts a
    ON a.agent_id = n.agent_id AND a.version = %s AND a.session < %s AND a.idx = n.span_start
LEFT JOIN run_timeline_message_facts b
    ON b.agent_id = n.agent_id AND b.version = %s AND b.session < %s AND b.idx = n.span_end
LEFT JOIN tail ta ON ta.idx = n.span_start
LEFT JOIN tail tb ON tb.idx = n.span_end
WHERE n.agent_id = %s AND n.span_start <= %s AND n.span_end >= %s AND n.span_end < %s
  AND n.start_ts IS NOT NULL AND n.end_ts IS NOT NULL
  AND coalesce(a.start_ts, ta.start_ts) <= %s AND coalesce(b.end_ts, tb.end_ts) >= %s
ORDER BY n.depth, n.span_start
"""


def window_nodes(
    db: Database,
    conn: Connection[Any],
    agent_id: int,
    sessions: int,
    count: int,
    tail: list[Fact],
    start: datetime,
    end: datetime,
) -> list[RunTimelineNode]:
    bounds = _bounds(conn, agent_id, sessions, tail, start, end)
    if bounds is None:
        return []
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            _NODES,
            (
                Jsonb([f.model_dump(mode="json") for f in tail]),
                VERSION,
                sessions,
                VERSION,
                sessions,
                agent_id,
                bounds[1],
                bounds[0],
                count,
                end,
                start,
            ),
        ).fetchall()
    jobs, checks = load_generation_costs(
        db,
        agent_id,
        job_ids=sorted({r["job_id"] for r in rows if r["job_id"] is not None}),
        check_keys=sorted({r["check_key"] for r in rows if r["check_key"] is not None}),
    )
    out: list[RunTimelineNode] = []
    for row in rows:
        before, after = Totals.model_validate(row["before"]), Totals.model_validate(row["after"])
        usage = {
            name: getattr(after, name) - getattr(before, name)
            for name in RunTimelineUsage.model_fields
        }
        usage["cost_usd"] = round(usage["cost_usd"], 9)
        generation = jobs.get(row["job_id"]) if row["depth"] == 1 else checks.get(row["check_key"])
        known = after.known > before.known
        out.append(
            RunTimelineNode(
                id=str(row["id"]),
                level=row["depth"],
                parent=str(row["parent_id"]) if row["parent_id"] else None,
                start=row["start"],
                end=row["end"],
                span_start=row["span_start"],
                span_end=row["span_end"],
                summary=row["text"],
                usage=RunTimelineUsage.model_validate(usage),
                generation=RunTimelineGeneration(**vars(generation)) if generation else None,
                context_tokens=after.context - before.context if known else None,
                estimated=after.estimated > before.estimated if known else None,
            )
        )
    return out
