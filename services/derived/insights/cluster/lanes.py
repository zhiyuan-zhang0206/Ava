"""The agent lanes: understanding-tree nodes of one level and LLM activity bars, per agent.

Nothing here loads a checkpoint. Nodes carry the `start_ts` / `end_ts` stored with them (the
single-agent run timeline re-derives node times from the message blocks, which differ by at
most a block edge); activity is the `llm_usage` rows; lifecycle markers are audit rows.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, LiteralString, cast

import psycopg

from base.events.contract import LLM_USAGE_KEYS
from base.telemetry.event_sql import numeric
from services.derived.insights.cluster.schemas import (
    AgentLane,
    ClusterLanes,
    ClusterWindow,
    LaneBar,
    LaneEvent,
    LaneNode,
    LevelCount,
)
from services.derived.insights.cluster.selection import TreeAgent

# A lane reads well with about this many nodes; the whole view with `_NODES_PER_LANE` of them
# per agent, but never more than `_NODES_MAX` altogether.
_NODES_PER_LANE = 8
_NODES_MAX = 400
SUMMARY_CHARS = 240

_LIFECYCLE_EVENTS = ["spawn", "resurrect", "restart_completed", "terminate"]


def _bars_sql() -> str:
    """One row per (agent, bin): the calls recorded in the bin, with the earliest request start."""
    cost = numeric(LLM_USAGE_KEYS["cost_usd"])
    latency = numeric(LLM_USAGE_KEYS["latency_ms"])
    tokens_in = numeric(LLM_USAGE_KEYS["in_total"])
    tokens_out = numeric(LLM_USAGE_KEYS["out_total"])
    return f"""
        SELECT agent_id, floor(extract(epoch FROM ts) / %s)::bigint AS bin,
               min(ts - make_interval(secs => COALESCE({latency}, 0) / 1000.0)), max(ts), count(*),
               COALESCE(sum({cost}), 0)::float8,
               COALESCE(sum({tokens_in}), 0)::bigint, COALESCE(sum({tokens_out}), 0)::bigint
        FROM telemetry_events
        WHERE event_name = 'llm_usage' AND agent_id = ANY(%s) AND ts >= %s AND ts < %s
        GROUP BY 1, 2
        ORDER BY 1, 2
    """  # noqa: S608 — keys come from the registered payload constants


_LEVELS_SQL = """
    SELECT depth, count(*) FROM understanding_nodes
    WHERE agent_id = ANY(%s) AND start_ts IS NOT NULL AND end_ts IS NOT NULL
      AND start_ts < %s AND end_ts >= %s
    GROUP BY depth ORDER BY depth
"""

_NODES_SQL = """
    SELECT agent_id, id, depth, parent_id, start_ts, end_ts, left(text, %s)
    FROM understanding_nodes
    WHERE agent_id = ANY(%s) AND depth = %s AND start_ts IS NOT NULL AND end_ts IS NOT NULL
      AND start_ts < %s AND end_ts >= %s
    ORDER BY agent_id, start_ts, id
"""


_BinRow = tuple[datetime, datetime, int, float, int, int]


def choose_level(counts: dict[int, int], agents: int) -> int | None:
    """The level to show when the caller names none: the one whose node count in the window is
    closest, on a log scale, to `8` per agent (at most `400` altogether). Ties go to the
    coarser level (the larger depth). None when no level has a node."""
    present = {level: n for level, n in counts.items() if n > 0}
    if not present:
        return None
    target = min(max(agents, 1) * _NODES_PER_LANE, _NODES_MAX)
    return min(present, key=lambda level: (abs(math.log(present[level] / target)), -level))


def merge_bars(rows: list[_BinRow], gap: timedelta) -> list[LaneBar]:
    """Bars from `(start, end, calls, cost, in, out)` bin rows of one agent in time order:
    rows whose interval starts within `gap` of the running bar's end join it."""
    bars: list[LaneBar] = []
    current: list[Any] | None = None
    for start, end, calls, cost, tokens_in, tokens_out in sorted(rows, key=lambda r: r[0]):
        if current is not None and start - current[1] <= gap:
            current[1] = max(current[1], end)
            current[2] += calls
            current[3] += cost
            current[4] += tokens_in
            current[5] += tokens_out
            continue
        if current is not None:
            bars.append(_bar(current))
        current = [start, end, calls, cost, tokens_in, tokens_out]
    if current is not None:
        bars.append(_bar(current))
    return bars


def _bar(parts: list[Any]) -> LaneBar:
    return LaneBar(
        start=parts[0],
        end=parts[1],
        calls=parts[2],
        cost_usd=parts[3],
        input_tokens=parts[4],
        output_tokens=parts[5],
    )


def _read_nodes(
    conn: psycopg.Connection[Any], ids: list[int], level: int, start: datetime, end: datetime
) -> dict[int, list[LaneNode]]:
    nodes: dict[int, list[LaneNode]] = defaultdict(list)
    for agent_id, node_id, depth, parent, n_start, n_end, text in conn.execute(
        _NODES_SQL, (SUMMARY_CHARS, ids, level, end, start)
    ):
        nodes[int(agent_id)].append(
            LaneNode(
                id=int(node_id),
                level=int(depth),
                parent=None if parent is None else int(parent),
                start=n_start,
                end=n_end,
                summary=text,
            )
        )
    return nodes


def _read_bins(
    conn: psycopg.Connection[Any], ids: list[int], start: datetime, end: datetime, width: int
) -> dict[int, list[_BinRow]]:
    rows: dict[int, list[_BinRow]] = defaultdict(list)
    for agent_id, _, b_start, b_end, calls, cost, tokens_in, tokens_out in conn.execute(
        cast(LiteralString, _bars_sql()), (width, ids, start, end)
    ):
        rows[int(agent_id)].append(
            (b_start, b_end, int(calls), float(cost), int(tokens_in), int(tokens_out))
        )
    return rows


def _read_events(
    conn: psycopg.Connection[Any], ids: list[int], start: datetime, end: datetime
) -> dict[int, list[LaneEvent]]:
    events: dict[int, list[LaneEvent]] = defaultdict(list)
    for agent_id, ts, name in conn.execute(
        "SELECT agent_id, ts, event_name FROM audit_events "
        "WHERE agent_id = ANY(%s) AND event_name = ANY(%s) AND ts >= %s AND ts < %s "
        "ORDER BY ts, id",
        (ids, _LIFECYCLE_EVENTS, start, end),
    ):
        events[int(agent_id)].append(LaneEvent(ts=ts, kind=name))
    return events


def read(
    conn: psycopg.Connection[Any],
    tree: list[TreeAgent],
    start: datetime,
    end: datetime,
    width: int,
    level: int | None,
) -> ClusterLanes:
    """The lanes of `tree` over `[start, end)`; `level` None picks the level (`choose_level`)."""
    ids = [agent.row.id for agent in tree]
    counts = {
        int(depth): int(n) for depth, n in conn.execute(_LEVELS_SQL, (ids, end, start)).fetchall()
    }
    chosen = choose_level(counts, len(ids)) if level is None else level
    nodes = {} if chosen is None else _read_nodes(conn, ids, chosen, start, end)
    bin_rows = _read_bins(conn, ids, start, end, width)
    events = _read_events(conn, ids, start, end)
    gap = timedelta(seconds=width)
    lanes: list[AgentLane] = []
    for agent in tree:
        bars = merge_bars(bin_rows.get(agent.row.id, []), gap)
        lanes.append(
            AgentLane(
                agent_id=agent.row.id,
                parent=agent.parent,
                kind=agent.kind,
                depth=agent.depth,
                status=agent.row.status,
                spawned_at=agent.row.spawned_at,
                calls=sum(bar.calls for bar in bars),
                cost_usd=sum(bar.cost_usd for bar in bars),
                nodes=nodes.get(agent.row.id, []),
                bars=bars,
                events=events.get(agent.row.id, []),
            )
        )
    return ClusterLanes(
        window=ClusterWindow(from_=start, to=end),
        level=chosen,
        auto_level=level is None,
        levels=[LevelCount(level=lv, nodes=n) for lv, n in sorted(counts.items())],
        bin_seconds=width,
        lanes=lanes,
    )
