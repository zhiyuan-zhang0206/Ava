"""The agent run timeline — ``GET /api/agents/{agent_id}/run-timeline``.

The data is the two things the agent persists about its own run: its message
history (the checkpoint) and the understanding tree (``understanding_nodes``).
The response is a window over both: every level of the tree intersecting the
window, and below them layer 0, the message units (`base.agents.history.hierarchy.units`).
No window means the agent's whole lifetime — from the earliest message or node to
the latest; drilling a node is asking for its span as the window.

Audit events (spawn, restart, terminate) are laid over the window as lifecycle
markers. They are not a data source: the window never looks at them, and a
failed read of them leaves the markers out.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from base.agents.history.hierarchy.serve import ServedNode, serve_nodes
from base.agents.history.hierarchy.store import load_generation_costs, load_nodes
from base.db import Database
from base.log import logger
from gateway.agents.eval_guard import deny_isolated_result_read
from gateway.run_timeline import _lifecycle
from gateway.run_timeline.context import llm_requests
from gateway.run_timeline.context import router as context_router
from gateway.run_timeline.history import HistoryView, HistoryViewCache
from gateway.run_timeline.messages import router as messages_router
from gateway.run_timeline.schemas import (
    RunTimelineEvent,
    RunTimelineGeneration,
    RunTimelineNode,
    RunTimelineResponse,
    RunTimelineUnit,
    RunTimelineUsage,
    RunTimelineWindow,
)
from gateway.run_timeline.tokens import block_tokens, span_tokens

router = APIRouter()
router.include_router(messages_router)
router.include_router(context_router)

# What the window is when the agent has neither a message nor a node.
_EMPTY_WINDOW = timedelta(hours=24)


def _lifetime(view: HistoryView, nodes: list[ServedNode]) -> tuple[datetime, datetime] | None:
    """The earliest and latest of the agent's messages and understanding nodes (read times)."""
    nodes_extent = (min(n.start for n in nodes), max(n.end for n in nodes)) if nodes else None
    extents = [extent for extent in (view.extent, nodes_extent) if extent is not None]
    if not extents:
        return None
    start = min(extent[0] for extent in extents)
    end = max(extent[1] for extent in extents)
    return start, end if end > start else start + timedelta(seconds=1)


def _covering_leaf(leaves: list[ServedNode], firsts: list[int], index: int) -> str | None:
    """The id of the level-1 node (`leaves` in message order, `firsts` their first messages) whose span holds `index`."""
    at = bisect_right(firsts, index) - 1
    return leaves[at].id if at >= 0 and index <= leaves[at].span_end else None


def _units(
    view: HistoryView, served: list[ServedNode], start: datetime, end: datetime
) -> list[RunTimelineUnit]:
    """The view's blocks intersecting the window, each naming the level-1 node that covers it."""
    leaves = [node for node in served if node.level == 1]
    firsts = [leaf.span_start for leaf in leaves]
    out: list[RunTimelineUnit] = []
    for unit in view.units:
        if unit.start > end or unit.end < start:
            continue
        tokens = block_tokens(view, unit)
        out.append(
            RunTimelineUnit(
                kind=unit.kind,
                i0=unit.i0,
                i1=unit.i1,
                start=unit.start,
                end=unit.end,
                source=unit.source,
                preview=unit.preview,
                parent=_covering_leaf(leaves, firsts, unit.i0),
                context_tokens=tokens.context_tokens,
                generation_tokens=tokens.generation_tokens,
                estimated=tokens.estimated,
            )
        )
    return out


def _node(view: HistoryView, node: ServedNode) -> RunTimelineNode:
    tokens = span_tokens(view, node.span_start, node.span_end)
    return RunTimelineNode(
        id=node.id,
        level=node.level,
        parent=node.parent,
        start=node.start,
        end=node.end,
        span_start=node.span_start,
        span_end=node.span_end,
        summary=node.summary,
        usage=RunTimelineUsage(**vars(node.usage)),
        generation=RunTimelineGeneration(**vars(node.generation)) if node.generation else None,
        context_tokens=tokens.context_tokens,
        estimated=tokens.estimated,
    )


def _window(
    lifetime: tuple[datetime, datetime] | None,
    from_: datetime | None,
    to: datetime | None,
    now: datetime,
) -> tuple[datetime, datetime]:
    for name, value in (("from", from_), ("to", to)):
        if value is not None and value.tzinfo is None:
            raise HTTPException(status_code=422, detail=f"{name} must include a timezone offset")
    default = lifetime or (now - _EMPTY_WINDOW, now)
    start, end = from_ or default[0], to or default[1]
    if start >= end:
        raise HTTPException(status_code=422, detail="from must be earlier than to")
    return start, end


def _events(db: Database, agent_id: int, start: datetime, end: datetime) -> list[RunTimelineEvent]:
    try:
        return _lifecycle.read(db, agent_id, start, end)
    except Exception:
        logger.exception("run-timeline lifecycle read failed for agent {}", agent_id)
        return []


@router.get(
    "/api/agents/{agent_id}/run-timeline",
    dependencies=[Depends(deny_isolated_result_read)],
)
def get_run_timeline(
    request: Request,
    agent_id: int,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
) -> RunTimelineResponse:
    """The understanding tree and the message units in a window; no window means the agent's whole lifetime."""
    db: Database = request.app.state.db
    views: HistoryViewCache = request.app.state.run_timeline_views
    view = views.get(db, agent_id)
    stored = load_nodes(db, agent_id)
    reach = max((node.span_end + 1 for node in stored), default=0)
    if reach > len(view.history.messages):
        # A node written after the cached view was built (or an orphan, which serve_nodes skips).
        view = views.get(db, agent_id, needs=reach)
    job_costs, check_costs = load_generation_costs(db, agent_id)
    served = serve_nodes(stored, view.usage, job_costs, check_costs, view.read, view.units)
    lifetime = _lifetime(view, served)
    start, end = _window(lifetime, from_, to, datetime.now(UTC))
    nodes = [node for node in served if node.start <= end and node.end >= start]
    return RunTimelineResponse(
        agent_id=agent_id,
        window=RunTimelineWindow(from_=start, to=end),
        lifetime=(
            RunTimelineWindow(from_=lifetime[0], to=lifetime[1]) if lifetime is not None else None
        ),
        nodes=[_node(view, node) for node in nodes],
        units=_units(view, served, start, end),
        events=_events(db, agent_id, start, end),
        requests=[r for r in llm_requests(view) if start <= r.ts <= end],
    )
