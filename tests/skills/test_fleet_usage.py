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
from datetime import UTC, date, datetime
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


LokiRow = tuple[int, str, int, int, int, int, int, float, int]


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
) -> LokiRow:
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


class _LedgerCursor:
    """Cursor fake for the whole-life ledger+tail split."""

    def __init__(self, newest_day: date, stale_rows: list[tuple[object, ...]]) -> None:
        self.newest_day = newest_day
        self.stale_rows = stale_rows
        self.queries: list[tuple[str, tuple[object, ...]]] = []
        self._rows: list[tuple[object, ...]] = []

    def __enter__(self) -> _LedgerCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, query: str, params: tuple[object, ...]) -> None:
        self.queries.append((query, params))
        if "SELECT agent_id, max(day)" in query:
            self._rows = [(7, self.newest_day)]
        elif "day < %s" in query:
            self._rows = []
        else:
            self._rows = self.stale_rows

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows


class _LedgerConnection:
    def __init__(self, cursor: _LedgerCursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> _LedgerConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def cursor(self) -> _LedgerCursor:
        return self._cursor


def test_whole_life_gap_day_excludes_stale_fleet_ledger_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fleet usage rereads the retained newest day, including its late write."""
    floor = datetime(2026, 8, 10, tzinfo=UTC)
    cursor = _LedgerCursor(
        newest_day=date(2026, 8, 10),
        stale_rows=[("m", 1, 1, 0, 100, 100, 0, 0, 1.0)],
    )
    monkeypatch.setattr(usage, "connect", lambda: _LedgerConnection(cursor))
    monkeypatch.setattr(usage, "retention_floor", lambda: floor)
    tails: list[datetime] = []

    def live_rows(_agent_ids: list[int], from_: datetime, _to: datetime | None) -> list[LokiRow]:
        tails.append(from_)
        # The true stream contains the original ledgered 100 plus late 50.
        return [_row(7, "m", 0, 150, 0, 0, 1, 1.5, 0)]

    monkeypatch.setattr(usage, "_loki_rows", live_rows)

    rows = usage._rows([7], None, None)

    assert tails == [floor]
    assert all("day < %s" in query for query, _params in cursor.queries[1:])
    assert usage.aggregate(rows)["total"]["tokens_out"] == 150


def test_whole_life_final_fleet_ledger_keeps_history_and_clamps_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A final pre-retention ledger day remains while the tail starts at the floor."""
    floor = datetime(2026, 8, 10, 12, tzinfo=UTC)
    cursor = _LedgerCursor(
        newest_day=date(2026, 8, 8),
        stale_rows=[("m", 1, 1, 0, 100, 100, 0, 0, 1.0)],
    )
    monkeypatch.setattr(usage, "connect", lambda: _LedgerConnection(cursor))
    monkeypatch.setattr(usage, "retention_floor", lambda: floor)
    tails: list[datetime] = []

    def live_rows(_agent_ids: list[int], from_: datetime, _to: datetime | None) -> list[LokiRow]:
        tails.append(from_)
        return [_row(7, "m", 0, 50, 0, 0, 1, 0.5, 0)]

    monkeypatch.setattr(usage, "_loki_rows", live_rows)

    rows = usage._rows([7], None, None)

    assert tails == [floor]
    assert all("day < %s" not in query for query, _params in cursor.queries[1:])
    assert usage.aggregate(rows)["total"]["tokens_out"] == 150
