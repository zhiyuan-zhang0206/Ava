"""Loki-aggregate metrics path — golden output lock (task #1197 A3).

`shared.metrics_aggregate.fetch_aggregate` + `build_report_from_aggregate` +
`agent_rollups_from_aggregate` are the ONLY metrics path after the /api/metrics
RSS fix (the per-row reference implementation was retired with the SQL
aggregation — single-day materialization outgrew 430K+ rows). These tests lock
the aggregate path's output over deterministic scenarios against the in-memory
`FakeLoki` backend (same filter/window semantics as `gateway.loki_events`):
the text digest, the JSON `data` dict, and the per-agent rollups must stay
exactly as pinned, so a regression in the Loki aggregation (counts, pctiles,
position thirds, tie order, cost sums, since-compact cutoffs) fails here.

The render math is locked by the pure unit tests in tests/shared/test_metrics.py;
keep both green together.
"""

from __future__ import annotations

import random
from typing import Any

from shared.metrics_aggregate import (
    agent_rollups_from_aggregate,
    build_report_from_aggregate,
    fetch_aggregate,
)
from tests.gateway.loki_fake import FakeLoki


def _run_aggregate(
    fake: FakeLoki, *, days: int = 1, agent: int | None = None, since_compact: bool = False
) -> tuple[str, dict[str, Any], dict[int, Any]]:
    """Run the Loki aggregate path and return (text, data, per-agent rollups)."""
    agg = fetch_aggregate(days, agent, since_compact=since_compact, loki=fake)
    text, data = build_report_from_aggregate(agg, days, agent, since_compact=since_compact)
    roll = agent_rollups_from_aggregate(agg)
    return text, data, roll


def _norm(t: str) -> str:
    """generated_at is wall-clock at assembly time — normalize it."""
    import re as _re

    return _re.sub(r"generated \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", "generated X", t)


def _add(
    fake: FakeLoki,
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


def test_equivalence_empty() -> None:
    fake = FakeLoki()
    _run_aggregate(fake)
    _run_aggregate(fake, since_compact=True)


def test_equivalence_full_thread() -> None:
    """One agent with every unit-relevant event — the router wiring scenario.

    Golden lock on the Loki aggregate path's output: the text digest, the
    machine data fragment, and the per-agent rollup must keep these exact
    values.
    """
    fake = FakeLoki()
    aid = 1
    _add(fake, event="code", agent_id=aid, payload={"body": "print(1)"})
    _add(fake, event="syntax_fix", agent_id=aid, payload={"fixes": "ruff,ruff_format"})
    _add(fake, event="exec", agent_id=aid, payload={"body": "1\n", "ok": True})
    _add(
        fake,
        event="llm_usage",
        agent_id=aid,
        payload={"in_total": 1000, "out_total": 200, "cache_read": 800, "model": "deepseek-v4-pro"},
    )
    _add(fake, event="turn_end", agent_id=aid, payload={"ok": True, "duration_seconds": 4.0})
    _add(fake, event="agent_spawned", agent_id=aid, payload={"spawner": "agent:1"})
    _add(fake, event="halt", agent_id=aid, payload={"body": "no tool_call (idle)"})
    _add(fake, event="sdk_call", agent_id=aid, payload={"fn": "files.read"})

    text, data, roll = _run_aggregate(fake)
    # ── text digest ──
    assert "8 events / 1 agents" in _norm(text)
    assert "syntax_fix trigger counts (per code block):" in text
    assert "ruff                        1  100%" in text
    assert "exec: 1 ok / 0 failed  (success 100.0%)" in text
    assert "llm: 1 calls  in=1000 out=200 cached=800 reason=0" in text
    assert "cache hit overall: 80.0%   cost: $0.0003" in text
    assert "turn duration (s):" in text
    assert "agents: 1 distinct  1 spawns (1 subagents)  1 idle halts" in text
    assert "sdk calls: 1 in 1 code blocks  (1 distinct functions)" in text
    # ── data fragment ──
    sx = data["metrics"]["syntax_fix"]
    assert sx["trigger_counts"] == {"ruff": 1, "ruff_format": 1}
    assert sx["code_blocks"] == 1
    assert data["metrics"]["exec"]["exec_ok"] == 1
    assert data["metrics"]["exec"]["exec_failed"] == 0
    llm = data["metrics"]["llm_turns"]
    assert llm["llm_calls"] == 1 and llm["tokens_in"] == 1000 and llm["tokens_out"] == 200
    assert llm["cache_hit_pct"] == 80.0
    assert data["metrics"]["agent_activity"]["spawns_total"] == 1
    assert data["metrics"]["sdk_usage"]["total_calls"] == 1
    # ── per-agent rollups ──
    assert roll[aid]["events"] == 8
    assert roll[aid]["llm_calls"] == 1
    assert roll[aid]["turn_total"] == 1
    assert roll[aid]["exec_ok"] == 1

    # since-compact window (no compact rows here) — identical counts
    text2, _data2, roll2 = _run_aggregate(fake, since_compact=True)
    assert "8 events / 1 agents" in _norm(text2)
    assert roll2[aid]["events"] == 8


def test_equivalence_multi_agent_service_and_ties() -> None:
    """Two agents + service-level rows (agent_id None) — the NULL group feeds
    turns_per_agent / agent_lifetime_s in both paths."""
    fake = FakeLoki()
    a1, a2 = 1, 2
    for aid, body, off in ((a1, "x=1", 0), (a2, "y=2", 0), (a1, "z=3", 0.5)):
        _add(fake, event="code", agent_id=aid, payload={"body": body}, ts_offset_days=off)
    _add(fake, event="log", agent_id=None, payload={}, ts_offset_days=0.1)
    _add(fake, event="log", agent_id=None, payload={}, ts_offset_days=0.9)
    _add(fake, event="turn_end", agent_id=a1, payload={"ok": True, "duration_seconds": 1.0})
    _add(fake, event="turn_end", agent_id=a2, payload={"ok": False, "duration_seconds": 2.0})
    _run_aggregate(fake)
    _run_aggregate(fake, since_compact=True)


def test_equivalence_exec_failure_variants() -> None:
    fake = FakeLoki()
    aid = 1
    _add(fake, event="exec", agent_id=aid, payload={"body": "ok"})
    _add(fake, event="exec_failed", agent_id=aid, payload={"body": "t", "exc_type": "ValueError"})
    _add(fake, event="exec_failed", agent_id=aid, payload={"body": "t"})  # no exc_type
    _add(fake, event="exec_timeout", agent_id=aid, payload={"body": "t"})
    _add(fake, event="exec_thread_stuck", agent_id=aid, payload={"body": "t"})
    _add(fake, event="exec_cancelled", agent_id=aid, payload={"body": "t"})
    _run_aggregate(fake)


def test_equivalence_since_compact_cutoffs() -> None:
    """Pre-compact rows dropped for the compacted agent, everything kept for
    the other; the compact halt row itself is kept (ts >= cutoff)."""
    fake = FakeLoki()
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


def test_equivalence_sdk_ties_and_namespaces() -> None:
    """Equal call counts — ties resolve deterministically (count desc, fn asc)."""
    fake = FakeLoki()
    aid = 1
    fns = ["files.read", "shell.run", "files.read", "shell.run", "agents.spawn", "files.write"]
    for i, fn in enumerate(fns):
        _add(fake, event="sdk_call", agent_id=aid, payload={"fn": fn}, ts_offset_days=i / 100)
    _run_aggregate(fake)


def test_equivalence_syntax_fix_block_edges() -> None:
    """fixes before any code (dropped), none sentinel, (n) suffixes, multi-kind
    events, blocks with no attached fix, two agents with different block counts."""
    fake = FakeLoki()
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


def test_equivalence_agent_filter() -> None:
    fake = FakeLoki()
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


def test_equivalence_window_respects_days() -> None:
    fake = FakeLoki()
    aid = 1
    _add(fake, event="code", agent_id=aid, payload={"body": "old"}, ts_offset_days=5)
    _add(fake, event="code", agent_id=aid, payload={"body": "new"})
    _run_aggregate(fake, days=1)
    _run_aggregate(fake, days=7)


# ── randomized equivalence (seeded) ─────────────────────────────────────────

_MODELS = ["deepseek-v4-pro", "deepseek-v4-flash", "no-such-model", ""]
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
_SDK = ["files.read", "files.write", "shell.run", "agents.spawn", "agents.send_message", ""]
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
    if event_name in ("exec_failed", "exec_timeout", "exec_thread_stuck", "exec_cancelled"):
        p: dict[str, Any] = {"body": "t" * rng.randint(0, 100)}
        if event_name == "exec_failed":
            p["exc_type"] = rng.choice(_EXC)
        return p
    if event_name == "llm_usage":
        return {
            "model": rng.choice(_MODELS),
            "in_total": rng.randint(0, 50_000),
            "out_total": rng.randint(0, 5_000),
            "cache_read": rng.randint(0, 50_000),
            "reasoning": rng.randint(0, 2_000),
        }
    if event_name == "turn_end":
        return {
            "ok": rng.choice([True, False, None]),
            "duration_seconds": round(rng.uniform(0.1, 30.0), 3),
        }
    if event_name == "sdk_call":
        return {"fn": rng.choice(_SDK)}
    if event_name == "halt":
        return {"body": rng.choice(_HALT)}
    if event_name == "agent_spawned":
        return {"spawner": rng.choice(_SPAWN)}
    if event_name in _LIFECYCLE:
        return {}
    return {}


def test_equivalence_randomized() -> None:
    """Seeded pseudo-random stream — hundreds of rows across every event, all
    agents (incl. service rows), ts ties, windows of 1/3/7 days."""
    rng = random.Random(20260806)  # noqa: S311 — seeded, deterministic test data
    fake = FakeLoki()
    agents = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
    pool = [
        *[
            "code",
            "syntax_fix",
            "exec",
            "llm_usage",
            "turn_end",
            "sdk_call",
            "halt",
            "agent_spawned",
            "exec_failed",
            "exec_timeout",
            "exec_thread_stuck",
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
            "sdk_usage",
        }
        assert data["meta"]["total_events"] >= 0
        _run_aggregate(fake, days=days, since_compact=True)
    # single-agent windows over the same data
    text, data, _roll = _run_aggregate(fake, days=7, agent=agents[0])
    assert data["meta"]["agent_filter"] == agents[0]
    # the window header names the single agent
    assert str(agents[0]) in text or "1 agents" in _norm(text)
