"""Feedback edges and back-edge bookkeeping on small directed graphs."""

from __future__ import annotations

from meizel.graph.graphs import feedback_edges, is_acyclic, unneeded_back_edges


def without(graph: dict[str, list[str]], edges: list[tuple[str, str]]) -> dict[str, list[str]]:
    return {s: [t for t in ts if (s, t) not in set(edges)] for s, ts in graph.items()}


def test_an_acyclic_graph_has_no_feedback_edges() -> None:
    graph = {"a": ["b", "c"], "b": ["c"], "c": []}
    assert feedback_edges(graph) == []
    assert is_acyclic(graph)


def test_removing_the_feedback_edges_leaves_the_graph_acyclic() -> None:
    graph = {
        "a": ["b"],
        "b": ["c", "a"],
        "c": ["d", "a"],
        "d": ["b", "e"],
        "e": [],
    }
    back = feedback_edges(graph)
    assert not is_acyclic(graph)
    assert is_acyclic(without(graph, back))
    assert len(back) <= 2


def test_a_simple_cycle_loses_exactly_one_edge() -> None:
    assert len(feedback_edges({"a": ["b"], "b": ["c"], "c": ["a"]})) == 1


def test_the_choice_is_deterministic() -> None:
    graph = {"x": ["y"], "y": ["z"], "z": ["x"], "w": ["x"], "q": ["w"]}
    assert feedback_edges(graph) == feedback_edges(dict(reversed(list(graph.items()))))


def test_a_recorded_edge_that_closes_no_cycle_is_unneeded() -> None:
    graph = {"a": ["b"], "b": ["c", "a"], "c": []}
    assert unneeded_back_edges(graph, [("b", "a")]) == []
    assert unneeded_back_edges(graph, [("b", "a"), ("b", "c")]) == [("b", "c")]


def test_an_edge_the_graph_no_longer_has_is_unneeded() -> None:
    assert unneeded_back_edges({"a": ["b"], "b": []}, [("b", "a")]) == [("b", "a")]


def test_two_edges_that_each_break_the_cycle_alone_are_not_both_dropped() -> None:
    graph = {"a": ["b"], "b": ["a"]}
    assert unneeded_back_edges(graph, [("a", "b"), ("b", "a")]) == [("a", "b")]
