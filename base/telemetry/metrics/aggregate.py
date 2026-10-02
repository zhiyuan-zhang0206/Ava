"""Aggregate fetch for metrics — the gateway + CLI path.

The digest's units reduce the window's telemetry and log rows to a handful of aggregates (counts,
sums, distributions, per-key counters, position buckets). `fetch_aggregate` reads those aggregates
from `telemetry_events` (`base.telemetry.metrics.aggregate_sql`, a few statements in one
connection, nothing materialized per row) and returns an `EventAggregate`;
`build_report_from_aggregate` / `agent_rollups_from_aggregate` rebuild the report from it, reusing
the helper functions (`pctiles`, `third_of`, `_fix_kinds`, `cost_usd`) so the math cannot drift.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg

from base.lm.plugin_providers import ensure_provider_plugins_loaded
from base.lm.pricing import cost_usd
from base.telemetry.metrics import (
    MetricSection,
    _fix_kinds,
    _render_agent_activity,
    _render_exec,
    _render_llm_turns,
    _render_plugin_activation,
    _render_sdk_usage,
    _render_syntax_fix,
    pctiles,
    third_of,
)
from base.telemetry.metrics.aggregate_sql import LIFECYCLE_EVENTS, read_agent_window, read_window


@dataclass(frozen=True)
class _LlmTotals:
    """Token + cost totals over one set of llm_usage rows — call count, token
    totals, cost, unpriced count (the SQL aggregate's per-model sums)."""

    calls: int
    tin: int
    tout: int
    tcached: int
    treason: int
    cost: float
    unpriced: int


@dataclass
class _PerAgentAgg:
    """Per-agent row aggregates — turn counts, exec outcomes, event span.

    Includes the service-level group (agent_id None): the units count it toward
    `turns_per_agent` and `agent_lifetime_s`, so the aggregate must too; the /agents
    endpoint filters it.
    """

    agent_id: int | None
    events: int
    turn_total: int
    turn_ok: int
    exec_ok: int
    exec_failed: int
    first_ts: datetime
    last_ts: datetime


@dataclass
class EventAggregate:
    """The window reduced in SQL — compact input for the report assemblers.

    Distributions that only feed `pctiles` keep their values (ints/floats); per-key counters are
    `{key: count}` mappings in descending count order.
    """

    total_events: int
    distinct_agents: int
    # syntax_fix
    code_blocks: int
    code_blocks_per_agent: dict[int | None, int]
    fix_events: list[tuple[int | None, int, str]]  # (agent_id, block_no, fixes) — time order
    # exec
    exec_ok: int
    exec_failed: int
    failure_types: dict[str, int]  # exc_type (else event name) -> count
    code_len: list[float]
    output_len: list[float]
    # llm / turns
    llm_by_model: list[
        tuple[str, int, int, int, int, int]
    ]  # (model, calls, in, out, cached, reasoning)
    llm_position: dict[str, tuple[int, int]]  # third -> (cache_read, in_total)
    turn_total: int
    turn_ok: int
    turn_durations: list[float]
    per_agent: dict[int | None, _PerAgentAgg]
    per_agent_llm: dict[
        int | None, list[tuple[str, int, int, int, int]]
    ]  # (model, calls, in, out, cached)
    # agent activity
    spawners: dict[str, int]  # spawner -> count
    lifecycle: dict[str, int]
    idle_halts: int
    # sdk usage
    sdk_fns: dict[str, int]
    # plugin activation
    plugin_acts: dict[tuple[str, str, str, str], int]  # (plugin, surface, identifier, model)


def _per_agent_aggs(
    per_agent: dict[int | None, dict[str, Any]], fallback: datetime
) -> dict[int | None, _PerAgentAgg]:
    return {
        agent: _PerAgentAgg(
            agent,
            int(row["events"]),
            int(row.get("turn_total", 0)),
            int(row.get("turn_ok", 0)),
            int(row.get("exec_ok", 0)),
            int(row.get("exec_failed", 0)),
            row.get("first_ts") or fallback,
            row.get("last_ts") or fallback,
        )
        for agent, row in per_agent.items()
        if row["events"]
    }


def _llm_by_agent(
    llm_per_agent: dict[int | None, dict[str, tuple[int, int, int, int, int]]],
) -> dict[int | None, list[tuple[str, int, int, int, int]]]:
    return {
        agent: [(model, n, i, o, c) for model, (n, i, o, c, _r) in models.items()]
        for agent, models in llm_per_agent.items()
    }


def _total(rows: list[dict[str, Any]], key: str) -> int:
    """The sum of one per-agent counter over every group (a group without it counts 0)."""
    return sum(int(r.get(key, 0)) for r in rows)


def _llm_by_model(
    llm_per_agent: dict[int | None, dict[str, tuple[int, int, int, int, int]]],
) -> list[tuple[str, int, int, int, int, int]]:
    """Per-model `(model, calls, in, out, cached, reasoning)` summed over the agents."""
    by_model: dict[str, list[int]] = {}
    for models in llm_per_agent.values():
        for model, sums in models.items():
            acc = by_model.setdefault(model, [0, 0, 0, 0, 0])
            for slot, value in enumerate(sums):
                acc[slot] += value
    return [(m, acc[0], acc[1], acc[2], acc[3], acc[4]) for m, acc in by_model.items()]


def fetch_aggregate(
    conn: psycopg.Connection[Any],
    days: int,
    agent_id: int | None,
    *,
    since_compact: bool = False,
    now: datetime | None = None,
) -> EventAggregate:
    """Windowed aggregate over `telemetry_events` — the gateway + CLI entry point.

    Category telemetry|log over [now-days, now], optional single-agent scope, optional
    since_compact (each agent's rows narrowed to at-or-after its latest compact halt; service rows
    always kept).
    """
    data = read_window(conn, days, agent_id, since_compact=since_compact, now=now)
    raw: dict[int | None, dict[str, Any]] = data["per_agent"]
    rows = list(raw.values())
    lengths = data["lengths"]
    return EventAggregate(
        total_events=_total(rows, "events"),
        distinct_agents=len([a for a in raw if a is not None]),
        code_blocks=_total(rows, "code"),
        code_blocks_per_agent={a: int(r["code"]) for a, r in raw.items() if r.get("code")},
        fix_events=data["fix_events"],
        exec_ok=_total(rows, "exec_ok"),
        exec_failed=_total(rows, "exec_failed"),
        failure_types=data["failure_types"],
        code_len=lengths["code_len"],
        output_len=lengths["output_len"],
        llm_by_model=_llm_by_model(data["llm_per_agent"]),
        llm_position=data["llm_position"],
        turn_total=_total(rows, "turn_total"),
        turn_ok=_total(rows, "turn_ok"),
        turn_durations=lengths["turn_dur"],
        per_agent=_per_agent_aggs(raw, data["start"]),
        per_agent_llm=_llm_by_agent(data["llm_per_agent"]),
        spawners=data["spawners"],
        lifecycle={
            name: sum(r.get("lifecycle", {}).get(name, 0) for r in rows)
            for name in LIFECYCLE_EVENTS.values()
        },
        idle_halts=_total(rows, "idle_halts"),
        sdk_fns=data["sdk_fns"],
        plugin_acts=data["plugin_acts"],
    )


def fetch_agent_rollups(
    conn: psycopg.Connection[Any],
    days: int,
    *,
    since_compact: bool = False,
    now: datetime | None = None,
) -> tuple[int, dict[int, dict[str, Any]]]:
    """`(total_events, per-agent headline counters)` of the window, for `/api/metrics/agents`.

    Runs only the statements that endpoint needs: the per-agent counters and the `llm_usage` sums.
    """
    data = read_agent_window(conn, days, since_compact=since_compact, now=now)
    raw: dict[int | None, dict[str, Any]] = data["per_agent"]
    per_agent = _per_agent_aggs(raw, datetime.now(UTC))
    total = sum(int(r["events"]) for r in raw.values())
    return total, agent_rollups(per_agent, _llm_by_agent(data["llm_per_agent"]))


def _llm_totals_from_models(rows: list[tuple[str, int, int, int, int, int]]) -> _LlmTotals:
    """Token + cost totals from per-model sums — cost_usd is linear in tokens
    per model, so pricing the summed tokens once per model equals the per-row
    loop (`_llm_totals`) exactly; every call on an unpriced model is unpriced."""
    ensure_provider_plugins_loaded()
    calls = sum(r[1] for r in rows)
    tin = sum(r[2] for r in rows)
    tout = sum(r[3] for r in rows)
    tcached = sum(r[4] for r in rows)
    treason = sum(r[5] for r in rows)
    cost = 0.0
    unpriced = 0
    for model, n, i, o, c, _r in rows:
        price = cost_usd(model, i, o, c)
        if price is None:
            unpriced += n
        else:
            cost += price
    return _LlmTotals(calls, tin, tout, tcached, treason, cost, unpriced)


def _sections_from_aggregate(agg: EventAggregate) -> list[MetricSection]:
    """Rebuild the five unit data dicts from `EventAggregate`, then render
    them with the shared render functions — the text blocks stay identical to
    the per-row path because the renders are the same functions."""
    # syntax_fix: per-event kinds (Counter, stream order) + per-block
    # ruff_format position thirds (block index i among the agent's n blocks ->
    # third_of(i, n)).
    kind_counts: Counter[str] = Counter()
    hits: dict[str, int] = {"early": 0, "mid": 0, "late": 0}
    blocks_b: dict[str, int] = {"early": 0, "mid": 0, "late": 0}
    for _agent_id, blk, fixes in agg.fix_events:
        if blk <= 0:
            continue
        kinds = set(_fix_kinds(fixes))
        kind_counts.update(kinds)
        b = third_of(blk - 1, agg.code_blocks_per_agent.get(_agent_id, 0))
        if "ruff_format" in kinds:
            hits[b] += 1
    for _agent_id, n in agg.code_blocks_per_agent.items():
        for i in range(n):
            blocks_b[third_of(i, n)] += 1
    rate = {
        b: round(hits[b] / blocks_b[b] * 100, 1) if blocks_b[b] else 0.0
        for b in ("early", "mid", "late")
    }
    data_syntax = {
        "code_blocks": agg.code_blocks,
        "trigger_counts": dict(kind_counts),
        "ruff_format_by_position": {
            b: {"hits": hits[b], "blocks": blocks_b[b]} for b in ("early", "mid", "late")
        },
        "ruff_format_rate_pct_by_position": rate,
    }

    # exec
    total_exec = agg.exec_ok + agg.exec_failed
    data_exec = {
        "exec_total": total_exec,
        "exec_ok": agg.exec_ok,
        "exec_failed": agg.exec_failed,
        "success_rate_pct": round(agg.exec_ok / total_exec * 100, 1) if total_exec else 0.0,
        "failure_types": dict(agg.failure_types),
        "code_len_chars": pctiles(agg.code_len),
        "output_len_chars": pctiles(agg.output_len),
    }

    # llm_turns
    totals = _llm_totals_from_models(agg.llm_by_model)
    # position thirds over each agent's llm_usage rows: (cache_read, in_total) sums.
    pos_hit = {
        b: round(c / i * 100, 1) if i else 0.0
        for b, (c, i) in ((b, agg.llm_position.get(b, (0, 0))) for b in ("early", "mid", "late"))
    }
    turns_per_agent = [row.turn_total for row in agg.per_agent.values()]
    data_llm = {
        "llm_calls": totals.calls,
        "tokens_in": totals.tin,
        "tokens_out": totals.tout,
        "tokens_cached": totals.tcached,
        "tokens_reasoning": totals.treason,
        "cache_hit_pct": round(totals.tcached / totals.tin * 100, 1) if totals.tin else 0.0,
        "cache_hit_pct_by_position": pos_hit,
        "cost_usd": round(totals.cost, 4),
        "cost_unpriced_calls": totals.unpriced,
        "turn_duration_s": pctiles(agg.turn_durations),
        "turn_ok": agg.turn_ok,
        "turn_total": agg.turn_total,
        "turns_per_agent": pctiles([float(x) for x in turns_per_agent]),
    }

    # agent_activity
    by_spawner: Counter[str] = Counter()
    subagents = 0
    for sp, count in agg.spawners.items():
        by_spawner[sp.split(":", 1)[0]] += count
        if sp.startswith("agent:"):
            subagents += count
    spans = [
        (row.last_ts - row.first_ts).total_seconds()
        for row in agg.per_agent.values()
        if row.events >= 2
    ]
    data_activity = {
        "distinct_agents": agg.distinct_agents,
        "spawns_total": sum(agg.spawners.values()),
        "spawns_by_spawner": dict(by_spawner),
        "subagent_spawns": subagents,
        "lifecycle": {
            k: agg.lifecycle.get(k, 0)
            for k in ("spawned", "terminated", "restarted", "resurrected")
        },
        "idle_halts": agg.idle_halts,
        "agent_lifetime_s": pctiles(spans),
    }

    # sdk_usage
    func_counts: Counter[str] = Counter(agg.sdk_fns)
    ns_counts: Counter[str] = Counter()
    for fq, cnt in func_counts.items():
        ns_counts[fq.split(".", 1)[0]] += cnt
    data_sdk = {
        "code_blocks": agg.code_blocks,
        "total_calls": sum(func_counts.values()),
        "distinct_functions": len(func_counts),
        "functions": [{"function": f, "count": c} for f, c in func_counts.most_common()],
        "by_namespace": dict(ns_counts),
    }

    # plugin_activation — philosophy §6's "removable as a gauge, not a vibe".
    # `by_contribution` is keyed the way `ava plugins inspect` spells a
    # registered contribution, so a row with no counterpart here is a
    # contribution that registered and never fired; `by_plugin_model` is the
    # per-model cut that answers "does model X still need this shim".
    act_plugin: Counter[str] = Counter()
    act_contribution: Counter[str] = Counter()
    act_plugin_model: Counter[str] = Counter()
    for (plugin, surface, identifier, model), count in agg.plugin_acts.items():
        act_plugin[plugin] += count
        act_contribution[f"{plugin}/{surface}/{identifier}"] += count
        act_plugin_model[f"{plugin}@{model or '?'}"] += count
    data_plugin = {
        "total_activations": sum(agg.plugin_acts.values()),
        "distinct_plugins": len(act_plugin),
        "by_plugin": dict(act_plugin),
        "by_contribution": [
            {"contribution": c, "count": n} for c, n in act_contribution.most_common()
        ],
        "by_plugin_model": dict(act_plugin_model),
    }

    return [
        MetricSection("syntax_fix", _render_syntax_fix(data_syntax), data_syntax),
        MetricSection("exec", _render_exec(data_exec), data_exec),
        MetricSection("llm_turns", _render_llm_turns(data_llm), data_llm),
        MetricSection("agent_activity", _render_agent_activity(data_activity), data_activity),
        MetricSection("sdk_usage", _render_sdk_usage(data_sdk), data_sdk),
        MetricSection("plugin_activation", _render_plugin_activation(data_plugin), data_plugin),
    ]


def build_report_from_aggregate(
    agg: EventAggregate,
    days: int,
    agent_id: int | None,
    *,
    since_compact: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Assemble the same text + data `build_report` produces, from an
    `EventAggregate` (see `fetch_aggregate`). Meta semantics are identical:
    `total_events` counts every telemetry/log row in the window (service-level
    rows included), `distinct_agents` excludes agent_id NULL."""
    sections = _sections_from_aggregate(agg)
    names = [s.name for s in sections]
    if len(names) != len(set(names)):
        raise ValueError(f"duplicate metric section names would clobber the JSON: {names}")
    generated_at = datetime.now(UTC).isoformat()
    meta: dict[str, Any] = {
        "window_days": days,
        "agent_filter": agent_id,
        "generated_at": generated_at,
        "total_events": agg.total_events,
        "distinct_agents": agg.distinct_agents,
        "since_compact": since_compact,
    }
    header = (
        "=" * 64
        + f"\n  Ava metrics — last {days}d"
        + (" — since last compact" if since_compact else "")
        + (f" — agent {agent_id}" if agent_id is not None else "")
        + f"\n  {agg.total_events} events / {agg.distinct_agents} agents"
        + f" / generated {generated_at[:19]}\n"
        + "=" * 64
        + "\n"
    )
    text = header + "\n" + "\n".join(s.text for s in sections)
    data = {"meta": meta, "metrics": {s.name: s.data for s in sections}}
    return text, data


def agent_rollups(
    per_agent: dict[int | None, _PerAgentAgg],
    per_agent_llm: dict[int | None, list[tuple[str, int, int, int, int]]],
) -> dict[int, dict[str, Any]]:
    """Per-agent headline counters for `/api/metrics/agents` (service rows excluded)."""
    ensure_provider_plugins_loaded()
    out: dict[int, dict[str, Any]] = {}
    for aid, row in per_agent.items():
        if aid is None:
            continue
        usage = per_agent_llm.get(aid, [])
        calls = sum(r[1] for r in usage)
        tin = sum(r[2] for r in usage)
        tout = sum(r[3] for r in usage)
        tcached = sum(r[4] for r in usage)
        cost = 0.0
        for model, _n, i, o, c in usage:
            price = cost_usd(model, i, o, c)
            if price is not None:
                cost += price
        out[aid] = {
            "events": row.events,
            "cost_usd": round(cost, 4),
            "llm_calls": calls,
            "tokens_in": tin,
            "tokens_out": tout,
            "tokens_cached": tcached,
            "cache_hit_pct": round(tcached / tin * 100, 1) if tin else 0.0,
            "turn_ok": row.turn_ok,
            "turn_total": row.turn_total,
            "exec_ok": row.exec_ok,
            "exec_failed": row.exec_failed,
        }
    return out
