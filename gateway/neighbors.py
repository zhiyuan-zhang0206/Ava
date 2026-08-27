"""Neighbor graph read — the computation behind /api/agents/{id}/neighbors.

Task #180 (LGTM cutover sweep): the retired `agent_neighbors()` SQL function
read the unified `events` table, which stopped being written at the LGTM
cutover (task #1197) — every agent has answered "no peers" since. This module
moves the read to the live event stream with the same archive stitch the
fleet graph uses: rows older than the archive freeze point (max(events.ts))
come from the frozen PG archive, rows at/after it from Loki. Task #1281
imports the archive into Loki, after which the archive read collapses and
this module becomes Loki-only.

Weight semantics are unchanged from the retired SQL function: a tie between
two agents is undirected; lineage events (spawn/fork/resurrect) weigh
LN(1+count) permanently, message events (send_message) weigh
EXP(-k * days_since_last) * LN(1+count). The recursive walk is a BFS/DFS
path walk in Python (the SQL recursive CTE had no Loki equivalent):
`max_depth` bounds the hop count, each extra hop discounts the score by
`gamma`, and a node's result is its shallowest arrival with the best score.

Ancestors: the read also returns the queried agent's spawn chain — the
agents that spawned it, walked upward over DIRECTED spawn/fork edges. The
event stream writes those rows as agent_id = the new agent, target_agent_id
= its lineage parent (the spawner for a spawn, the fork source for a fork —
fork-lineage ruling 2026-08-28), so the upward walk follows
agent_id -> target_agent_id.
Only creation events carry parentage: send_message is a peer tie and
resurrect wakes an existing agent, so neither forms an ancestor. Spawn
chains form a forest, so the upward walk is a simple linked-list traversal
to the top (a visited set guards against malformed cycles — no depth cap).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg import Cursor
from psycopg_pool import ConnectionPool

from gateway import events_archive, loki_events
from shared.log import logger

# Audit event names that form ties (same family as fleet_graph._EDGE_EVENT_NAMES).
_LINEAGE_EVENT_NAMES = ("spawn", "fork", "resurrect")
_EDGE_EVENT_NAMES = ("send_message", *_LINEAGE_EVENT_NAMES)

# The lineage subset that creates parentage. Resurrect wakes an EXISTING
# agent, so it ties but never parents.
_ANCESTOR_EVENT_NAMES = ("spawn", "fork")

# Loki fetch cap for the edge stream. Audit events are low-volume since the
# cutover; the cap is a guardrail, not an expectation (mirrors fleet_graph).
_LOKI_EDGE_LIMIT = 50_000


def _archive_boundary(cur: Cursor) -> datetime | None:
    """The frozen PG `events` archive's freeze point — its newest row's ts.

    Rows older than the boundary come from the archive, rows at/after it from
    Loki (task #1280 interim; task #1281 imports the archive into Loki, after
    which the archive read collapses to Loki-only)."""
    return events_archive.load_frozen_boundary(cur)


def _fetch_archive_edges(
    cur: Cursor, *, boundary: datetime | None
) -> list[tuple[int, int, str, int, datetime]]:
    """Pre-cutover tie rows from the frozen PG archive, grouped per
    (least, greatest, event_name) with raw count + last-seen ts — the merge
    step applies the LN/EXP weight math on the COMBINED counts so the two
    sides sum exactly like one table would."""
    if boundary is None:
        return []
    cur.execute(
        "SELECT LEAST(agent_id, target_agent_id) AS a, "
        "       GREATEST(agent_id, target_agent_id) AS b, "
        "       event_name, "
        "       COUNT(*) AS cnt, "
        "       MAX(ts) AS last_seen "
        "FROM events "
        "WHERE category = 'audit' "
        "  AND event_name IN ('send_message', 'spawn', 'fork', 'resurrect') "
        "  AND target_agent_id IS NOT NULL "
        "  AND agent_id IS NOT NULL "
        "  AND ts < %s "
        "GROUP BY 1, 2, 3",
        (boundary,),
    )
    return [(int(r[0]), int(r[1]), str(r[2]), int(r[3]), r[4]) for r in cur.fetchall()]


def _fetch_archive_lineage(cur: Cursor, *, boundary: datetime | None) -> list[tuple[int, int, int]]:
    """Pre-cutover spawn/fork rows from the frozen PG archive, grouped per
    DIRECTED (child, parent) pair with the raw count.

    Direction is what the neighbor merge deliberately discards (undirected
    ties) but the ancestor walk needs: the events stream writes spawn/fork
    rows as agent_id = the new agent, target_agent_id = its lineage parent
    (the spawner for a spawn, the fork source for a fork — fork-lineage
    ruling 2026-08-28)."""
    if boundary is None:
        return []
    cur.execute(
        "SELECT agent_id, target_agent_id, COUNT(*) "
        "FROM events "
        "WHERE category = 'audit' "
        "  AND event_name IN ('spawn', 'fork') "
        "  AND target_agent_id IS NOT NULL "
        "  AND agent_id IS NOT NULL "
        "  AND ts < %s "
        "GROUP BY 1, 2",
        (boundary,),
    )
    return [(int(r[0]), int(r[1]), int(r[2])) for r in cur.fetchall()]


def _fetch_loki_edges(*, boundary: datetime | None, now: datetime) -> list[dict[str, Any]]:
    """Live-tail audit rows from Loki since the archive freeze boundary."""
    loki_from = boundary if boundary is not None else now - timedelta(days=30)
    rows, has_more = loki_events.query_events(
        event_names=list(_EDGE_EVENT_NAMES),
        categories=["audit"],
        from_=loki_from,
        to=now,
        limit=_LOKI_EDGE_LIMIT,
        direction="forward",
    )
    if has_more:
        logger.warning(
            "neighbors Loki edge stream exceeded the %d-row fetch cap — ties truncated",
            _LOKI_EDGE_LIMIT,
        )
    return rows


def _merge_weights(
    archive_rows: list[tuple[int, int, str, int, datetime]],
    loki_rows: list[dict[str, Any]],
    *,
    k: float,
    now: datetime,
) -> dict[tuple[int, int], float]:
    """Combined undirected tie weights keyed by (least, greatest).

    Per pair: lineage weight = LN(1 + total lineage count) — permanent.
    Message weight = EXP(-k * days_since_last_message) * LN(1 + total
    message count) — the decay reference is `now`, and per-row the count is
    summed across BOTH sources before the LN so the merge is exact."""
    counts: dict[tuple[int, int, str], tuple[int, datetime]] = {}

    def _absorb(a: int, b: int, name: str, cnt: int, last_seen: datetime) -> None:
        key = (min(a, b), max(a, b), name)
        prev = counts.get(key)
        if prev is None:
            counts[key] = (cnt, last_seen)
        else:
            counts[key] = (prev[0] + cnt, max(prev[1], last_seen))

    for a, b, name, cnt, last_seen in archive_rows:
        _absorb(a, b, name, cnt, last_seen)

    for r in loki_rows:
        agent = r.get("agent_id")
        target = r.get("target_agent_id")
        name = r.get("event_name")
        if agent is None or target is None or name is None or r.get("ts") is None:
            continue
        _absorb(int(agent), int(target), str(name), 1, r["ts"])

    weights: dict[tuple[int, int], float] = {}
    for (a, b, name), (cnt, last_seen) in counts.items():
        if name in _LINEAGE_EVENT_NAMES:
            weights[(a, b)] = weights.get((a, b), 0.0) + math.log1p(cnt)
        else:  # send_message
            days = (now - last_seen).total_seconds() / 86400.0
            weights[(a, b)] = weights.get((a, b), 0.0) + math.exp(-k * days) * math.log1p(cnt)
    return weights


def _merge_lineage_parents(
    archive_rows: list[tuple[int, int, int]],
    loki_rows: list[dict[str, Any]],
) -> dict[int, dict[int, float]]:
    """Directed spawn/fork parent edges: child -> {parent: lineage weight}.

    Weight per directed pair = LN(1 + combined spawn/fork count), the same
    lineage weight the undirected neighbor tie uses — the merge sums both
    sources before the LN, exactly like `_merge_weights`."""
    counts: dict[tuple[int, int], int] = {}
    for child, parent, cnt in archive_rows:
        counts[(child, parent)] = counts.get((child, parent), 0) + cnt
    for r in loki_rows:
        name = r.get("event_name")
        child = r.get("agent_id")
        parent = r.get("target_agent_id")
        if name not in _ANCESTOR_EVENT_NAMES or child is None or parent is None:
            continue
        key = (int(child), int(parent))
        counts[key] = counts.get(key, 0) + 1
    parents: dict[int, dict[int, float]] = {}
    for (child, parent), cnt in counts.items():
        parents.setdefault(child, {})[parent] = math.log1p(cnt)
    return parents


def _walk_ancestors(
    parents: dict[int, dict[int, float]], *, root: int, gamma: float
) -> list[tuple[int, int, float]]:
    """Walk the spawn/fork parent chain upward from `root` to the top —
    (agent_id, depth, score) rows, nearest ancestor first.

    depth = hops up (1 = the agent that directly spawned the queried agent);
    score = that edge's lineage weight discounted by `gamma` per hop, the
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


def compute(
    pool: ConnectionPool,
    *,
    root: int,
    max_depth: int,
    limit: int,
    k: float = 0.5,
    gamma: float = 0.5,
) -> tuple[list[tuple[int, int, float]], list[tuple[int, int, float]]]:
    """(neighbors, ancestors) for `root` — each a list of (agent_id, depth,
    score) rows; neighbors strongest first, ancestors nearest first. The
    Python counterpart of the retired agent_neighbors() SQL function, plus
    the spawn-chain read it never had."""
    now = datetime.now(UTC)
    with pool.connection() as conn, conn.cursor() as cur:
        boundary = _archive_boundary(cur)
        archive_rows = _fetch_archive_edges(cur, boundary=boundary)
        archive_lineage = _fetch_archive_lineage(cur, boundary=boundary)
    loki_rows = _fetch_loki_edges(boundary=boundary, now=now)
    weights = _merge_weights(archive_rows, loki_rows, k=k, now=now)
    parents = _merge_lineage_parents(archive_lineage, loki_rows)
    return (
        _walk(weights, root=root, max_depth=max_depth, gamma=gamma, limit=limit),
        _walk_ancestors(parents, root=root, gamma=gamma),
    )
