"""`shared.hierarchy.serve` — the run-timeline layer selection contract.

Pure over stored-node shapes: the finest level that fits wins, coarser levels
ride along as context, wire depths are relative to the selected top, and the
coverage result distinguishes none / partial / full for the endpoint's
fallback shape.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from shared.hierarchy.serve import select_layers
from shared.hierarchy.store import StoredNode

W0 = datetime(2026, 9, 12, 4, 0, tzinfo=UTC)
W1 = datetime(2026, 9, 12, 8, 0, tzinfo=UTC)


def node(
    node_id: int,
    *,
    level: int,
    start: datetime | None,
    end: datetime | None,
    text: str = "t",
    parent_id: int | None = None,
) -> StoredNode:
    return StoredNode(
        id=node_id,
        depth=level,  # StoredNode.depth is the ENGINE level (1 = finest)
        span_start=0,
        span_end=1,
        start_ts=start,
        end_ts=end,
        text=text,
        parent_id=parent_id,
        engine_version="0.3",
        prompt_version="0.3",
    )


def test_no_usable_nodes_is_none() -> None:
    assert select_layers([], window_start=W0, window_end=W1, max_nodes=10).coverage == "none"
    assert (
        select_layers(
            [node(1, level=1, start=None, end=None)],
            window_start=W0,
            window_end=W1,
            max_nodes=10,
        ).layers
        is None
    )


def test_single_level_full_coverage() -> None:
    selection = select_layers(
        [node(7, level=1, start=W0, end=W1, text="blocks")],
        window_start=W0,
        window_end=W1,
        max_nodes=10,
    )
    assert selection.coverage == "full"
    assert selection.layers is not None
    (layer,) = selection.layers
    assert (layer.id, layer.depth, layer.parent, layer.summary) == ("7", 0, None, "blocks")


def test_cap_drops_to_the_next_coarser_level() -> None:
    leaves = [node(100 + i, level=1, start=W0, end=W0 + timedelta(minutes=30)) for i in range(4)]
    stage = node(50, level=2, start=W0, end=W1, text="stage", parent_id=None)
    selection = select_layers([*leaves, stage], window_start=W0, window_end=W1, max_nodes=3)
    assert selection.coverage == "full"
    assert selection.layers is not None
    assert [layer.id for layer in selection.layers] == ["50"]  # leaf level dropped


def test_finer_level_fits_and_coarser_rides_along() -> None:
    leaf_a = node(11, level=1, start=W0, end=W0 + timedelta(hours=2), text="a", parent_id=2)
    leaf_b = node(12, level=1, start=W0 + timedelta(hours=2), end=W1, text="b", parent_id=2)
    root = node(2, level=2, start=W0, end=W1, text="root")
    selection = select_layers([leaf_a, leaf_b, root], window_start=W0, window_end=W1, max_nodes=10)
    assert selection.coverage == "full"
    assert selection.layers is not None
    assert [(layer.id, layer.depth, layer.parent) for layer in selection.layers] == [
        ("11", 1, "2"),
        ("12", 1, "2"),
        ("2", 0, None),
    ]


def test_partial_coverage() -> None:
    selection = select_layers(
        [node(1, level=1, start=W0, end=W0 + timedelta(hours=1))],
        window_start=W0,
        window_end=W1,
        max_nodes=10,
    )
    assert selection.coverage == "partial"
    assert selection.layers is not None
