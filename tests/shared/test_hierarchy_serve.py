"""`shared.hierarchy.serve` — the run-timeline layer selection contract.

Pure over stored-node shapes: the finest level that fits wins, coarser levels
ride along as context, wire depths are relative to the selected top, and the
coverage result distinguishes none / partial / full for the endpoint's
fallback shape.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from shared.hierarchy.serve import pending_spans, select_layers
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


def test_coverage_reads_the_union_across_levels() -> None:
    """Mixed-depth segments: a shallower tree's root sits below the selection's
    top and still covers its stretch -- coverage must read the union, not the
    top level alone."""
    deep = node(2, level=3, start=W0, end=W0 + timedelta(hours=2), text="segA")
    shallow = node(5, level=2, start=W0 + timedelta(hours=2), end=W1, text="segB")
    selection = select_layers([deep, shallow], window_start=W0, window_end=W1, max_nodes=10)
    assert selection.coverage == "full"


# --- pending placeholders (B spec, 2026-09-18) ---

P0 = datetime(2026, 9, 18, 9, 0, tzinfo=UTC)


def span(start_min: int, end_min: int) -> tuple[datetime, datetime]:
    return P0 + timedelta(minutes=start_min), P0 + timedelta(minutes=end_min)


def test_pending_empty_without_sealed_history() -> None:
    assert pending_spans([span(0, 30)], [], coverage_start=None) == ()


def test_pending_right_tail_and_fully_covered_window() -> None:
    covered = [span(0, 120)]
    assert pending_spans([span(180, 210)], covered, coverage_start=covered[0][0]) == (
        span(180, 210),
    )
    assert pending_spans([span(0, 90)], covered, coverage_start=covered[0][0]) == ()


def test_pending_internal_gap_between_sealed_stretches() -> None:
    covered = [span(0, 60), span(90, 150)]
    assert pending_spans([span(0, 150)], covered, coverage_start=covered[0][0]) == (span(60, 90),)


def test_pending_never_promises_left_of_coverage() -> None:
    covered = [span(120, 180)]
    activity = [span(0, 60), span(200, 230)]
    assert pending_spans(activity, covered, coverage_start=covered[0][0]) == (span(200, 230),)


def test_pending_clips_the_boundary_span_and_merges_inputs() -> None:
    covered = [span(30, 60)]
    assert pending_spans([span(0, 90)], covered, coverage_start=covered[0][0]) == (span(60, 90),)
    unsorted_covered = [span(60, 90), span(0, 30)]
    adjacent_activity = [span(30, 60), span(0, 30)]
    assert pending_spans(
        adjacent_activity, unsorted_covered, coverage_start=unsorted_covered[1][0]
    ) == (span(30, 60),)
