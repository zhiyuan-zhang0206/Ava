"""The agents of a cluster read: a root and its lineage, in tree order."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

from base.telemetry.metrics.usage import Lineage, select_agents
from services.derived.insights.cluster.schemas import AgentKind


@dataclass(frozen=True)
class AgentRow:
    """The lineage columns of `agents_meta` one lane needs."""

    id: int
    spawner: str
    born_spawner: str | None
    fork_source: int | None
    status: str
    spawned_at: datetime


@dataclass(frozen=True)
class TreeAgent:
    """An agent placed in the tree: its lineage parent within the selection and its depth."""

    row: AgentRow
    parent: int | None
    kind: AgentKind
    depth: int


def lineage_parent(row: AgentRow) -> tuple[int | None, AgentKind]:
    """The agent a row descends from, as `select_agents` follows it: the fork source of a fork,
    else the immutable birth spawner (the current spawner only for rows born before it was kept)."""
    if row.fork_source is not None:
        return row.fork_source, "fork"
    spawner = row.born_spawner if row.born_spawner is not None else row.spawner
    if spawner.startswith("agent:") and spawner.removeprefix("agent:").isdecimal():
        return int(spawner.removeprefix("agent:")), "spawn"
    return None, "root"


def order_tree(rows: list[AgentRow]) -> list[TreeAgent]:
    """Depth-first order: a parent before its children, siblings by birth time then id.

    An agent whose lineage parent is not among `rows` is a root. A cycle in the historical
    lineage cannot hang the walk: every row is placed once.
    """
    by_id = {row.id: row for row in rows}
    children: dict[int | None, list[AgentRow]] = {}
    for row in rows:
        parent, _ = lineage_parent(row)
        key = parent if parent in by_id and parent != row.id else None
        children.setdefault(key, []).append(row)
    for siblings in children.values():
        siblings.sort(key=lambda r: (r.spawned_at, r.id))
    placed: set[int] = set()
    out: list[TreeAgent] = []

    def walk(parent: int | None, depth: int) -> None:
        for row in children.get(parent, []):
            if row.id in placed:
                continue
            placed.add(row.id)
            _, kind = lineage_parent(row)
            out.append(TreeAgent(row, parent, kind if parent is not None else "root", depth))
            walk(row.id, depth + 1)

    walk(None, 0)
    # A pure cycle has no root; append what the walk never reached, as roots.
    for row in sorted(rows, key=lambda r: (r.spawned_at, r.id)):
        if row.id not in placed:
            placed.add(row.id)
            out.append(TreeAgent(row, None, "root", 0))
            walk(row.id, 1)
    return out


def load_tree(conn: psycopg.Connection[Any], root: int, lineage: Lineage) -> list[TreeAgent]:
    """The tree of `root` under `lineage`. Raises ValueError for an unknown root."""
    ids = select_agents(conn, [root], lineage)
    rows = conn.execute(
        "SELECT id, spawner, born_spawner, fork_source_agent_id, status, spawned_at "
        "FROM agents_meta WHERE id = ANY(%s)",
        (ids,),
    ).fetchall()
    return order_tree([AgentRow(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows])
