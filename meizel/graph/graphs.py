"""Small directed-graph helpers for local graphs: components, cycles, reduction."""

from __future__ import annotations

from collections.abc import Hashable, Iterable, Mapping

__all__ = ["cycle_through", "reachable", "strongly_connected", "transitive_reduction_size"]


def strongly_connected[N: Hashable](graph: Mapping[N, Iterable[N]]) -> list[list[N]]:
    """Strongly connected components with more than one node (Tarjan).

    Local graphs hold at most a few dozen nodes, so the recursion stays shallow.
    """
    successors: dict[N, list[N]] = {n: list(targets) for n, targets in graph.items()}
    for targets in list(successors.values()):
        for target in targets:
            successors.setdefault(target, [])
    index: dict[N, int] = {}
    low: dict[N, int] = {}
    stack: list[N] = []
    result: list[list[N]] = []

    def visit(node: N) -> None:
        index[node] = low[node] = len(index)
        stack.append(node)
        for target in successors[node]:
            if target not in index:
                visit(target)
                low[node] = min(low[node], low[target])
            elif target in stack:
                low[node] = min(low[node], index[target])
        if low[node] == index[node]:
            component = stack[stack.index(node) :]
            del stack[stack.index(node) :]
            if len(component) > 1:
                result.append(component)

    for root in successors:
        if root not in index:
            visit(root)
    return result


def reachable[N: Hashable](
    graph: Mapping[N, Iterable[N]], start: N, skip: tuple[N, N] | None = None
) -> set[N]:
    seen: set[N] = set()
    pending = [start]
    while pending:
        node = pending.pop()
        for target in graph.get(node, ()):
            if skip == (node, target) or target in seen:
                continue
            seen.add(target)
            pending.append(target)
    return seen


def cycle_through[N: Hashable](graph: Mapping[N, Iterable[N]], members: Iterable[N]) -> list[N]:
    """A shortest cycle inside a strongly connected component, as [a, b, ..., a]."""
    inside = set(members)
    best: list[N] = []
    for start in sorted(inside, key=str):
        parents: dict[N, N] = {}
        queue = [start]
        found = False
        while queue and not found:
            node = queue.pop(0)
            for target in graph.get(node, ()):
                if target not in inside:
                    continue
                if target == start:
                    path = [node]
                    while path[-1] != start:
                        path.append(parents[path[-1]])
                    path.reverse()
                    candidate = [*path, start]
                    if not best or len(candidate) < len(best):
                        best = candidate
                    found = True
                    break
                if target not in parents:
                    parents[target] = node
                    queue.append(target)
    return best


def transitive_reduction_size[N: Hashable](graph: Mapping[N, Iterable[N]]) -> int:
    """Edges left after dropping those implied by a longer path; cycles keep every edge."""
    components = {m: tuple(c) for c in strongly_connected(graph) for m in c}
    kept = 0
    for node, targets in graph.items():
        for target in targets:
            in_cycle = target in components.get(node, ())
            if in_cycle or target not in reachable(graph, node, skip=(node, target)):
                kept += 1
    return kept
