"""Hermetic unit tests for the ava_fleet budget meter
(`ava_builtins/plugins/ava_fleet/skills/ava-fleet/reference/usage.py`).

The live DB scan is exercised out of band (the query mirrors
`gateway/routers/agent_inspect._agent_cost`, which the dashboard tests already
cover). These lock the *pure* pricing aggregation that a budget watcher reads:
that every (agent, model) group is priced through the single `cost_usd` source,
that an unpriced model contributes 0 cost but keeps its calls in
`unpriced_calls`, that per-agent costs sum to the total, and that the window
clause refuses two windows at once.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest

from shared.lm.pricing import cost_usd

_PATH = (
    Path(__file__).resolve().parents[2]
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


def _row(agent_id, model, tin, tout, cached, reason, calls):
    """One grouped scan row: agent_id, model, in_total, out_total, cache_read,
    reasoning, calls — the shape `_rows` fetches and `aggregate` prices."""
    return (agent_id, model, tin, tout, cached, reason, calls)


def _priced(model: str, tin: int, tout: int, cached: int) -> float:
    """cost_usd for a model known to be in the pricing table (never None here)."""
    price = cost_usd(model, tin, tout, cached)
    assert price is not None
    return price


def test_single_group_prices_via_cost_usd() -> None:
    rows = [_row(1464, "claude-fable-5", 1_000_000, 500_000, 200_000, 10_000, 7)]
    out = usage.aggregate(rows)
    expected = round(_priced("claude-fable-5", 1_000_000, 500_000, 200_000), 4)
    agent = out["per_agent"]["1464"]
    assert agent["cost_usd"] == expected
    assert agent["llm_calls"] == 7
    assert agent["tokens_reasoning"] == 10_000
    assert agent["unpriced_calls"] == 0
    assert out["total"]["cost_usd"] == expected
    assert out["total"]["distinct_agents"] == 1


def test_unpriced_model_zero_cost_but_counted() -> None:
    rows = [_row(1, "no-such-model-x", 5_000, 5_000, 0, 0, 3)]
    out = usage.aggregate(rows)
    agent = out["per_agent"]["1"]
    assert agent["cost_usd"] == 0.0
    assert agent["unpriced_calls"] == 3
    assert agent["llm_calls"] == 3
    assert agent["by_model"]["no-such-model-x"]["cost_usd"] is None


def test_per_agent_costs_sum_to_total() -> None:
    rows = [
        _row(1, "claude-fable-5", 800_000, 400_000, 100_000, 0, 4),
        _row(1, "claude-haiku-4-5-20251001", 2_000_000, 300_000, 500_000, 0, 9),
        _row(2, "claude-sonnet-5", 1_200_000, 600_000, 300_000, 5_000, 6),
    ]
    out = usage.aggregate(rows)
    a1 = out["per_agent"]["1"]["cost_usd"]
    a2 = out["per_agent"]["2"]["cost_usd"]
    assert out["total"]["distinct_agents"] == 2
    assert out["total"]["cost_usd"] == round(a1 + a2, 4)
    # agent 1 folds two model groups into one agent cost
    fable = _priced("claude-fable-5", 800_000, 400_000, 100_000)
    haiku = _priced("claude-haiku-4-5-20251001", 2_000_000, 300_000, 500_000)
    assert a1 == round(fable + haiku, 4)
    assert out["per_agent"]["1"]["llm_calls"] == 13


def test_window_clause_rejects_two_windows() -> None:
    with pytest.raises(ValueError, match="at most one"):
        usage._window_clause(datetime(2026, 7, 22, tzinfo=UTC), 3.0)


def test_window_clause_variants() -> None:
    empty_sql, empty_params = usage._window_clause(None, None)
    assert empty_sql == "" and empty_params == []
    since_sql, since_params = usage._window_clause(datetime(2026, 7, 22, 18, tzinfo=UTC), None)
    assert "ts >" in since_sql and len(since_params) == 1
    hours_sql, hours_params = usage._window_clause(None, 3)
    assert "interval" in hours_sql and hours_params == [3.0]
