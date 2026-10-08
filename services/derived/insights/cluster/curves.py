"""The cluster curves: per-bucket cost by agent, active agents, messages and queue time.

Cost and activity come from `llm_usage` rows of `telemetry_events` (index `(agent_id, ts)`);
messages and queue time from the `send_message` rows of `audit_events` joined to the
`inbound_messages` row they name. Buckets are epoch-aligned and a bucket holds the events whose
timestamp falls in it: an LLM call by the time it was recorded (its end), a message by the time
it was sent.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, LiteralString, cast

import psycopg

from base.events.contract import LLM_USAGE_KEYS
from base.telemetry.event_sql import numeric
from services.derived.insights.cluster.schemas import (
    ClusterAgentCost,
    ClusterCurves,
    ClusterWindow,
    CurveBucket,
)
from services.derived.insights.cluster.window import bucket_start


def _usage_sql() -> str:
    cost = numeric(LLM_USAGE_KEYS["cost_usd"])
    return f"""
        SELECT floor(extract(epoch FROM ts) / %s)::bigint AS bucket, agent_id,
               count(*), COALESCE(sum({cost}), 0)::float8,
               count(*) FILTER (WHERE ({cost}) IS NULL)
        FROM telemetry_events
        WHERE event_name = 'llm_usage' AND agent_id = ANY(%s) AND ts >= %s AND ts < %s
        GROUP BY 1, 2
    """  # noqa: S608 — keys come from the registered payload constants


# `inbound_id` is the audit payload's reference to the inbound row (written with the message);
# the pattern guard keeps a malformed value from failing the cast.
INBOUND_ID_SQL = (
    "CASE WHEN a.attributes->>'inbound_id' ~ '^[0-9]+$' "
    "THEN (a.attributes->>'inbound_id')::bigint END"
)

# `agent_id` of a send_message audit row is the RECEIVER and `target_agent_id` the SENDER.
_MESSAGE_SQL = f"""
    SELECT floor(extract(epoch FROM a.ts) / %s)::bigint AS bucket, count(*),
           count(im.claimed_at),
           percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM im.claimed_at - im.created_at)),
           percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM im.claimed_at - im.created_at))
    FROM audit_events a
    LEFT JOIN inbound_messages im ON im.id = {INBOUND_ID_SQL}
    WHERE a.event_name = 'send_message' AND a.agent_id = ANY(%s) AND a.target_agent_id = ANY(%s)
      AND a.ts >= %s AND a.ts < %s
    GROUP BY 1
"""  # noqa: S608 — constant fragments only


def read(
    conn: psycopg.Connection[Any],
    agent_ids: list[int],
    start: datetime,
    end: datetime,
    width: int,
) -> ClusterCurves:
    """The curves of `agent_ids` over `[start, end)` in buckets of `width` seconds."""
    costs: dict[int, list[ClusterAgentCost]] = {}
    unpriced = 0
    for bucket, agent_id, calls, cost, unpriced_calls in conn.execute(
        cast(LiteralString, _usage_sql()), (width, agent_ids, start, end)
    ):
        costs.setdefault(int(bucket), []).append(
            ClusterAgentCost(agent_id=int(agent_id), calls=int(calls), cost_usd=float(cost))
        )
        unpriced += int(unpriced_calls)
    messages: dict[int, tuple[int, int, float | None, float | None]] = {}
    for bucket, count, claimed, p50, p95 in conn.execute(
        _MESSAGE_SQL, (width, agent_ids, agent_ids, start, end)
    ):
        messages[int(bucket)] = (
            int(count),
            int(claimed),
            None if p50 is None else float(p50),
            None if p95 is None else float(p95),
        )
    buckets: list[CurveBucket] = []
    for index in sorted(costs.keys() | messages.keys()):
        in_bucket = sorted(costs.get(index, []), key=lambda c: c.agent_id)
        count, claimed, p50, p95 = messages.get(index, (0, 0, None, None))
        buckets.append(
            CurveBucket(
                ts=bucket_start(index, width),
                costs=in_bucket,
                active_agents=len(in_bucket),
                messages=count,
                queue_samples=claimed,
                queue_p50_seconds=p50,
                queue_p95_seconds=p95,
            )
        )
    return ClusterCurves(
        window=ClusterWindow(from_=start, to=end),
        bucket_seconds=width,
        agent_ids=agent_ids,
        unpriced_calls=unpriced,
        buckets=buckets,
    )
