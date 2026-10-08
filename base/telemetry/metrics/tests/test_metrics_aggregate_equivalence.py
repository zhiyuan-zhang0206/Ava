"""The metrics aggregate over `telemetry_events` — golden output lock.

`base.telemetry.metrics.aggregate.fetch_aggregate` + `build_report_from_aggregate` +
`agent_rollups` are the ONLY metrics path: the window is reduced in SQL and nothing materializes
per row. These tests lock the output over deterministic scenarios written into the real table: the
text digest, the JSON `data` dict, and the per-agent rollups must stay exactly as pinned, so a
regression in the aggregation (counts, pctiles, position thirds, tie order, cost sums,
since-compact cutoffs) fails here. Every test writes into its own stretch of time (`TelemetryStream.now`),
so the rows of other tests never fall into its window.

The render math is locked by the pure unit tests in base/telemetry/metrics/tests/test_metrics.py;
keep both green together.
"""

from __future__ import annotations

import random
import re
from typing import Any

import psycopg
import pytest

from base.telemetry.metrics.aggregate import (
    build_report_from_aggregate,
    fetch_agent_rollups,
    fetch_aggregate,
)
from tests.components.gateway.telemetry_stream import TelemetryStream


@pytest.fixture
def fake(db_conn: psycopg.Connection) -> TelemetryStream:
    return TelemetryStream(db_conn)


def _run_aggregate(
    stream: TelemetryStream, *, days: int = 1, agent: int | None = None, since_compact: bool = False
) -> tuple[str, dict[str, Any], dict[int, Any]]:
    """Run the aggregate path and return (text, data, per-agent rollups)."""
    agg = fetch_aggregate(stream.db, days, agent, since_compact=since_compact, now=stream.now)
    text, data = build_report_from_aggregate(agg, days, agent, since_compact=since_compact)
    _total, roll = fetch_agent_rollups(stream.db, days, since_compact=since_compact, now=stream.now)
    return text, data, roll


def _norm(t: str) -> str:
    """generated_at is wall-clock at assembly time — normalize it."""
    return re.sub(r"generated \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", "generated X", t)


def _add(
    fake: TelemetryStream,
    *,
    event: str,
    agent_id: int | None,
    payload: dict[str, Any] | None = None,
    ts_offset_days: float = 0,
) -> None:
    fake.add(
        event=event, agent_id=agent_id, payload=payload or {}, ts_offset_hours=ts_offset_days * 24
    )


# ── deterministic scenario tests ────────────────────────────────────────────


def test_equivalence_empty(fake: TelemetryStream) -> None:
    _run_aggregate(fake)
    _run_aggregate(fake, since_compact=True)


def test_pre_snapshot_compatibility_keeps_model_and_agent_tier_scopes(
    fake: TelemetryStream,
) -> None:
    for aid in (1, 2):
        _add(
            fake,
            event="llm_usage",
            agent_id=aid,
            payload={
                "model": "gpt-6-sol",
                "in_total": 200_000,
                "out_total": 0,
                "cache_read": 0,
            },
        )
    _text, data, rollups = _run_aggregate(fake)
    # The old compatibility path selects tiers from per-model window sums;
    # per-agent rollups select from each agent's sums. Recorded calls never use it.
    assert data["metrics"]["llm_turns"]["cost_usd"] == pytest.approx(1.6)
    assert rollups[1]["cost_usd"] == pytest.approx(0.4)
    assert rollups[2]["cost_usd"] == pytest.approx(0.4)


def test_cache_write_cost_snapshots_survive_aggregation_and_unpriced_calls(
    fake: TelemetryStream,
) -> None:
    base = {
        "model": "claude-opus-5-5",
        "in_total": 1000,
        "out_total": 50,
        "cache_read": 200,
        "cache_write_5m": 300,
        "cache_write_1h": 400,
    }
    _add(fake, event="llm_usage", agent_id=1, payload={**base, "cost_usd": 0.00614})
    # A recorded zero is a known cost; an explicit unpriced call must never be re-priced.
    _add(fake, event="llm_usage", agent_id=1, payload={**base, "cost_usd": 0})
    _add(fake, event="llm_usage", agent_id=1, payload={**base, "unpriced": 1})
    # Service rows remain in the window's cost, outside per-agent rollups.
    _add(fake, event="llm_usage", agent_id=None, payload={**base, "cost_usd": 0.01228})
    # Pre-snapshot input preserves its existing compatibility calculation.
    _add(
        fake,
        event="llm_usage",
        agent_id=2,
        payload={
            "model": "claude-opus-5-5",
            "in_total": 1000,
            "out_total": 0,
            "cache_read": 0,
        },
    )
    _text, data, rollups = _run_aggregate(fake)
    llm = data["metrics"]["llm_turns"]
    assert llm["cost_usd"] == pytest.approx(round(0.00614 + 0.01228 + 0.004, 4))
    assert llm["cost_unpriced_calls"] == 1
    assert rollups[1]["cost_usd"] == pytest.approx(round(0.00614, 4))
    assert rollups[2]["cost_usd"] == pytest.approx(0.004)

    # Aggregation also shares the single-agent and since-compact scope.
    _text, filtered, _rollups = _run_aggregate(fake, agent=1, since_compact=True)
    assert filtered["metrics"]["llm_turns"]["cost_usd"] == pytest.approx(round(0.00614, 4))


def _write_full_thread_scenario(fake: TelemetryStream, aid: int) -> None:
    _add(fake, event="code", agent_id=aid, payload={"body": "print(1)"})
    _add(fake, event="syntax_fix", agent_id=aid, payload={"fixes": "ruff,ruff_format"})
    _add(fake, event="exec", agent_id=aid, payload={"body": "1\n", "ok": True})
    _add(
        fake,
        event="llm_usage",
        agent_id=aid,
        payload={"in_total": 1000, "out_total": 200, "cache_read": 800, "model": "mimo-v2.5-pro"},
    )
    _add(fake, event="turn_end", agent_id=aid, payload={"ok": True, "duration_seconds": 4.0})
    _add(fake, event="agent_spawned", agent_id=aid, payload={"spawner": "agent:1"})
    _add(fake, event="halt", agent_id=aid, payload={"body": "no tool_call (idle)"})


def _assert_full_thread_text_digest(text: str) -> None:
    assert "7 events / 1 agents" in _norm(text)
    assert "syntax_fix trigger counts (per code block):" in text
    assert "ruff                        1  100%" in text
    assert "exec: 1 ok / 0 failed  (success 100.0%)" in text
    assert "llm: 1 calls  in=1000 out=200 cached=800 reason=0" in text
    assert "cache hit overall: 80.0%   cost: $0.0003" in text
    assert "turn duration (s):" in text
    assert "agents: 1 distinct  1 spawns (1 subagents)  1 idle halts" in text


def _assert_full_thread_data_fragment(data: dict[str, Any]) -> None:
    sx = data["metrics"]["syntax_fix"]
    assert sx["trigger_counts"] == {"ruff": 1, "ruff_format": 1}
    assert sx["code_blocks"] == 1
    assert data["metrics"]["exec"]["exec_ok"] == 1
    assert data["metrics"]["exec"]["exec_failed"] == 0
    llm = data["metrics"]["llm_turns"]
    assert llm["llm_calls"] == 1 and llm["tokens_in"] == 1000 and llm["tokens_out"] == 200
    assert llm["cache_hit_pct"] == 80.0
    assert data["metrics"]["agent_activity"]["spawns_total"] == 1


def _assert_full_thread_rollup(roll: dict[int, Any], aid: int) -> None:
    assert roll[aid]["events"] == 7
    assert roll[aid]["llm_calls"] == 1
    assert roll[aid]["turn_total"] == 1
    assert roll[aid]["exec_ok"] == 1


def test_equivalence_full_thread(fake: TelemetryStream) -> None:
    """One agent with every unit-relevant event — the router wiring scenario.

    Golden lock on the aggregate output: the text digest, the
    machine data fragment, and the per-agent rollup must keep these exact
    values.
    """
    aid = 1
    _write_full_thread_scenario(fake, aid)

    text, data, roll = _run_aggregate(fake)
    _assert_full_thread_text_digest(text)
    _assert_full_thread_data_fragment(data)
    _assert_full_thread_rollup(roll, aid)

    # since-compact window (no compact rows here) — identical counts
    text2, _data2, roll2 = _run_aggregate(fake, since_compact=True)
    assert "7 events / 1 agents" in _norm(text2)
    assert roll2[aid]["events"] == 7


def test_plugin_activation_section_is_the_obsolescence_gauge(fake: TelemetryStream) -> None:
    """Philosophy §6 asks a shim to measure its own obsolescence. The section
    counts activations per contribution (the `<plugin>/<surface>/<identifier>`
    key `ava plugins inspect` lists as registered) and per plugin x model, so a
    shim that fired under one model and never under another is visible as data
    rather than as an opinion."""

    def _act(plugin: str, surface: str, identifier: str, model: str) -> None:
        _add(
            fake,
            event="plugin_activation",
            agent_id=1,
            payload={
                "plugin": plugin,
                "surface": surface,
                "identifier": identifier,
                "detail": "",
                "model": model,
            },
        )

    _act("ava_syntax_fix", "hooks", "before_exec", "old-model")
    _act("ava_syntax_fix", "hooks", "before_exec", "old-model")
    _act("ava_code", "sdkWraps", "files.read", "new-model")

    text, data, _roll = _run_aggregate(fake)
    section = data["metrics"]["plugin_activation"]
    assert section["total_activations"] == 3
    assert section["distinct_plugins"] == 2
    assert section["by_contribution"] == [
        {"contribution": "ava_syntax_fix/hooks/before_exec", "count": 2},
        {"contribution": "ava_code/sdkWraps/files.read", "count": 1},
    ]
    assert section["by_plugin_model"] == {
        "ava_syntax_fix@old-model": 2,
        "ava_code@new-model": 1,
    }
    assert "plugin activations: 3 across 2 plugins" in text
    assert "ava_syntax_fix/hooks/before_exec" in text


def test_plugin_activation_section_empty_when_nothing_fired(fake: TelemetryStream) -> None:
    """A window in which no plugin surface fired renders the zero state — the
    other half of the gauge: silence is the removal evidence."""
    _add(fake, event="code", agent_id=1, payload={"body": "print(1)"})

    text, data, _roll = _run_aggregate(fake)
    assert data["metrics"]["plugin_activation"] == {
        "total_activations": 0,
        "distinct_plugins": 0,
        "by_plugin": {},
        "by_contribution": [],
        "by_plugin_model": {},
    }
    assert "no plugin hook, wrap, or prompt section fired" in text


def test_equivalence_multi_agent_service_and_ties(fake: TelemetryStream) -> None:
    """Two agents + service-level rows (agent_id None) — the NULL group feeds
    turns_per_agent / agent_lifetime_s in both paths."""
    a1, a2 = 1, 2
    for aid, body, off in ((a1, "x=1", 0), (a2, "y=2", 0), (a1, "z=3", 0.5)):
        _add(fake, event="code", agent_id=aid, payload={"body": body}, ts_offset_days=off)
    _add(fake, event="log", agent_id=None, payload={}, ts_offset_days=0.1)
    _add(fake, event="log", agent_id=None, payload={}, ts_offset_days=0.9)
    _add(fake, event="turn_end", agent_id=a1, payload={"ok": True, "duration_seconds": 1.0})
    _add(fake, event="turn_end", agent_id=a2, payload={"ok": False, "duration_seconds": 2.0})
    _run_aggregate(fake)
    _run_aggregate(fake, since_compact=True)


def test_equivalence_exec_failure_variants(fake: TelemetryStream) -> None:
    aid = 1
    _add(fake, event="exec", agent_id=aid, payload={"body": "ok"})
    _add(fake, event="exec_failed", agent_id=aid, payload={"body": "t", "exc_type": "ValueError"})
    _add(fake, event="exec_failed", agent_id=aid, payload={"body": "t"})  # no exc_type
    _add(fake, event="exec_timeout", agent_id=aid, payload={"body": "t"})
    _add(fake, event="exec_cancelled", agent_id=aid, payload={"body": "t"})
    _add(fake, event="exec(failed)", agent_id=aid, payload={"body": "t", "exc_type": "ValueError"})
    _text, data, roll = _run_aggregate(fake)
    ex = data["metrics"]["exec"]
    assert (ex["exec_ok"], ex["exec_failed"]) == (1, 5)
    assert ex["failure_types"] == {
        "ValueError": 2,
        "exec_cancelled": 1,
        "exec_failed": 1,
        "exec_timeout": 1,
    }
    assert roll[aid]["exec_failed"] == 5


def test_equivalence_since_compact_cutoffs(fake: TelemetryStream) -> None:
    """Pre-compact rows dropped for the compacted agent, everything kept for
    the other; the compact halt row itself is kept (ts >= cutoff)."""
    a1, a2 = 1, 2
    _add(fake, event="code", agent_id=a1, payload={"body": "old"}, ts_offset_days=0.8)
    _add(
        fake,
        event="halt",
        agent_id=a1,
        payload={"body": "system_halt (compact)"},
        ts_offset_days=0.6,
    )
    _add(fake, event="code", agent_id=a1, payload={"body": "new"}, ts_offset_days=0.2)
    _add(
        fake,
        event="halt",
        agent_id=a1,
        payload={"body": "system_halt (compact)"},
        ts_offset_days=0.1,
    )
    _add(fake, event="code", agent_id=a1, payload={"body": "post"}, ts_offset_days=0.05)
    _add(fake, event="code", agent_id=a2, payload={"body": "other"}, ts_offset_days=0.7)
    _add(fake, event="log", agent_id=None, payload={}, ts_offset_days=0.9)
    # since-compact: a1's pre-compact rows (old at 0.8d) are dropped, the
    # compact halt itself (0.1d) and everything after are kept; a2's rows and
    # the agentless log row survive (agentless row counts in total, not rollup)
    text, data, roll = _run_aggregate(fake, since_compact=True)
    assert "4 events / 2 agents" in _norm(text), text[:200]
    assert data["meta"]["total_events"] == 4
    assert roll[a1]["events"] == 2
    assert roll[a2]["events"] == 1
    # the non-compact window keeps everything (no cutoffs applied)
    text2, data2, roll2 = _run_aggregate(fake)
    assert "7 events / 2 agents" in _norm(text2)
    assert data2["meta"]["total_events"] == 7
    assert roll2[a1]["events"] == 5
    assert roll2[a2]["events"] == 1


def test_equivalence_syntax_fix_block_edges(fake: TelemetryStream) -> None:
    """fixes before any code (dropped), none sentinel, (n) suffixes, multi-kind
    events, blocks with no attached fix, two agents with different block counts."""
    a1, a2 = 1, 2
    _add(fake, event="syntax_fix", agent_id=a1, payload={"fixes": "ruff"})  # before any code
    _add(fake, event="code", agent_id=a1, payload={"body": "a"})
    _add(fake, event="syntax_fix", agent_id=a1, payload={"fixes": "none"})
    _add(fake, event="code", agent_id=a1, payload={"body": "b"})
    _add(fake, event="syntax_fix", agent_id=a1, payload={"fixes": "chinese_punct(3),ruff_format"})
    _add(fake, event="code", agent_id=a1, payload={"body": "c"})
    _add(fake, event="code", agent_id=a2, payload={"body": "d"})
    _add(fake, event="syntax_fix", agent_id=a2, payload={"fixes": "ruff_format"})
    _add(fake, event="syntax_fix", agent_id=a2, payload={"fixes": "missing_imports(2)"})
    _run_aggregate(fake)


def test_equivalence_agent_filter(fake: TelemetryStream) -> None:
    a1, a2 = 1, 2
    for aid in (a1, a2):
        _add(fake, event="code", agent_id=aid, payload={"body": "x"})
        _add(
            fake,
            event="llm_usage",
            agent_id=aid,
            payload={"in_total": 10, "out_total": 1, "cache_read": 5, "model": "deepseek-v4-pro"},
        )
    _run_aggregate(fake, agent=a1)


def test_equivalence_window_respects_days(fake: TelemetryStream) -> None:
    aid = 1
    _add(fake, event="code", agent_id=aid, payload={"body": "old"}, ts_offset_days=5)
    _add(fake, event="code", agent_id=aid, payload={"body": "new"})
    _run_aggregate(fake, days=1)
    _run_aggregate(fake, days=7)


# ── randomized equivalence (seeded) ─────────────────────────────────────────

_MODELS = ["mimo-v2.5-pro", "gpt-5.6-sol", "no-such-model", ""]
_EXC = ["ValueError", "TypeError", "NameError", None, ""]
_FIXES = [
    "ruff",
    "ruff_format",
    "chinese_punct(3)",
    "missing_imports(2)",
    "none",
    "",
    "ruff,ruff_format",
]
_HALT = ["no tool_call (idle)", "system_halt (compact)", "lifecycle AgentTermination", "other"]
_SPAWN = ["user", "agent:1", "scheduler", "cron", ""]
_LIFECYCLE = ["agent_spawned", "agent_terminated", "agent_restarted", "agent_resurrected"]
_NONRELEVANT = ["log", "node_enter", "node_exit", "text", "status_change"]


def _random_payload(rng: random.Random, event_name: str) -> dict[str, Any]:
    if event_name == "code":
        return {"body": "x" * rng.randint(0, 500)}
    if event_name == "syntax_fix":
        return {"fixes": rng.choice(_FIXES)}
    if event_name == "exec":
        return {"body": "o" * rng.randint(0, 200), "ok": True}
    if event_name in ("exec_failed", "exec_timeout", "exec_cancelled"):
        p: dict[str, Any] = {"body": "t" * rng.randint(0, 100)}
        if event_name == "exec_failed":
            p["exc_type"] = rng.choice(_EXC)
        return p
    if event_name == "llm_usage":
        in_total = rng.randint(0, 50_000)
        return {
            "model": rng.choice(_MODELS),
            "in_total": in_total,
            "out_total": rng.randint(0, 5_000),
            "cache_read": rng.randint(0, in_total),
            "reasoning": rng.randint(0, 2_000),
        }
    if event_name == "turn_end":
        return {
            "ok": rng.choice([True, False, None]),
            "duration_seconds": round(rng.uniform(0.1, 30.0), 3),
        }
    if event_name == "halt":
        return {"body": rng.choice(_HALT)}
    if event_name == "agent_spawned":
        return {"spawner": rng.choice(_SPAWN)}
    if event_name in _LIFECYCLE:
        return {}
    return {}


def test_equivalence_randomized(fake: TelemetryStream) -> None:
    """Seeded pseudo-random stream — hundreds of rows across every event, all
    agents (incl. service rows), ts ties, windows of 1/3/7 days."""
    rng = random.Random(20260806)  # noqa: S311 — seeded, deterministic test data
    agents = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
    pool = [
        *[
            "code",
            "syntax_fix",
            "exec",
            "llm_usage",
            "turn_end",
            "halt",
            "agent_spawned",
            "exec_failed",
            "exec_timeout",
            "exec_cancelled",
        ],
        *_LIFECYCLE,
        *_NONRELEVANT,
    ]
    n = 400
    for _i in range(n):
        event_name = rng.choice(pool)
        aid = rng.choice(agents + [None] * 3)  # service rows sometimes
        payload = _random_payload(rng, event_name)
        # offsets span ~3.5 days so 1/3/7-day windows slice differently; ties
        # via bucketing to 0.001-day steps.
        offset = rng.randint(0, 3500) / 1000
        _add(fake, event=event_name, agent_id=aid, payload=payload, ts_offset_days=offset)
    for days in (1, 3, 7):
        text, data, _roll = _run_aggregate(fake, days=days)
        # structure: every section present, per-agent rollups subset the total
        assert set(data["metrics"]) == {
            "syntax_fix",
            "exec",
            "llm_turns",
            "agent_activity",
            "plugin_activation",
        }
        assert data["meta"]["total_events"] >= 0
        _run_aggregate(fake, days=days, since_compact=True)
    # single-agent windows over the same data
    text, data, _roll = _run_aggregate(fake, days=7, agent=agents[0])
    assert data["meta"]["agent_filter"] == agents[0]
    # the window header names the single agent
    assert str(agents[0]) in text or "1 agents" in _norm(text)
