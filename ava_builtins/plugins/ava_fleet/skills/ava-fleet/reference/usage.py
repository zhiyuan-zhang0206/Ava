#!/usr/bin/env python3
"""Per-agent LLM spend, aggregated from `events` (event_name=llm_usage) — the budget watcher's meter.

A spawner points a watcher (`ava.watcher.launch` / `ava.watcher.cron`) at this
script to read what a delegation has cost so far and decide whether to press a
worker toward convergence or tell it to wrap up. It is the standalone,
skill-side cost reader: usage introspection is deliberately not a core SDK verb,
so a budget check is a bash call the watcher makes, not a capability the fleet
carries.

Cost accounting mirrors `gateway/routers/agent_inspect.py:_agent_cost` exactly —
one grouped scan of `event = 'llm_usage'` rows, summing `in_total` / `out_total`
/ `cache_read` / `reasoning` per (agent, model) and pricing each group through
`shared.lm.pricing.cost_usd` (the single pricing source). It restates the cost
basis the fleet dashboard uses (`_agent_cost`) and `shared.metrics` — the usage
SDK that used to be the agent-facing reader (`ava.self.usage` / `ava.agents.usage`)
has been removed, and this script is its skill-side replacement. Only the SQL is
restated here, never the rates, so these numbers never drift from the fleet
dashboard's. An unpriced model (absent from the pricing table) contributes 0 cost
and its calls land in `unpriced_calls` — never silently counted as $0 spend.

Usage:
    .venv/bin/python plugins/ava_fleet/skills/ava-fleet/reference/usage.py \
        --agent-id 1464 --agent-id 1465 --hours 3

    # whole-life, all agents:
    .venv/bin/python .../usage.py

Windows are `--since <ISO datetime>` (absolute cutoff) or `--hours <N>`
(relative); pass at most one, omit both for whole-life. Emits one JSON object
to stdout: `per_agent` keyed by agent id, plus a `total` rollup — the watcher
reads `per_agent[id]["cost_usd"]` / `total["cost_usd"]` against the budget.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Any, LiteralString

from shared.db import connect
from shared.events.contract import LLM_USAGE_KEYS, sql_join
from shared.lm.pricing import cost_usd


def _window_clause(since: datetime | None, hours: float | None) -> tuple[LiteralString, list[Any]]:
    """SQL fragment + params for the optional time window (mirrors
    `shared.usage._window`). At most one of `since` / `hours`; both None = whole
    life. The fragment is a literal so the composed query stays a LiteralString."""
    if since is not None and hours is not None:
        raise ValueError("pass at most one of --since / --hours")
    if since is not None:
        return " AND ts > %s", [since]
    if hours is not None:
        return " AND ts > now() - %s * interval '1 hour'", [float(hours)]
    return "", []


def _rows(
    agent_ids: list[int], since: datetime | None, hours: float | None
) -> list[tuple[Any, ...]]:
    """One grouped scan of `llm_usage` rows, by (agent_id, model). Optional
    `agent_ids` filter and time window; Pattern A connection (own cursor)."""
    where, params = _window_clause(since, hours)
    if agent_ids:
        where = " AND agent_id = ANY(%s)" + where
        params = [agent_ids, *params]
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            sql_join(
                "SELECT agent_id, COALESCE(",
                LLM_USAGE_KEYS["model"],
                ", ''), ",
                "       COALESCE(SUM((",
                LLM_USAGE_KEYS["in_total"],
                ")::bigint), 0), ",
                "       COALESCE(SUM((",
                LLM_USAGE_KEYS["out_total"],
                ")::bigint), 0), ",
                "       COALESCE(SUM((",
                LLM_USAGE_KEYS["cache_read"],
                ")::bigint), 0), ",
                "       COALESCE(SUM((",
                LLM_USAGE_KEYS["reasoning"],
                ")::bigint), 0), ",
                "       COUNT(*) ",
                "FROM events ",
                "WHERE event_name = 'llm_usage'",
                where,
                " ",
                "GROUP BY agent_id, COALESCE(",
                LLM_USAGE_KEYS["model"],
                ", '')",
            ),
            params,
        )
        return cur.fetchall()


def aggregate(rows: list[tuple[Any, ...]]) -> dict[str, Any]:
    """Price the grouped `(agent_id, model, in, out, cached, reasoning, calls)`
    rows into `{per_agent: {id: {...}}, total: {...}}`.

    Pure — no DB — so the pricing math is unit-tested directly. Each (agent,
    model) group is priced via `cost_usd`; None (unpriced model) adds 0 cost and
    its calls fall into `unpriced_calls`. Per-agent and total costs round once at
    the end, matching `_agent_cost`'s `round(cost, 4)`."""
    per_agent: dict[str, dict[str, Any]] = {}
    for agent_id, model, r_in, r_out, r_cached, r_reason, r_calls in rows:
        aid = str(agent_id)
        a = per_agent.setdefault(
            aid,
            {
                "cost_usd": 0.0,
                "llm_calls": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "tokens_cached": 0,
                "tokens_reasoning": 0,
                "unpriced_calls": 0,
                "by_model": {},
            },
        )
        m_in, m_out, m_cached, m_reason, m_calls = (
            int(r_in),
            int(r_out),
            int(r_cached),
            int(r_reason),
            int(r_calls),
        )
        priced = cost_usd(model, m_in, m_out, m_cached)
        a["by_model"][model] = {
            "cost_usd": None if priced is None else round(priced, 4),
            "llm_calls": m_calls,
            "tokens_in": m_in,
            "tokens_out": m_out,
            "tokens_cached": m_cached,
            "tokens_reasoning": m_reason,
        }
        a["cost_usd"] += 0.0 if priced is None else priced
        a["llm_calls"] += m_calls
        a["tokens_in"] += m_in
        a["tokens_out"] += m_out
        a["tokens_cached"] += m_cached
        a["tokens_reasoning"] += m_reason
        if priced is None:
            a["unpriced_calls"] += m_calls

    total = {
        "cost_usd": 0.0,
        "llm_calls": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "tokens_cached": 0,
        "tokens_reasoning": 0,
        "unpriced_calls": 0,
        "distinct_agents": len(per_agent),
    }
    for a in per_agent.values():
        for k in (
            "llm_calls",
            "tokens_in",
            "tokens_out",
            "tokens_cached",
            "tokens_reasoning",
            "unpriced_calls",
        ):
            total[k] += a[k]
        total["cost_usd"] += a["cost_usd"]
        a["cost_usd"] = round(a["cost_usd"], 4)
    total["cost_usd"] = round(total["cost_usd"], 4)
    return {"per_agent": per_agent, "total": total}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--agent-id",
        type=int,
        action="append",
        default=[],
        help="restrict to this agent id; repeat for several. Omit for all agents.",
    )
    ap.add_argument(
        "--since",
        type=datetime.fromisoformat,
        help="absolute cutoff, ISO-8601 (e.g. 2026-07-22T18:00).",
    )
    ap.add_argument("--hours", type=float, help="relative window, last N hours.")
    args = ap.parse_args(argv)

    result = aggregate(_rows(args.agent_id, args.since, args.hours))
    result["window"] = {
        "since": args.since.isoformat() if args.since else None,
        "hours": args.hours,
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
