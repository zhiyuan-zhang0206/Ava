#!/usr/bin/env python3
"""Per-agent LLM spend from the LGTM read side — the budget watcher's meter.

A spawner points a watcher (`ava.watcher.launch` / `ava.watcher.cron`) at this
script to read what a delegation has cost so far and decide whether to press a
worker toward convergence or tell it to wrap up. It is the standalone,
skill-side cost reader: usage introspection is deliberately not a core SDK verb,
so a budget check is a bash call the watcher makes, not a capability the fleet
carries.

The read mirrors `gateway/routers/_agent_cost.py` — the same two surfaces the
fleet dashboard's cost path uses since the LGTM cutover (task #1197: the PG
`events` table is a frozen archive, task #180, and this script no longer reads
it):

- a windowed request (`--since` / `--hours`) aggregates **pure Loki** over the
  window;
- whole life reads the durable `agent_model_tokens_daily` ledger (whole UTC
  days) + a **Loki tail** from the ledger watermark (the midnight after the
  newest rolled day) to now, so a maintenance-daemon lag widens the tail
  instead of opening a hole.

Cost is summed from usage-time `cost_usd` snapshots — never re-priced at read
time (the pricing table is not consulted; a call without a snapshot counts in
`unpriced_calls` and contributes 0 cost). The same accounting the fleet
dashboard uses, so these numbers never drift from it.

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
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from gateway import loki_events
from shared.db import connect
from shared.loki_index_labels import ledger_gap_plan, retention_floor

# The token/cost payload fields summed per (agent, model) — the llm_usage
# payload keys (shared/events/contract.py LLM_USAGE_KEYS).
_SUM_FIELDS = ("in_total", "out_total", "cache_read", "reasoning", "cost_usd")

# llm_usage category filter — telemetry since the 2026-08-05 convention, plus
# the pre-convention log rows (the same predicate the gateway cost path uses).
_CATEGORIES = ["telemetry", "log"]

# Polite spacing between per-agent Loki query groups — a whole-life run over
# many agents fans out ~7 instant queries per agent; keep the burst bounded
# (Loki query-load discipline, 2026-08-18 storm).
_AGENT_QUERY_PAUSE_S = 0.15


def _window_bounds(
    since: datetime | None, hours: float | None
) -> tuple[datetime | None, datetime | None]:
    """(from_, to) for the Loki-side aggregates. ``(None, None)`` = whole
    life (the caller switches to the ledger + tail path). At most one of
    ``since`` / ``hours``; ``hours`` counts back from now."""
    if since is not None and hours is not None:
        raise ValueError("pass at most one of --since / --hours")
    if since is not None:
        return since, datetime.now(tz=UTC)
    if hours is not None:
        now = datetime.now(tz=UTC)
        return now - timedelta(hours=float(hours)), now
    return None, None


# One grouped row per (agent, model): tokens in/out/cached/reasoning, calls,
# summed cost snapshots, unpriced calls. The aggregate() contract.
_LokiRow = tuple[int, str, int, int, int, int, int, float, int]


def _loki_rows(agent_ids: list[int], from_: datetime, to: datetime | None) -> list[_LokiRow]:
    """Per (agent, model) llm_usage aggregates from pure Loki over
    [from_, to) — the same subqueries `_agent_cost._loki_aggs_into` runs:
    one sum per token/cost field, one count, one count restricted to rows
    carrying a cost snapshot (unpriced = calls - costed)."""
    out: list[_LokiRow] = []
    for aid in agent_ids:
        common: dict[str, Any] = {
            "event_names": ["llm_usage"],
            "categories": _CATEGORIES,
            "agent_id": aid,
            "from_": from_,
            "to": to,
        }
        sums = {
            key: dict(
                loki_events.attribute_aggregate(field=key, agg="sum", group_by="model", **common)
            )
            for key in _SUM_FIELDS
        }
        calls = dict(
            loki_events.attribute_aggregate(
                field="in_total", agg="count", group_by="model", **common
            )
        )
        costed = dict(
            loki_events.attribute_aggregate(
                field="in_total",
                agg="count",
                group_by="model",
                attribute_filters={"cost_usd": "!="},
                **common,
            )
        )
        for model in calls:
            n_calls = int(calls.get(model, 0))
            out.append(
                (
                    aid,
                    model,
                    int(sums["in_total"].get(model, 0.0)),
                    int(sums["out_total"].get(model, 0.0)),
                    int(sums["cache_read"].get(model, 0.0)),
                    int(sums["reasoning"].get(model, 0.0)),
                    n_calls,
                    float(sums["cost_usd"].get(model, 0.0)),
                    n_calls - int(costed.get(model, 0)),
                )
            )
        time.sleep(_AGENT_QUERY_PAUSE_S)
    return out


_LEDGER_SQL = """SELECT model,
  sum(llm_calls), sum(costed_calls), sum(unpriced_calls),
  sum(tokens_in), sum(tokens_out), sum(tokens_cached), sum(tokens_reasoning),
  sum(cost_usd)
FROM agent_model_tokens_daily
WHERE agent_id = %s{}
GROUP BY model"""

_NEWEST_LEDGER_DAYS_SQL = """SELECT agent_id, max(day)
FROM agent_model_tokens_daily
WHERE agent_id = ANY(%s)
GROUP BY agent_id"""


def _ledger_rows(
    agent_ids: list[int], floor: datetime
) -> tuple[list[_LokiRow], dict[int, datetime]]:
    """Ledger rows and per-agent live-tail starts, mirroring gateway cost.

    A retained newest day is excluded from each agent's sum and wholly reread
    from Loki; non-retained ledger history stays durable. Ledger
    ``unpriced_calls`` maps directly to the row's unpriced slot.
    """
    rows: list[_LokiRow] = []
    tails: dict[int, datetime] = {}
    with connect() as conn, conn.cursor() as cur:
        cur.execute(_NEWEST_LEDGER_DAYS_SQL, (agent_ids,))
        newest_days = {int(agent_id): day for agent_id, day in cur.fetchall()}
        for aid in agent_ids:
            gap = ledger_gap_plan(newest_days.get(aid), floor)
            tails[aid] = gap.tail_from
            condition = " AND day < %s" if gap.day_lt is not None else ""
            params = (aid, gap.day_lt) if gap.day_lt is not None else (aid,)
            cur.execute(_LEDGER_SQL.format(condition), params)
            for row in cur.fetchall():
                rows.append(
                    (
                        aid,
                        row[0],
                        int(row[4]),
                        int(row[5]),
                        int(row[6]),
                        int(row[7]),
                        int(row[1]),
                        float(row[8]),
                        int(row[3]),
                    )
                )
    return rows, tails


def _active_agents(from_: datetime, to: datetime | None) -> list[int]:
    """Agent ids with llm_usage events in Loki over [from_, to) — the
    all-agents fallback (no --agent-id) and the ledger-less tail window."""
    counts = loki_events.count_grouped(
        group_by="agent_id",
        exclude_empty=True,
        event_names=["llm_usage"],
        categories=_CATEGORIES,
        from_=from_,
        to=to,
    )
    return sorted(int(k) for k in counts)


def _rows(agent_ids: list[int], since: datetime | None, hours: float | None) -> list[_LokiRow]:
    """The merged row set for one request, mirroring `_agent_cost.agent_cost`:
    windowed = pure Loki over the window; whole life = ledger + the per-agent
    Loki tail from the shared gap-day plan (ledger-less agents tail from the
    retention floor — data older than retention is indistinguishable from
    never having cost, exactly like the gateway cost path)."""
    from_, to = _window_bounds(since, hours)
    if from_ is not None:
        if not agent_ids:
            agent_ids = _active_agents(from_, to)
        return _loki_rows(agent_ids, from_, to)

    floor = retention_floor()
    now = datetime.now(tz=UTC)
    if not agent_ids:
        agent_ids = _active_agents(floor, now)
    rows, tails = _ledger_rows(agent_ids, floor)
    for aid in agent_ids:
        tail_from = tails[aid]
        rows.extend(_loki_rows([aid], tail_from, now))
    return rows


def aggregate(rows: list[_LokiRow]) -> dict[str, Any]:
    """Fold the grouped rows into `{per_agent: {id: {...}}, total: {...}}`.

    Pure — no DB/Loki — so the folding math is unit-tested directly. Per
    (agent, model) the cost is the summed usage-time snapshot; a model with
    no costed call reports `cost_usd: None` in `by_model` (never silently
    $0). Per-agent and total costs round once at the end, matching
    `_agent_cost`'s `round(cost, 4)`."""
    per_agent: dict[str, dict[str, Any]] = {}
    for aid, model, r_in, r_out, r_cached, r_reason, r_calls, r_cost, r_unpriced in rows:
        key = str(aid)
        a = per_agent.setdefault(
            key,
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
        a["by_model"][model] = {
            "cost_usd": round(r_cost, 4) if r_calls > r_unpriced else None,
            "llm_calls": r_calls,
            "tokens_in": r_in,
            "tokens_out": r_out,
            "tokens_cached": r_cached,
            "tokens_reasoning": r_reason,
        }
        a["cost_usd"] += r_cost
        a["llm_calls"] += r_calls
        a["tokens_in"] += r_in
        a["tokens_out"] += r_out
        a["tokens_cached"] += r_cached
        a["tokens_reasoning"] += r_reason
        a["unpriced_calls"] += r_unpriced

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
