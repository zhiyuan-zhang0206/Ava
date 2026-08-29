"""Neighbor graph read — the computation behind /api/agents/{id}/neighbors.

Task #180 (LGTM cutover sweep): the retired `agent_neighbors()` SQL function
read the unified `events` table, which stopped being written at the LGTM
cutover (task #1197) — every agent has answered "no peers" since. This module
moves the read to the Loki event streams: rows before the LGTM cutover come
from the task #1281 archive stream, rows at/after it from the live stream
(the same two-stream stitch the fleet graph uses).

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

Performance (task #1958): the archive stream is immutable — it froze at the
cutover and no new rows enter it — yet every request re-scanned its ~24k
edge rows (~5-28s Loki query, occasionally timing out at the client's 45s
bound). The archive rows are now cached in Redis for a day (same pattern and
TTL as the fleet graph's frozen-source caches), leaving one bounded live-tail
read per request. The live read carries an 8s timeout (the fleet graph's
telemetry-read bound) so a stalled Loki fails the route in seconds instead of
pinning it for 45s. The cache is fail-open: a Redis outage degrades to a
direct query, never to a 500.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Any

from gateway import loki_events
from shared.log import logger
from shared.loki_index_labels import ARCHIVE_FLOOR_AT, ARCHIVE_FREEZE_AT
from shared.redis_client import sync_redis

# Audit event names that form ties (same family as fleet_graph._EDGE_EVENT_NAMES).
_LINEAGE_EVENT_NAMES = ("spawn", "fork", "resurrect")
_EDGE_EVENT_NAMES = ("send_message", *_LINEAGE_EVENT_NAMES)

# The lineage subset that creates parentage. Resurrect wakes an EXISTING
# agent, so it ties but never parents.
_ANCESTOR_EVENT_NAMES = ("spawn", "fork")

# Loki fetch cap for the edge stream. Audit events are low-volume since the
# cutover; the cap is a guardrail, not an expectation (mirrors fleet_graph).
_LOKI_EDGE_LIMIT = 50_000

# Frozen-source cache (mirrors gateway/routers/fleet_graph.py): the archive
# stream is immutable, so a 24h Redis entry turns its per-request scan into a
# once-a-day one. 24h matches the fleet-graph precedent and self-heals if the
# archive is ever rebuilt or the entry is evicted.
_FROZEN_CACHE_TTL_SECONDS = 24 * 60 * 60
_ARCHIVE_CACHE_KEY = "neighbors:archive:v1"

# The live-tail read is the only per-request Loki query left. Bound it well
# below the shared client's 45s default so a stalled Loki fails the route in
# seconds instead of pinning a request (and a query-budget slot) for 45s —
# the same bound fleet_graph uses for its telemetry reads.
_LIVE_READ_TIMEOUT_S = 8.0


def _read_frozen_json(key: str, *, cache_name: str) -> Any | None:
    """Read one frozen-source payload, treating Redis or JSON errors as misses."""
    try:
        with sync_redis(decode_responses=True) as redis:
            cached = redis.get(key)
        return json.loads(cached) if cached is not None else None
    except Exception as exc:
        logger.debug("neighbors frozen %s cache read failed — querying source: %s", cache_name, exc)
        return None


def _write_frozen_json(key: str, payload: object, *, cache_name: str) -> None:
    """Write one frozen-source payload without making Redis route-critical."""
    try:
        with sync_redis(decode_responses=True) as redis:
            redis.set(key, json.dumps(payload), ex=_FROZEN_CACHE_TTL_SECONDS)
    except Exception as exc:
        logger.debug("neighbors frozen %s cache write failed: %s", cache_name, exc)


def _rows_from_cache_payload(raw: Any, *, cache_name: str) -> list[dict[str, Any]] | None:
    """Decode a cached [agent_id, target_agent_id, event_name, ts] row list.

    Returns None on any shape/type mismatch so the caller refetches the
    source — a corrupt entry must never be served as a (wrong) answer."""
    try:
        return [
            {
                "agent_id": row[0],
                "target_agent_id": row[1],
                "event_name": row[2],
                "ts": datetime.fromisoformat(row[3]),
            }
            for row in raw["rows"]
        ]
    except Exception as exc:
        logger.debug("neighbors frozen %s cache decode failed: %s", cache_name, exc)
        return None


def _fetch_archive_rows() -> list[dict[str, Any]]:
    """Pre-cutover tie + lineage rows from the Loki archive stream.

    Raw rows (not grouped): the merge absorbs both archive and live rows
    with the same per-row math, so the two sides sum exactly like one table
    would. The archive stream holds only pre-cutover rows, so the query is
    bounded to the archive's own span (within Loki's 90d max_query_length).
    The rows are immutable and cached in Redis for a day; a miss runs the
    Loki query and repopulates the cache. The fetch keeps the shared client's
    45s default timeout (NOT the 8s live-read bound): it is a once-a-day
    refresh whose measured range is 5-28s, so an 8s bound would time it out
    on a cold Loki, leave the cache empty, and turn every request in that
    window into a failure — worse than the slowness the cache exists to fix.
    The fetch's `has_more` flag is persisted with the rows, so a cache hit
    reports truncation exactly as the originating fetch did (no masking)."""
    cached = _read_frozen_json(_ARCHIVE_CACHE_KEY, cache_name="Loki archive")
    if cached is not None:
        rows = _rows_from_cache_payload(cached, cache_name="Loki archive")
        if rows is not None:
            # The payload persists the originating fetch's has_more (entries
            # without it predate that shape — a full page then conservatively
            # counts as truncated).
            if cached.get("has_more", len(rows) >= _LOKI_EDGE_LIMIT):
                logger.warning(
                    "neighbors Loki archive stream exceeded the %d-row fetch cap — ties truncated",
                    _LOKI_EDGE_LIMIT,
                )
            return rows
    rows, has_more = loki_events.query_events(
        event_names=list(_EDGE_EVENT_NAMES),
        categories=["audit"],
        from_=ARCHIVE_FLOOR_AT,
        to=ARCHIVE_FREEZE_AT,
        limit=_LOKI_EDGE_LIMIT,
        direction="forward",
        archive=True,
    )
    if has_more:
        logger.warning(
            "neighbors Loki archive stream exceeded the %d-row fetch cap — ties truncated",
            _LOKI_EDGE_LIMIT,
        )
    _write_frozen_json(
        _ARCHIVE_CACHE_KEY,
        {
            "rows": [
                [row["agent_id"], row["target_agent_id"], row["event_name"], row["ts"].isoformat()]
                for row in rows
            ],
            "has_more": has_more,
        },
        cache_name="Loki archive",
    )
    return rows


def _fetch_loki_edges(*, now: datetime) -> list[dict[str, Any]]:
    """Live-tail audit rows from Loki since the archive freeze point.

    The only per-request Loki read left after the archive cache; bounded at
    `_LIVE_READ_TIMEOUT_S` so a stalled Loki fails the route fast instead of
    pinning it for the shared client's 45s default."""
    loki_from = ARCHIVE_FREEZE_AT
    rows, has_more = loki_events.query_events(
        event_names=list(_EDGE_EVENT_NAMES),
        categories=["audit"],
        from_=loki_from,
        to=now,
        limit=_LOKI_EDGE_LIMIT,
        direction="forward",
        timeout_s=_LIVE_READ_TIMEOUT_S,
    )
    if has_more:
        logger.warning(
            "neighbors Loki edge stream exceeded the %d-row fetch cap — ties truncated",
            _LOKI_EDGE_LIMIT,
        )
    return rows


def _merge_weights(
    archive_rows: list[dict[str, Any]],
    loki_rows: list[dict[str, Any]],
    *,
    k: float,
    now: datetime,
) -> dict[tuple[int, int], float]:
    """Combined undirected tie weights keyed by (least, greatest).

    Per pair: lineage weight = LN(1 + total lineage count) — permanent.
    Message weight = EXP(-k * days_since_last_message) * LN(1 + total
    message count) — the decay reference is `now`, and per-row the count is
    summed across BOTH sources before the LN so the merge is exact. Both
    sides carry raw rows (the archive side comes from the Loki archive
    stream)."""
    counts: dict[tuple[int, int, str], tuple[int, datetime]] = {}

    def _absorb(a: int, b: int, name: str, cnt: int, last_seen: datetime) -> None:
        key = (min(a, b), max(a, b), name)
        prev = counts.get(key)
        if prev is None:
            counts[key] = (cnt, last_seen)
        else:
            counts[key] = (prev[0] + cnt, max(prev[1], last_seen))

    for r in [*archive_rows, *loki_rows]:
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
    archive_rows: list[dict[str, Any]],
    loki_rows: list[dict[str, Any]],
) -> dict[int, dict[int, float]]:
    """Directed spawn/fork parent edges: child -> {parent: lineage weight}.

    Weight per directed pair = LN(1 + combined spawn/fork count), the same
    lineage weight the undirected neighbor tie uses — the merge sums both
    sources before the LN, exactly like `_merge_weights`."""
    counts: dict[tuple[int, int], int] = {}
    for r in [*archive_rows, *loki_rows]:
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
    archive_rows = _fetch_archive_rows()
    loki_rows = _fetch_loki_edges(now=now)
    weights = _merge_weights(archive_rows, loki_rows, k=k, now=now)
    parents = _merge_lineage_parents(archive_rows, loki_rows)
    return (
        _walk(weights, root=root, max_depth=max_depth, gamma=gamma, limit=limit),
        _walk_ancestors(parents, root=root, gamma=gamma),
    )
