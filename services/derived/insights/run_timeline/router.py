# ava:ref services/derived/insights/run_timeline/docs/range-projection.ava.okf.md#indexed-history-windows
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from services.derived.insights.run_timeline.context import router as context_router
from services.derived.insights.run_timeline.links import router as links_router
from services.derived.insights.run_timeline.messages import router as messages_router
from services.derived.insights.run_timeline.projection.reader import TimelineReader
from services.derived.insights.run_timeline.schemas import (
    RunTimelineResponse,
    RunTimelineWindow,
)

router = APIRouter()
router.include_router(messages_router)
router.include_router(context_router)
router.include_router(links_router)


_EMPTY_WINDOW = timedelta(hours=24)


def _window(
    lifetime: tuple[datetime, datetime] | None,
    from_: datetime | None,
    to: datetime | None,
    now: datetime,
    recent_hours: int | None = None,
) -> tuple[datetime, datetime]:
    for name, value in (("from", from_), ("to", to)):
        if value is not None and value.tzinfo is None:
            raise HTTPException(status_code=422, detail=f"{name} must include a timezone offset")
    default = lifetime or (now - _EMPTY_WINDOW, now)
    end = to or default[1]
    start = from_ or (
        max(default[0], end - timedelta(hours=recent_hours))
        if recent_hours is not None
        else default[0]
    )
    if start >= end:
        raise HTTPException(status_code=422, detail="from must be earlier than to")
    return start, end


@router.get(
    "/api/agents/{agent_id}/run-timeline",
    description="A time window; bounded reads omit dense message detail and name its snapshot.",
)
def get_run_timeline(
    request: Request,
    agent_id: int,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    recent_hours: Annotated[int | None, Query(ge=1, le=168)] = None,
    max_units: Annotated[int | None, Query(ge=1, le=10000)] = None,
    snapshot: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
) -> RunTimelineResponse:
    reader: TimelineReader = request.app.state.run_timeline_reader
    if recent_hours is not None and (from_ is not None or to is not None):
        raise HTTPException(status_code=422, detail="recent_hours cannot be combined with from/to")
    for name, value in (("from", from_), ("to", to)):
        if value is not None and value.tzinfo is None:
            raise HTTPException(422, f"{name} must include a timezone offset")
    if from_ is not None and to is not None and from_ >= to:
        raise HTTPException(422, "from must be earlier than to")
    cut = reader.get(agent_id, snapshot)
    start, end = _window(cut.lifetime, from_, to, datetime.now(UTC), recent_hours)
    nodes, units, omitted = reader.window(cut, start, end, max_units)
    return RunTimelineResponse(
        agent_id=agent_id,
        window=RunTimelineWindow(from_=start, to=end),
        lifetime=RunTimelineWindow(from_=cut.lifetime[0], to=cut.lifetime[1])
        if cut.lifetime
        else None,
        nodes=nodes,
        units=units,
        units_omitted=omitted,
        snapshot=cut.id,
    )
