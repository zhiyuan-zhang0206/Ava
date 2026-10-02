"""Per-agent LLM spend from the durable record — the budget watcher's meter.

A spawner points a watcher (`ava.watcher.launch` / `ava.watcher.cron`) at this
script to read what a delegation has cost so far and decide whether to press a
worker toward convergence or tell it to wrap up. It is the standalone,
skill-side cost reader: usage introspection is deliberately not a core SDK verb,
so a budget check is a bash call the watcher makes, not a capability the fleet
carries.

The report reads Postgres only:

- a windowed request (`--since` / `--hours`) aggregates the `llm_usage` rows of
  `telemetry_events` over the window;
- whole life reads the durable ledger (whole UTC days) — the folded sums of
  `agent_model_tokens_total` plus the `agent_model_tokens_daily` days after the fold watermark —
  and the `telemetry_events` tail from the ledger watermark (the midnight after the agent's newest
  rolled day) to now, so a maintenance-daemon lag widens the tail instead of opening a hole.

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
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, LiteralString, cast

from base.db import connect
from base.events.contract import LLM_USAGE_KEYS

_NUMBER = r"^-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?$"


def _window_bounds(
    since: datetime | None, hours: float | None
) -> tuple[datetime | None, datetime | None]:
    """(from_, to) for the event-side aggregates. ``(None, None)`` = whole
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
_Row = tuple[int, str, int, int, int, int, int, float, int]


def _number(key: str) -> str:
    expression = LLM_USAGE_KEYS[key]
    return f"CASE WHEN {expression} ~ '{_NUMBER}' THEN ({expression})::numeric END"


_COST = LLM_USAGE_KEYS["cost_usd"]

# Per (agent, model) llm_usage aggregates over [from, to) with one tail start per agent:
# one sum per token/cost field, one count, one count of rows without a cost snapshot.
_EVENT_SQL = f"""
    SELECT t.agent_id, COALESCE({LLM_USAGE_KEYS["model"]}, ''),
           COALESCE(sum({_number("in_total")}), 0)::bigint,
           COALESCE(sum({_number("out_total")}), 0)::bigint,
           COALESCE(sum({_number("cache_read")}), 0)::bigint,
           COALESCE(sum({_number("reasoning")}), 0)::bigint,
           count(*), COALESCE(sum({_number("cost_usd")}), 0)::float8,
           count(*) FILTER (WHERE COALESCE({_COST}, '') = '')
    FROM telemetry_events t
    JOIN unnest(%s::bigint[], %s::timestamptz[]) AS w(agent_id, tail_from)
      ON w.agent_id = t.agent_id
    WHERE t.event_name = 'llm_usage' AND t.ts >= w.tail_from AND t.ts < %s
    GROUP BY t.agent_id, 2
"""  # noqa: S608 — keys come from the registered payload constants

_ACTIVE_AGENTS_SQL = """
    SELECT DISTINCT agent_id FROM telemetry_events
    WHERE event_name = 'llm_usage' AND agent_id IS NOT NULL AND ts >= %s AND ts < %s
"""

# The ledger's whole-life sums come in two parts: the folded totals up to the watermark of
# `agent_model_tokens_total_through`, and the ledger days after it. Both group per (agent, model).
_THROUGH_SQL = "SELECT day FROM agent_model_tokens_total_through"

_TOTAL_SQL = """SELECT agent_id, model,
  llm_calls, costed_calls, unpriced_calls,
  tokens_in, tokens_out, tokens_cached, tokens_reasoning,
  cost_usd
FROM agent_model_tokens_total
WHERE agent_id = ANY(%s)"""

_LEDGER_SQL = """SELECT agent_id, model,
  sum(llm_calls), sum(costed_calls), sum(unpriced_calls),
  sum(tokens_in), sum(tokens_out), sum(tokens_cached), sum(tokens_reasoning),
  sum(cost_usd)
FROM agent_model_tokens_daily
WHERE agent_id = ANY(%s) AND day > %s
GROUP BY agent_id, model"""

_NEWEST_LEDGER_DAYS_SQL = """SELECT agent_id, max(day)
FROM agent_model_tokens_daily
WHERE agent_id = ANY(%s) AND day > %s
GROUP BY agent_id"""

_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


def _event_rows(conn: Any, tails: dict[int, datetime], until: datetime | None) -> list[_Row]:
    """The `llm_usage` rows of `telemetry_events` from each agent's tail start to `until`."""
    if not tails:
        return []
    agent_ids = sorted(tails)
    rows = conn.execute(
        cast(LiteralString, _EVENT_SQL),
        (agent_ids, [tails[a] for a in agent_ids], until or datetime.now(tz=UTC)),
    ).fetchall()
    return [
        (
            int(r[0]),
            str(r[1]),
            int(r[2]),
            int(r[3]),
            int(r[4]),
            int(r[5]),
            int(r[6]),
            float(r[7]),
            int(r[8]),
        )
        for r in rows
    ]


def _ledger_rows(conn: Any, agent_ids: list[int]) -> tuple[list[_Row], dict[int, datetime]]:
    """Ledger rows (folded totals + the days after the watermark) and each agent's live-tail start:
    the midnight after its newest ledger day, the midnight after the watermark for an agent whose
    ledger is folded entirely, and the beginning for an agent with no ledger row."""
    through_row = conn.execute(_THROUGH_SQL).fetchone()
    through: date = through_row[0] if through_row is not None else date.min
    rows: list[_Row] = []
    tails: dict[int, datetime] = dict.fromkeys(agent_ids, _EPOCH)
    folded_tail = datetime.combine(through + timedelta(days=1), time.min, tzinfo=UTC)
    for sql, params in ((_TOTAL_SQL, (agent_ids,)), (_LEDGER_SQL, (agent_ids, through))):
        for row in conn.execute(sql, params).fetchall():
            aid = int(row[0])
            rows.append(
                (
                    aid,
                    row[1],
                    int(row[5]),
                    int(row[6]),
                    int(row[7]),
                    int(row[8]),
                    int(row[2]),
                    float(row[9]),
                    int(row[4]),
                )
            )
            if sql is _TOTAL_SQL:
                tails[aid] = folded_tail
    for agent_id, day in conn.execute(_NEWEST_LEDGER_DAYS_SQL, (agent_ids, through)):
        tails[int(agent_id)] = datetime.combine(day + timedelta(days=1), time.min, tzinfo=UTC)
    return rows, tails


def _rows(agent_ids: list[int], since: datetime | None, hours: float | None) -> list[_Row]:
    """The merged row set for one request: windowed = the window's event rows; whole life = the
    ledger + each agent's event tail from its ledger watermark."""
    from_, to = _window_bounds(since, hours)
    with connect() as conn:
        if from_ is not None:
            if not agent_ids:
                agent_ids = sorted(
                    int(r[0]) for r in conn.execute(_ACTIVE_AGENTS_SQL, (from_, to)).fetchall()
                )
            return _event_rows(conn, dict.fromkeys(agent_ids, from_), to)
        if not agent_ids:
            agent_ids = sorted(
                int(r[0])
                for r in conn.execute(
                    "SELECT DISTINCT agent_id FROM agent_model_tokens_daily "
                    "UNION SELECT DISTINCT agent_id FROM telemetry_events "
                    "WHERE event_name = 'llm_usage' AND agent_id IS NOT NULL"
                ).fetchall()
            )
        rows, tails = _ledger_rows(conn, agent_ids)
        rows.extend(_event_rows(conn, tails, None))
        return rows


def aggregate(rows: list[_Row]) -> dict[str, Any]:
    """Fold the grouped rows into `{per_agent: {id: {...}}, total: {...}}`.

    Pure — no DB — so the folding math is unit-tested directly. Per
    (agent, model) the cost is the summed usage-time snapshot; a model with
    no costed call reports `cost_usd: None` in `by_model` (never silently
    $0). Per-agent and total costs round once at the end, matching
    the Inspector's four-decimal display precision."""
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
