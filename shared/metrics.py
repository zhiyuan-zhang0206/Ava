"""Render helpers for the metrics report over the unified `events` table.

This module is the shared rendering layer for the SQL-aggregated metrics path
(`shared.metrics_aggregate`): it owns the ASCII text blocks and the machine
data shapes (`MetricSection`, `Pctiles`) that the gateway report endpoint and
the CLI both emit. The per-row computation it used to host (the
`EventRow` / `EventIndex` / `@metric_unit` registry and the `query_events`
fetch) was retired with the SQL aggregation (single-day materialization
outgrew 430K+ rows; the aggregate path reduced it to a handful of SQL GROUP
BYs). The only production importer is `metrics_aggregate`, which computes the
data dicts in SQL and calls the render helpers here — the math (pctiles,
third_of, fix-kind parsing) cannot drift because both sides share these
functions.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from typing import Any, TypedDict


@dataclass
class MetricSection:
    name: str  # unit key, e.g. "syntax_fix"
    text: str  # human/agent-readable block
    data: dict[str, Any]  # machine fragment for the JSON / API


class Pctiles(TypedDict):
    """A distribution digest — the shape `pctiles` produces and both
    `render_pctiles` and the machine-readable `data` fragment consume. A
    TypedDict (not NamedTuple) so it stays a JSON object over `/api/metrics`."""

    n: int
    p50: float
    p90: float
    max: float
    mean: float


def pctiles(values: list[float]) -> Pctiles:
    """n / p50 / p90 / max / mean. Empty -> all zero (n=0).

    p50/p90 are nearest-rank (index = floor(p*n)), not interpolated, so they
    differ slightly from SQL percentile_cont — fine for a digest.
    """
    if not values:
        return {"n": 0, "p50": 0, "p90": 0, "max": 0, "mean": 0}
    s = sorted(values)

    def q(p: float) -> float:
        return s[min(len(s) - 1, int(p * len(s)))]

    return {
        "n": len(s),
        "p50": round(q(0.5), 2),
        "p90": round(q(0.9), 2),
        "max": round(s[-1], 2),
        "mean": round(statistics.mean(s), 2),
    }


def third_of(index: int, total: int) -> str:
    """Bucket a position into early / mid / late thirds of its thread."""
    if index < total / 3:
        return "early"
    if index < 2 * total / 3:
        return "mid"
    return "late"


def render_bar(count: int, max_count: int, width: int = 24) -> str:
    if max_count <= 0:
        return ""
    return "#" * max(1, round(count / max_count * width)) if count else ""


def render_counts(title: str, counts: dict[str, int], total: int | None = None) -> str:
    """A title + one bar line per key, sorted by count desc."""
    if not counts:
        return f"{title}\n  (none)\n"
    top = max(counts.values())
    lines = [title]
    for k, c in sorted(counts.items(), key=lambda kv: -kv[1]):
        share = f"{c / total * 100:>4.0f}%" if total else "     "
        lines.append(f"  {k:<22}{c:>7} {share}  {render_bar(c, top)}")
    return "\n".join(lines) + "\n"


def render_pctiles(title: str, p: Pctiles) -> str:
    return (
        f"{title}\n  n={p['n']}  p50={p['p50']}  p90={p['p90']}  max={p['max']}  mean={p['mean']}\n"
    )


def _render_syntax_fix(data: dict[str, Any]) -> str:
    text = render_counts(
        "syntax_fix trigger counts (per code block):", data["trigger_counts"], data["code_blocks"]
    )
    text += "\nruff_format trigger rate by thread position:\n"
    for b in ("early", "mid", "late"):
        h, t = (
            data["ruff_format_by_position"][b]["hits"],
            data["ruff_format_by_position"][b]["blocks"],
        )
        text += f"  {b:<6} {data['ruff_format_rate_pct_by_position'][b]:>5.1f}%  ({h}/{t})  {render_bar(int(data['ruff_format_rate_pct_by_position'][b]), 100, 20)}\n"
    return text


def _render_exec(data: dict[str, Any]) -> str:
    text = (
        f"exec: {data['exec_ok']} ok / {data['exec_failed']} failed  (success {data['success_rate_pct']}%)\n"
        + render_pctiles("executed-code length (chars):", data["code_len_chars"])
        + render_pctiles("exec output length (chars):", data["output_len_chars"])
    )
    if data["failure_types"]:
        text += render_counts("failure types:", data["failure_types"])
    return text


def _render_llm_turns(data: dict[str, Any]) -> str:
    text = (
        f"llm: {data['llm_calls']} calls  in={data['tokens_in']} out={data['tokens_out']} "
        f"cached={data['tokens_cached']} reason={data['tokens_reasoning']}\n"
        f"  cache hit overall: {data['cache_hit_pct']}%   cost: ${data['cost_usd']}"
        + (
            f"  ({data['cost_unpriced_calls']} calls unpriced, excluded)"
            if data["cost_unpriced_calls"]
            else ""
        )
        + "\n  cache hit by turn position:\n"
    )
    for b in ("early", "mid", "late"):
        text += f"    {b:<6} {data['cache_hit_pct_by_position'][b]:>5.1f}%  {render_bar(int(data['cache_hit_pct_by_position'][b]), 100, 20)}\n"
    text += render_pctiles("turn duration (s):", data["turn_duration_s"])
    text += render_pctiles("turns per agent:", data["turns_per_agent"])
    return text


def _render_agent_activity(data: dict[str, Any]) -> str:
    text = (
        f"agents: {data['distinct_agents']} distinct  "
        f"{data['spawns_total']} spawns ({data['subagent_spawns']} subagents)  {data['idle_halts']} idle halts\n"
        f"  lifecycle: terminated={data['lifecycle']['terminated']} "
        f"restarted={data['lifecycle']['restarted']} resurrected={data['lifecycle']['resurrected']}\n"
    )
    if data["spawns_by_spawner"]:
        text += render_counts("spawns by spawner:", data["spawns_by_spawner"])
    text += render_pctiles("agent lifetime (s, within window):", data["agent_lifetime_s"])
    return text


def _render_sdk_usage(data: dict[str, Any]) -> str:
    text = f"sdk calls: {data['total_calls']} in {data['code_blocks']} code blocks  "
    text += f"({data['distinct_functions']} distinct functions)\n"
    if data["functions"]:
        max_c = int(data["functions"][0]["count"])
        for item in data["functions"][:20]:
            bar = render_bar(int(item["count"]), max_c)
            text += f"  ava.{item['function']:<30} {item['count']:>6}  {bar}\n"
    else:
        text += "  (none)\n"
    return text


def _render_plugin_activation(data: dict[str, Any]) -> str:
    """Which plugin injection surfaces actually fired in the window.

    Philosophy §6 asks a shim to measure its own obsolescence, so the block
    leads with the per-contribution counts (the same
    `<plugin>/<surface>/<identifier>` key `ava plugins inspect` lists as
    registered) and closes with the per-model cut: a contribution sitting at
    zero for a model across successive windows is the removal evidence.
    """
    text = (
        f"plugin activations: {data['total_activations']} "
        f"across {data['distinct_plugins']} plugins\n"
    )
    if data["by_contribution"]:
        max_c = int(data["by_contribution"][0]["count"])
        for item in data["by_contribution"][:20]:
            bar = render_bar(int(item["count"]), max_c)
            text += f"  {item['contribution']:<46} {item['count']:>6}  {bar}\n"
        text += render_counts("by plugin x model:", data["by_plugin_model"])
    else:
        text += "  (none — no plugin hook, wrap, or prompt section fired)\n"
    return text


def _fix_kinds(fixes: str) -> list[str]:
    """Parse a syntax_fix `fixes` payload, e.g. "ruff,chinese_punct(3)".

    Strips the per-block `(n)` count suffix, yielding the kind name only.
    The "none" sentinel (logged on the llm_repair / compile_failed paths when
    no deterministic fix ran) is not a fix kind and is dropped.
    """
    out: list[str] = []
    for raw in fixes.split(","):
        part = raw.strip()
        if part and part != "none":
            out.append(re.sub(r"\(\d+\)$", "", part))
    return out
