"""Hermetic unit tests for the ava_fleet budget meter
(`ava_builtins/plugins/ava_fleet/skills/ava-fleet/reference/usage.py`).

The live Loki + ledger read is exercised out of band (it mirrors
`gateway/routers/_agent_cost.py`, which the dashboard tests already cover).
These lock the *pure* folding aggregation that a budget watcher reads: that
every (agent, model) group contributes its summed cost snapshot, that a group
without costed calls contributes 0 cost but keeps its calls in
`unpriced_calls`, that per-agent costs sum to the total, and that the window
resolver refuses two windows at once.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest

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


def _row(agent_id, model, tin, tout, cached, reason, calls, cost, unpriced):
    """One grouped row: agent_id, model, in_total, out_total, cache_read,
    reasoning, calls, summed cost snapshots, unpriced calls — the shape
    `_rows` fetches (ledger + Loki) and `aggregate` folds."""
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
    since = datetime(2026, 7, 22, 18, tzinfo=UTC)
    assert usage._window_bounds(None, None) == (None, None)
    from_, to = usage._window_bounds(since, None)
    assert from_ == since and to is not None
    hours_from, hours_to = usage._window_bounds(None, 3)
    assert hours_from is not None and hours_to is not None
    assert abs((hours_to - hours_from).total_seconds() - 3 * 3600) <= 2
