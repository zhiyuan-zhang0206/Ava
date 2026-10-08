"""The cluster view endpoints, mounted in `services/derived/insights/app.py`.

Each takes the same selection — a `root` agent, the `lineage` edges to follow from it
(`base.telemetry.metrics.usage.select_agents`) — and the time window `from` / `to`.
The gateway declares the same parameters and response models
(`gateway/routers/insights.py`) and forwards the request unchanged.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from base.db import Database
from base.telemetry.metrics.usage import Lineage
from services.derived.insights.cluster import curves, lanes, messages
from services.derived.insights.cluster.schemas import ClusterCurves, ClusterLanes, ClusterMessages
from services.derived.insights.cluster.selection import TreeAgent, load_tree
from services.derived.insights.cluster.window import bucket_seconds, parse_window

router = APIRouter()

# Activity bars merge at the width of `bins` equal slices of the window.
_DEFAULT_BINS = 1200


def _tree(db: Database, root: int, lineage: Lineage) -> list[TreeAgent]:
    with db.connect(autocommit=True) as conn:
        try:
            return load_tree(conn, root, lineage)
        except ValueError as exc:
            status = 404 if str(exc).startswith("unknown agent IDs") else 422
            raise HTTPException(status_code=status, detail=str(exc)) from None


@router.get("/api/insights/cluster/curves")
def get_cluster_curves(
    request: Request,
    root: Annotated[int, Query(ge=1)],
    from_: Annotated[datetime, Query(alias="from")],
    to: Annotated[datetime, Query()],
    lineage: Annotated[Lineage, Query()] = "all",
    buckets: Annotated[int, Query(ge=10, le=400)] = 120,
) -> ClusterCurves:
    """Cost by agent, active agents, messages and queue time per time bucket of the window."""
    start, end = parse_window(from_, to)
    db: Database = request.app.state.db
    tree = _tree(db, root, lineage)
    width = bucket_seconds((end - start).total_seconds(), buckets)
    with db.connect(autocommit=True) as conn:
        return curves.read(conn, [a.row.id for a in tree], start, end, width)


@router.get("/api/insights/cluster/lanes")
def get_cluster_lanes(
    request: Request,
    root: Annotated[int, Query(ge=1)],
    from_: Annotated[datetime, Query(alias="from")],
    to: Annotated[datetime, Query()],
    lineage: Annotated[Lineage, Query()] = "all",
    level: Annotated[int | None, Query(ge=1)] = None,
    bins: Annotated[int, Query(ge=50, le=4000)] = _DEFAULT_BINS,
) -> ClusterLanes:
    """One lane per agent in spawn order: understanding nodes of one level and LLM activity bars."""
    start, end = parse_window(from_, to)
    db: Database = request.app.state.db
    tree = _tree(db, root, lineage)
    width = bucket_seconds((end - start).total_seconds(), bins)
    with db.connect(autocommit=True) as conn:
        return lanes.read(conn, tree, start, end, width, level)


@router.get("/api/insights/cluster/messages")
def get_cluster_messages(
    request: Request,
    root: Annotated[int, Query(ge=1)],
    from_: Annotated[datetime, Query(alias="from")],
    to: Annotated[datetime, Query()],
    lineage: Annotated[Lineage, Query()] = "all",
    limit: Annotated[int, Query(ge=1, le=5000)] = 2000,
) -> ClusterMessages:
    """The agent-to-agent messages among the selection, each with its sent and read time."""
    start, end = parse_window(from_, to)
    db: Database = request.app.state.db
    tree = _tree(db, root, lineage)
    with db.connect(autocommit=True) as conn:
        return messages.read(conn, [a.row.id for a in tree], start, end, limit)
