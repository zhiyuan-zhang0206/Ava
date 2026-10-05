"""Selectable agent usage scopes over durable events and the lifetime ledger."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from datetime import time as day_time
from typing import Any, Literal, LiteralString, cast

import psycopg

from base.events.contract import LLM_USAGE_KEYS
from base.telemetry.event_sql import numeric

# One grouped row per (agent, model): tokens in/out/cached/reasoning, calls,
# summed cost snapshots, unpriced calls. The aggregate() contract.
_Row = tuple[int, str, int, int, int, int, int, float, int]


_COST = LLM_USAGE_KEYS["cost_usd"]

# Per (agent, model) llm_usage aggregates over [from, to) with one tail start per agent:
# one sum per token/cost field, one count, one count of rows without a cost snapshot.
_EVENT_SQL = f"""
    SELECT t.agent_id, COALESCE({LLM_USAGE_KEYS["model"]}, ''),
           COALESCE(sum({numeric(LLM_USAGE_KEYS["in_total"])}), 0)::bigint,
           COALESCE(sum({numeric(LLM_USAGE_KEYS["out_total"])}), 0)::bigint,
           COALESCE(sum({numeric(LLM_USAGE_KEYS["cache_read"])}), 0)::bigint,
           COALESCE(sum({numeric(LLM_USAGE_KEYS["reasoning"])}), 0)::bigint,
           count(*), COALESCE(sum({numeric(LLM_USAGE_KEYS["cost_usd"])}), 0)::float8,
           count(*) FILTER (WHERE ({numeric(_COST)}) IS NULL)
    FROM telemetry_events t
    JOIN unnest(%s::bigint[], %s::timestamptz[]) AS w(agent_id, tail_from)
      ON w.agent_id = t.agent_id
    WHERE t.event_name = 'llm_usage' AND t.ts >= w.tail_from AND t.ts < %s
    GROUP BY t.agent_id, 2
"""  # noqa: S608 — keys come from the registered payload constants

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
    folded_tail = datetime.combine(through + timedelta(days=1), day_time.min, tzinfo=UTC)
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
        tails[int(agent_id)] = datetime.combine(day + timedelta(days=1), day_time.min, tzinfo=UTC)
    return rows, tails


def aggregate(rows: list[_Row]) -> dict[str, Any]:
    """Fold the grouped rows into `{per_agent: {id: {...}}, total: {...}}`.

    Pure — no DB — so the folding math is unit-tested directly. Per
    (agent, model) the cost is the summed usage-time snapshot; a model with
    no costed call reports `cost_usd: None` in `by_model` (never silently
    $0). Reports retain precision for budget comparisons."""
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
        model_usage = a["by_model"].setdefault(
            model,
            {
                "cost_usd": None,
                "llm_calls": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "tokens_cached": 0,
                "tokens_reasoning": 0,
            },
        )
        if r_calls > r_unpriced:
            model_usage["cost_usd"] = (model_usage["cost_usd"] or 0.0) + r_cost
        for key, value in (
            ("llm_calls", r_calls),
            ("tokens_in", r_in),
            ("tokens_out", r_out),
            ("tokens_cached", r_cached),
            ("tokens_reasoning", r_reason),
        ):
            model_usage[key] += value
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
    return {"per_agent": per_agent, "total": total}


Lineage = Literal["self", "spawn", "fork", "all"]


def select_agents(
    conn: psycopg.Connection[Any], roots: Sequence[int], lineage: Lineage
) -> list[int]:
    """Include roots and recursively traverse only the selected birth edges.

    Spawn edges exclude forks. Fork edges follow the context source, not the
    caller who requested a fork. `all` traverses either edge, including mixed
    chains. Immutable birth ancestry survives later tree folding. UNION prevents
    overlapping roots and historical cycles from counting an agent twice.
    """
    if lineage not in {"self", "spawn", "fork", "all"}:
        raise ValueError(f"unknown lineage: {lineage!r}")
    if not roots or any(type(root) is not int or root <= 0 for root in roots):
        raise ValueError("roots must contain positive integer agent IDs")
    unique_roots = sorted(set(roots))
    found = {
        row[0]
        for row in conn.execute("SELECT id FROM agents_meta WHERE id = ANY(%s)", (unique_roots,))
    }
    missing = set(unique_roots) - found
    if missing:
        raise ValueError(f"unknown agent IDs: {sorted(missing)}")
    if lineage == "self":
        return unique_roots
    rows = conn.execute(
        """WITH RECURSIVE selected(id) AS (
            SELECT id FROM agents_meta WHERE id = ANY(%s)
            UNION
            SELECT child.id FROM agents_meta child JOIN selected parent ON (
                (%s IN ('spawn', 'all') AND child.fork_source_agent_id IS NULL
                 AND COALESCE(child.born_spawner, child.spawner) = 'agent:' || parent.id::text)
                OR (%s IN ('fork', 'all') AND child.fork_source_agent_id = parent.id)
            )
        ) SELECT id FROM selected ORDER BY id""",
        (unique_roots, lineage, lineage),
    )
    return [row[0] for row in rows]


def usage_report(
    conn: psycopg.Connection[Any],
    *,
    roots: Sequence[int],
    lineage: Lineage,
    start: datetime | None,
    end: datetime,
) -> dict[str, Any]:
    """Report recorded usage in [start, end), or lifetime ledger + tail.

    Lifetime totals use folded history and must end at the current time. Missing
    prices remain unpriced_calls; in-flight calls and other expenses are absent.
    """
    if end.utcoffset() is None or (
        start is not None and (start.utcoffset() is None or start >= end)
    ):
        raise ValueError("window must have timezone-aware timestamps with start < end")
    agents = select_agents(conn, roots, lineage)
    if start is None:
        rows, tails = _ledger_rows(conn, agents)
        rows.extend(_event_rows(conn, tails, end))
    else:
        rows = _event_rows(conn, dict.fromkeys(agents, start), end)
    folded = aggregate(rows)
    per_agent: list[dict[str, Any]] = []
    for agent_id in agents:
        row = folded["per_agent"].get(str(agent_id), {})
        per_agent.append(
            {
                "agent_id": agent_id,
                "calls": row.get("llm_calls", 0),
                "input_tokens": row.get("tokens_in", 0),
                "output_tokens": row.get("tokens_out", 0),
                "cache_read_tokens": row.get("tokens_cached", 0),
                "reasoning_tokens": row.get("tokens_reasoning", 0),
                "total_tokens": row.get("tokens_in", 0) + row.get("tokens_out", 0),
                "recorded_cost_usd": row.get("cost_usd", 0.0),
                "unpriced_calls": row.get("unpriced_calls", 0),
                "by_model": row.get("by_model", {}),
            }
        )
    totals: dict[str, Any] = {
        key: sum(agent[key] for agent in per_agent)
        for key in per_agent[0]
        if key not in {"agent_id", "by_model"}
    }
    return {
        "roots": sorted(set(roots)),
        "lineage": lineage,
        "start": start.isoformat() if start is not None else None,
        "end": end.isoformat(),
        "agents": per_agent,
        "totals": totals,
        "coverage": "Recorded LLM usage only; windows use retained events, lifetime uses ledger + tail. Excludes in-flight/unreported usage and other expenses.",
    }
