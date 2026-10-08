"""Neighbor graph read — the computation behind /api/agents/{id}/neighbors.

The tie graph reads the audit record: `audit_events` in Postgres
(base/events/audit_rows.py), aggregated per (agent, target, event) pair. The
retired `agent_neighbors()` SQL function read the old `events` table; the walks
run in Python (the SQL recursive CTE had no equivalent over the event stream).

Weight semantics are unchanged from the retired SQL function: a tie between
two agents is undirected; lineage events (spawn/fork/resurrect) weigh
LN(1+count) permanently, message events (send_message) weigh
EXP(-k * days_since_last) * LN(1+count). The recursive walk is a BFS/DFS
path walk in Python: `max_depth` bounds the hop count, each extra hop discounts
the score by `gamma`, and a node's result is its shallowest arrival with the
best score.

Ancestors: the read also returns the queried agent's birth chain — the agents
that spawned it, walked upward over immutable agents_meta.born_spawner edges.
Send_message and resurrect remain peer ties only; they never form an
ancestor. Spawn chains form a forest, so the upward walk is a simple
linked-list traversal to the top (a visited set guards against malformed
cycles — no depth cap).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from base.events import audit_rows
from base.events.audit_rows import LINEAGE_EVENT_NAMES

# Bound on the tie read, below the route's client timeouts.
_TIE_READ_STATEMENT_TIMEOUT_MS = 8_000


def _fetch_edge_counts(pool: Any) -> list[tuple[int, int, str, int, datetime]]:
    """Per-edge `(agent, target, event_name, count, last_seen)` from the audit record."""
    with pool.connection() as conn:
        conn.execute(f"SET LOCAL statement_timeout = {_TIE_READ_STATEMENT_TIMEOUT_MS}")
        return audit_rows.edge_counts(conn)


def _weights(
    edges: list[tuple[int, int, str, int, datetime]],
    *,
    k: float,
    now: datetime,
) -> dict[tuple[int, int], float]:
    """Undirected tie weights keyed by (least, greatest).

    Per pair: lineage weight = LN(1 + total lineage count) — permanent.
    Message weight = EXP(-k * days_since_last_message) * LN(1 + total
    message count) — the decay reference is `now`. The two directions of a pair
    are summed before the LN."""
    counts: dict[tuple[int, int, str], tuple[int, datetime]] = {}
    for agent, target, name, cnt, last_seen in edges:
        key = (min(agent, target), max(agent, target), name)
        prev = counts.get(key)
        counts[key] = (cnt, last_seen) if prev is None else (prev[0] + cnt, max(prev[1], last_seen))

    weights: dict[tuple[int, int], float] = {}
    for (a, b, name), (cnt, last_seen) in counts.items():
        if name in LINEAGE_EVENT_NAMES:
            weights[(a, b)] = weights.get((a, b), 0.0) + math.log1p(cnt)
        else:  # send_message
            days = (now - last_seen).total_seconds() / 86400.0
            weights[(a, b)] = weights.get((a, b), 0.0) + math.exp(-k * days) * math.log1p(cnt)
    return weights


def _fetch_born_spawner_parents(pool: Any, *, root: int) -> dict[int, dict[int, float]]:
    """The immutable birth chain above ``root`` as child -> parent edges.

    Each child has at most one born_spawner parent, so every edge carries the
    fixed single-birth weight LN(1 + 1). The recursive query follows only
    canonical agent identifiers and prevents malformed cycles from revisiting
    a row before `_walk_ancestors` applies its own defensive guard.
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH RECURSIVE spawner_walk(child_id, parent_id, path) AS (
                SELECT id,
                       substring(born_spawner FROM '^agent:([0-9]+)$')::BIGINT,
                       ARRAY[id]
                FROM agents_meta
                WHERE id = %s AND born_spawner ~ '^agent:[0-9]+$'
                UNION ALL
                SELECT parent.id,
                       substring(parent.born_spawner FROM '^agent:([0-9]+)$')::BIGINT,
                       walk.path || parent.id
                FROM spawner_walk AS walk
                JOIN agents_meta AS parent ON parent.id = walk.parent_id
                WHERE parent.born_spawner ~ '^agent:[0-9]+$'
                  AND NOT parent.id = ANY(walk.path)
            )
            SELECT child_id, parent_id FROM spawner_walk
            """,
            (root,),
        )
        rows = cur.fetchall()
    return {child: {parent: math.log1p(1)} for child, parent in rows}


def _walk_ancestors(
    parents: dict[int, dict[int, float]], *, root: int, gamma: float
) -> list[tuple[int, int, float]]:
    """Walk the immutable birth-parent chain upward from `root` to the top —
    (agent_id, depth, score) rows, nearest ancestor first.

    depth = hops up (1 = the direct birth parent of the queried agent);
    score = that edge's fixed birth weight discounted by `gamma` per hop, the
    same convention `_walk` uses. Chains are a forest, so the traversal is a
    simple upward walk with a visited set against malformed cycles — no
    depth cap (a chain is at most as long as agents have spawned agents)."""
    seen: dict[int, tuple[int, float]] = {}
    frontier: list[tuple[int, int]] = [(root, 0)]
    while frontier:
        nxt: list[tuple[int, int]] = []
        for node, depth in frontier:
            for parent, w in parents.get(node, {}).items():
                if parent in seen or parent == root:
                    continue
                seen[parent] = (depth + 1, w * gamma**depth)
                nxt.append((parent, depth + 1))
        frontier = nxt
    return sorted(
        ((n, seen[n][0], seen[n][1]) for n in seen),
        key=lambda t: (t[1], -t[2]),
    )


def _walk(
    weights: dict[tuple[int, int], float],
    *,
    root: int,
    max_depth: int,
    gamma: float,
    limit: int,
) -> list[tuple[int, int, float]]:
    """Path walk from `root`, replicating the retired SQL recursive CTE.

    Every path that never revisits a node is followed to `max_depth`; a
    node's result is its shallowest arrival depth with the best score
    (edge weight * gamma**(depth-1))."""
    adj: dict[int, list[tuple[int, float]]] = {}
    for (a, b), w in weights.items():
        adj.setdefault(a, []).append((b, w))
        adj.setdefault(b, []).append((a, w))

    best_depth: dict[int, int] = {}
    best_score: dict[int, float] = {}
    # (node, depth, hop_w, path) — hop_w is the last edge's weight.
    frontier: list[tuple[int, int, float, frozenset[int]]] = [(root, 0, 0.0, frozenset({root}))]
    while frontier:
        nxt: list[tuple[int, int, float, frozenset[int]]] = []
        for node, depth, hop_w, path in frontier:
            if node != root:
                score = hop_w * gamma ** (depth - 1)
                prev_d = best_depth.get(node)
                if prev_d is None:
                    best_depth[node] = depth
                    best_score[node] = score
                else:
                    best_depth[node] = min(prev_d, depth)
                    best_score[node] = max(best_score[node], score)
            if depth < max_depth:
                for dst, w in adj.get(node, []):
                    if dst not in path:
                        nxt.append((dst, depth + 1, w, path | {dst}))
        frontier = nxt

    ranked = sorted(
        ((n, best_depth[n], best_score[n]) for n in best_score),
        key=lambda t: t[2],
        reverse=True,
    )
    return ranked[:limit]


def born_chain(pool: Any, *, root: int, gamma: float = 0.5) -> list[tuple[int, int, float]]:
    """The immutable birth-parent chain above `root` — (agent_id, depth, score)
    rows, nearest ancestor first.

    The light half of `compute`: one recursive born_spawner SQL plus the
    upward walk, with none of the tie graph's archive / Loki reads. Callers
    that need only the chain (the inherited-memory context note, read at every
    window establishment) must not drag the tie computation into their path."""
    parents = _fetch_born_spawner_parents(pool, root=root)
    return _walk_ancestors(parents, root=root, gamma=gamma)


def compute(
    *,
    root: int,
    max_depth: int,
    limit: int,
    db_pool: Any,
    k: float = 0.5,
    gamma: float = 0.5,
) -> tuple[list[tuple[int, int, float]], list[tuple[int, int, float]]]:
    """(neighbors, ancestors) for `root` — lists of (agent_id, depth, score)
    rows; neighbors strongest first, ancestors nearest first. The Python
    counterpart of the retired agent_neighbors() SQL function, plus the
    immutable spawn-chain read it never had."""
    now = datetime.now(UTC)
    weights = _weights(_fetch_edge_counts(db_pool), k=k, now=now)
    parents = _fetch_born_spawner_parents(db_pool, root=root)
    return (
        _walk(weights, root=root, max_depth=max_depth, gamma=gamma, limit=limit),
        _walk_ancestors(parents, root=root, gamma=gamma),
    )
