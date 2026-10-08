"""The cluster view's pure parts: bucket widths, level choice, bar merging and tree order."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from services.derived.insights.cluster import lanes
from services.derived.insights.cluster.selection import AgentRow, lineage_parent, order_tree
from services.derived.insights.cluster.window import bucket_seconds, bucket_start, parse_window

T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def row(
    agent_id: int,
    spawner: str = "user",
    *,
    born: str | None = None,
    fork: int | None = None,
    minutes: int = 0,
) -> AgentRow:
    return AgentRow(agent_id, spawner, born, fork, "idling", T0 + timedelta(minutes=minutes))


def test_bucket_width_is_the_narrowest_round_width_within_the_target() -> None:
    assert bucket_seconds(3600, 120) == 30
    assert bucket_seconds(86400, 120) == 900  # 720 s wanted
    assert bucket_seconds(10, 120) == 1
    assert bucket_seconds(10**9, 120) == 604800


def test_buckets_are_aligned_to_the_epoch() -> None:
    assert bucket_start(0, 60) == datetime(1970, 1, 1, tzinfo=UTC)
    assert bucket_start(int(T0.timestamp()) // 60, 60) == T0


def test_a_window_needs_offsets_and_an_order() -> None:
    with pytest.raises(HTTPException) as naive:
        parse_window(datetime(2026, 1, 1), T0)  # noqa: DTZ001
    assert naive.value.status_code == 422
    with pytest.raises(HTTPException):
        parse_window(T0, T0)
    assert parse_window(T0, T0 + timedelta(hours=1)) == (T0, T0 + timedelta(hours=1))


def test_the_level_closest_to_eight_nodes_per_agent_is_chosen() -> None:
    assert lanes.choose_level({1: 400, 2: 60, 3: 8}, agents=8) == 2  # target 64
    assert lanes.choose_level({1: 400, 2: 60, 3: 8}, agents=1) == 3  # target 8
    assert lanes.choose_level({1: 5000, 2: 900}, agents=500) == 2  # capped at 400 altogether


def test_a_tied_level_goes_to_the_coarser_one_and_no_nodes_means_no_level() -> None:
    assert lanes.choose_level({1: 16, 2: 4}, agents=1) == 2  # both a factor 2 off 8
    assert lanes.choose_level({}, agents=3) is None
    assert lanes.choose_level({1: 0}, agents=3) is None


def bin_row(
    start: int, end: int, calls: int = 1
) -> tuple[datetime, datetime, int, float, int, int]:
    return (T0 + timedelta(seconds=start), T0 + timedelta(seconds=end), calls, 0.5, 10, 2)


def test_bins_closer_than_the_gap_merge_into_one_bar() -> None:
    bars = lanes.merge_bars(
        [bin_row(0, 3), bin_row(4, 6, calls=2), bin_row(30, 33)], timedelta(seconds=2)
    )
    assert [(b.start, b.end, b.calls) for b in bars] == [
        (T0, T0 + timedelta(seconds=6), 3),
        (T0 + timedelta(seconds=30), T0 + timedelta(seconds=33), 1),
    ]
    assert bars[0].cost_usd == 1.0
    assert (bars[0].input_tokens, bars[0].output_tokens) == (20, 4)


def test_an_overlapping_request_keeps_the_longest_end() -> None:
    bars = lanes.merge_bars([bin_row(0, 20), bin_row(5, 8)], timedelta(seconds=1))
    assert [(b.end, b.calls) for b in bars] == [(T0 + timedelta(seconds=20), 2)]


def test_lineage_parent_follows_the_fork_source_then_the_birth_spawner() -> None:
    assert lineage_parent(row(2, "agent:1")) == (1, "spawn")
    assert lineage_parent(row(3, "agent:9", fork=1)) == (1, "fork")
    assert lineage_parent(row(4, "agent:7", born="agent:1")) == (1, "spawn")  # folded since
    assert lineage_parent(row(5, "user")) == (None, "root")
    assert lineage_parent(row(6, "agent:x")) == (None, "root")


def test_tree_order_puts_children_under_their_parent_by_birth() -> None:
    tree = order_tree(
        [
            row(30, "agent:10", minutes=5),
            row(10),
            row(20, "agent:10", minutes=1),
            row(21, "agent:20", minutes=2),
            row(40, "agent:99", minutes=3),  # parent outside the selection: a root
        ]
    )
    assert [(t.row.id, t.parent, t.depth) for t in tree] == [
        (10, None, 0),
        (20, 10, 1),
        (21, 20, 2),
        (30, 10, 1),
        (40, None, 0),
    ]
    assert [t.kind for t in tree] == ["root", "spawn", "spawn", "spawn", "root"]


def test_a_lineage_cycle_places_every_agent_once() -> None:
    tree = order_tree(
        [row(1, "agent:2", minutes=0), row(2, "agent:1", minutes=1), row(3, "agent:3")]
    )
    assert sorted(t.row.id for t in tree) == [1, 2, 3]
