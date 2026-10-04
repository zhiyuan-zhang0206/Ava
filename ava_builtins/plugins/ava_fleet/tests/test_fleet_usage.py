"""Hermetic unit tests for the ava_fleet budget meter
(`ava_builtins/plugins/ava_fleet/skills/ava-fleet/reference/usage.py`).

These lock the *pure* folding aggregation that a budget watcher reads: that
every (agent, model) group contributes its summed cost snapshot, that a group
without costed calls contributes 0 cost but keeps its calls in
`unpriced_calls`, that per-agent costs sum to the total, and that the window
resolver refuses two windows at once; and, against the real tables, that the
windowed read sums the `llm_usage` rows of `telemetry_events` and the
whole-life read joins the ledger to the event tail from its watermark.
"""

from __future__ import annotations

import importlib.util
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from services.upkeep.events_maintenance import token_totals

_PATH = (
    Path(__file__).resolve().parents[4]
    / "ava_builtins"
    / "plugins"
    / "ava_fleet"  # the PLUGIN dir stays a Python package
    / "skills"
    / "ava-fleet"
    / "reference"
    / "usage.py"
)
_spec = importlib.util.spec_from_file_location("fleet_usage_under_test", _PATH)
assert _spec and _spec.loader
usage = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(usage)


Row = tuple[int, str, int, int, int, int, int, float, int]


def _row(
    agent_id: int,
    model: str,
    tin: int,
    tout: int,
    cached: int,
    reason: int,
    calls: int,
    cost: float,
    unpriced: int,
) -> Row:
    """One grouped row: agent_id, model, in_total, out_total, cache_read,
    reasoning, calls, summed cost snapshots, unpriced calls — the shape
    `_rows` fetches (ledger + events) and `aggregate` folds."""
    return (agent_id, model, tin, tout, cached, reason, calls, cost, unpriced)


def test_single_group_folds_cost_snapshot() -> None:
    rows = [_row(1464, "claude-fable-5", 1_000_000, 500_000, 200_000, 10_000, 7, 1.2345, 0)]
    out = usage.aggregate(rows)
    agent = out["per_agent"]["1464"]
    assert agent["cost_usd"] == 1.2345
    assert agent["llm_calls"] == 7
    assert agent["tokens_reasoning"] == 10_000
    assert agent["unpriced_calls"] == 0
    assert out["total"]["cost_usd"] == 1.2345
    assert out["total"]["distinct_agents"] == 1


def test_unpriced_group_zero_cost_but_counted() -> None:
    rows = [_row(1, "no-such-model-x", 5_000, 5_000, 0, 0, 3, 0.0, 3)]
    out = usage.aggregate(rows)
    agent = out["per_agent"]["1"]
    assert agent["cost_usd"] == 0.0
    assert agent["unpriced_calls"] == 3
    assert agent["llm_calls"] == 3
    assert agent["by_model"]["no-such-model-x"]["cost_usd"] is None


def test_per_agent_costs_sum_to_total() -> None:
    rows = [
        _row(1, "claude-fable-5", 800_000, 400_000, 100_000, 0, 4, 0.5, 0),
        _row(1, "claude-haiku-4-5-20251001", 2_000_000, 300_000, 500_000, 0, 9, 0.25, 0),
        _row(2, "claude-sonnet-5", 1_200_000, 600_000, 300_000, 5_000, 6, 0.75, 1),
    ]
    out = usage.aggregate(rows)
    a1 = out["per_agent"]["1"]["cost_usd"]
    a2 = out["per_agent"]["2"]["cost_usd"]
    assert out["total"]["distinct_agents"] == 2
    assert out["total"]["cost_usd"] == round(a1 + a2, 4)
    assert a1 == 0.75
    assert out["per_agent"]["1"]["llm_calls"] == 13
    assert out["per_agent"]["2"]["unpriced_calls"] == 1


def test_window_bounds_rejects_two_windows() -> None:
    with pytest.raises(ValueError, match="at most one"):
        usage._window_bounds(datetime(2026, 7, 22, tzinfo=UTC), 3.0)


def test_window_bounds_variants() -> None:
    since = datetime(2026, 7, 22, 18, tzinfo=UTC)  # time-bomb-ok: explicit input, passed through
    assert usage._window_bounds(None, None) == (None, None)
    from_, to = usage._window_bounds(since, None)
    assert from_ == since and to is not None
    hours_from, hours_to = usage._window_bounds(None, 3)
    assert hours_from is not None and hours_to is not None
    assert abs((hours_to - hours_from).total_seconds() - 3 * 3600) <= 2


def _agent(db: psycopg.Connection) -> int:
    row = db.execute("INSERT INTO agents (label) VALUES ('usage') RETURNING id").fetchone()
    assert row is not None
    db.commit()
    return int(row[0])


def _usage(
    db: psycopg.Connection, agent_id: int, *, hours_ago: float, model: str = "m", **payload: Any
) -> None:
    db.execute(
        "INSERT INTO telemetry_events (event_uid, ts, agent_id, machine, cluster, process, "
        "category, event_name, level, source, attributes) VALUES (%s, now() - (%s * interval "
        "'1 hour'), %s, 'm', 'c', 'p', 'telemetry', 'llm_usage', 'info', 'test', %s::jsonb)",
        (
            uuid.uuid4().int % (1 << 62),
            hours_ago,
            agent_id,
            json.dumps({"model": model, **payload}),
        ),
    )
    db.commit()


def test_a_window_sums_the_llm_usage_rows_per_agent_and_model(db_conn: psycopg.Connection) -> None:
    a, b = _agent(db_conn), _agent(db_conn)
    base: dict[str, Any] = {"in_total": 100, "out_total": 50, "cache_read": 10, "reasoning": 5}
    _usage(db_conn, a, hours_ago=1, cost_usd=0.5, **base)
    _usage(db_conn, a, hours_ago=2, cost_usd="0.25", **base)
    _usage(db_conn, a, hours_ago=2, **base)  # no cost snapshot: unpriced
    _usage(db_conn, a, hours_ago=2, model="other", cost_usd=1.0, in_total=1)
    _usage(db_conn, a, hours_ago=30, cost_usd=9.0, **base)  # outside the window
    _usage(db_conn, b, hours_ago=1, cost_usd=2.0, **base)

    out = usage.aggregate(usage._rows([a], None, 3.0))

    agent = out["per_agent"][str(a)]
    assert agent["llm_calls"] == 4
    assert agent["tokens_in"] == 301
    assert agent["unpriced_calls"] == 1
    assert agent["cost_usd"] == 1.75
    assert agent["by_model"]["m"]["llm_calls"] == 3
    assert str(b) not in out["per_agent"]
    everyone = usage.aggregate(usage._rows([], None, 3.0))
    assert set(everyone["per_agent"]) == {str(a), str(b)}


def test_whole_life_is_the_ledger_plus_the_event_tail_from_its_watermark(
    db_conn: psycopg.Connection,
) -> None:
    agent = _agent(db_conn)
    yesterday = datetime.now(UTC).date() - timedelta(days=1)
    db_conn.execute(
        "INSERT INTO agent_model_tokens_daily (agent_id, day, model, llm_calls, costed_calls, "
        "unpriced_calls, tokens_in, tokens_out, cost_usd) VALUES (%s, %s, 'm', 10, 10, 0, 1000, "
        "500, 4.0)",
        (agent, yesterday),
    )
    db_conn.execute(
        "INSERT INTO agent_model_tokens_daily (agent_id, day, model, llm_calls, costed_calls, "
        "unpriced_calls, tokens_in, tokens_out, cost_usd) VALUES (%s, %s, 'm', 5, 5, 0, 100, 50, "
        "1.0)",
        (agent, yesterday - timedelta(days=3)),
    )
    # Events of the ledgered days are already in the ledger; only those after the watermark count.
    _usage(db_conn, agent, hours_ago=24 * 2, cost_usd=100.0, in_total=7000, out_total=1)
    _usage(db_conn, agent, hours_ago=0, cost_usd=0.5, in_total=10, out_total=5)  # the live tail
    db_conn.commit()

    out = usage.aggregate(usage._rows([agent], None, None))["per_agent"][str(agent)]

    assert out["llm_calls"] == 16
    assert out["tokens_in"] == 1110
    assert out["cost_usd"] == 5.5


def test_an_agent_with_no_ledger_row_reads_every_event(db_conn: psycopg.Connection) -> None:
    agent = _agent(db_conn)
    _usage(db_conn, agent, hours_ago=24 * 40, cost_usd=1.0, in_total=1)
    _usage(db_conn, agent, hours_ago=1, cost_usd=2.0, in_total=2)

    out = usage.aggregate(usage._rows([agent], None, None))["per_agent"][str(agent)]

    assert (out["llm_calls"], out["cost_usd"]) == (2, 3.0)


def test_whole_life_is_the_same_before_and_after_the_ledger_is_folded(
    db_conn: psycopg.Connection,
) -> None:
    agent = _agent(db_conn)
    today = datetime.now(UTC).date()
    for age_days, calls, cost in ((40, 7, 3.0), (20, 5, 2.0), (3, 10, 4.0)):
        db_conn.execute(
            "INSERT INTO agent_model_tokens_daily (agent_id, day, model, llm_calls, costed_calls, "
            "unpriced_calls, tokens_in, tokens_out, cost_usd) VALUES (%s, %s, 'm', %s, %s, 0, "
            "%s, 1, %s)",
            (agent, today - timedelta(days=age_days), calls, calls, calls * 100, cost),
        )
    _usage(db_conn, agent, hours_ago=0, cost_usd=0.5, in_total=10, out_total=5)  # the live tail
    db_conn.commit()

    before = usage.aggregate(usage._rows([agent], None, None))["per_agent"][str(agent)]
    token_totals.fold_totals(db_conn, today=today)
    db_conn.commit()
    after = usage.aggregate(usage._rows([agent], None, None))["per_agent"][str(agent)]

    assert token_totals.folded_through(db_conn) >= today - timedelta(days=20)  # two days folded
    assert (before["llm_calls"], before["tokens_in"], before["cost_usd"]) == (23, 2210, 9.5)
    assert after == before
