# meiva:note
# Proxying preserves public paths, queries and upstream statuses, strips browser credentials, rejects isolated callers and keeps gateway/service declarations aligned.
# meiva:link gateway/routers/docs/insights-proxy.meiva.okf.md
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from base.paths import insights_socket
from gateway.agents.eval_guard import deny_isolated_result_read
from services.derived.insights.run_timeline.schemas import (
    RunTimelineContext,
    RunTimelineLinkContent,
    RunTimelineLinks,
    RunTimelineMessages,
    RunTimelineResponse,
)

router = APIRouter()


_TIMEOUT = httpx.Timeout(connect=2.0, read=120.0, write=5.0, pool=5.0)


_FORWARDED_REQUEST_HEADERS = frozenset({"accept"})
_FORWARDED_RESPONSE_HEADERS = frozenset({"content-type", "cache-control", "etag"})


def build_client(home: Path) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(
            uds=str(insights_socket(home)), limits=httpx.Limits(max_connections=32)
        ),
        base_url="http://insights",
        timeout=_TIMEOUT,
        trust_env=False,
    )


def _validate_rest(rest: str) -> None:
    for seg in rest.split("/"):
        if seg in (".", "..") or "\\" in seg or any(ord(c) < 0x20 for c in seg):
            raise HTTPException(status_code=400, detail=f"invalid insights path segment {seg!r}")


async def _forward(request: Request, path: str) -> Response:
    target = path + (f"?{request.url.query}" if request.url.query else "")
    headers = {k: v for k, v in request.headers.items() if k.lower() in _FORWARDED_REQUEST_HEADERS}
    client: httpx.AsyncClient = request.app.state.insights_client
    try:
        resp = await client.get(target, headers=headers)
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="insights service timed out") from None
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="insights service unreachable") from None
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers={k: v for k, v in resp.headers.items() if k.lower() in _FORWARDED_RESPONSE_HEADERS},
    )


@router.get(
    "/api/agents/{agent_id}/run-timeline/messages",
    response_model=RunTimelineMessages,
    dependencies=[Depends(deny_isolated_result_read)],
    description="Messages ``start..end`` of the agent's stitched history, at most ``limit`` of them.",
)
async def get_run_timeline_messages(
    request: Request,
    agent_id: int,
    _start: Annotated[int, Query(alias="start", ge=0)],
    _end: Annotated[int, Query(alias="end", ge=0)],
    _limit: Annotated[int, Query(alias="limit", ge=1, le=200)] = 50,
    *,
    _full: Annotated[bool, Query(alias="full")] = False,
    _snapshot: Annotated[str | None, Query(alias="snapshot", min_length=1, max_length=64)] = None,
) -> Response:
    return await _forward(request, f"/api/agents/{agent_id}/run-timeline/messages")


@router.get(
    "/api/agents/{agent_id}/run-timeline/context",
    response_model=RunTimelineContext,
    dependencies=[Depends(deny_isolated_result_read)],
    description="The context breakdown of the LLM request at (or next after) message index `at`.",
)
async def get_run_timeline_context(
    request: Request,
    agent_id: int,
    _at: Annotated[int, Query(alias="at", ge=0)],
    _snapshot: Annotated[str | None, Query(alias="snapshot", min_length=1, max_length=64)] = None,
) -> Response:
    return await _forward(request, f"/api/agents/{agent_id}/run-timeline/context")


@router.get(
    "/api/agents/{agent_id}/run-timeline",
    response_model=RunTimelineResponse,
    dependencies=[Depends(deny_isolated_result_read)],
    description="The understanding tree and the message units in a window; no window means the agent's whole lifetime.",
)
async def get_run_timeline(
    request: Request,
    agent_id: int,
    _from: Annotated[datetime | None, Query(alias="from")] = None,
    _to: Annotated[datetime | None, Query(alias="to")] = None,
    _recent_hours: Annotated[int | None, Query(alias="recent_hours", ge=1, le=168)] = None,
    _max_units: Annotated[int | None, Query(alias="max_units", ge=1, le=10000)] = None,
    _snapshot: Annotated[str | None, Query(alias="snapshot", min_length=1, max_length=64)] = None,
) -> Response:
    return await _forward(request, f"/api/agents/{agent_id}/run-timeline")


@router.get(
    "/api/insights/run-timeline/links",
    response_model=RunTimelineLinks,
    dependencies=[Depends(deny_isolated_result_read)],
    description="The agent-to-agent events in a window with an end among the comma-separated `agents`.",
)
async def get_run_timeline_links(
    request: Request,
    _agents: Annotated[str, Query(alias="agents")],
    _from: Annotated[datetime, Query(alias="from")],
    _to: Annotated[datetime, Query(alias="to")],
) -> Response:
    return await _forward(request, "/api/insights/run-timeline/links")


@router.get(
    "/api/insights/run-timeline/link-content",
    response_model=RunTimelineLinkContent,
    dependencies=[Depends(deny_isolated_result_read)],
    description="The full text of one chat message (`inbound_id`) or the title and text of one notice (`notice_id`).",
)
async def get_run_timeline_link_content(
    request: Request,
    _inbound_id: Annotated[int | None, Query(alias="inbound_id", ge=1)] = None,
    _notice_id: Annotated[int | None, Query(alias="notice_id", ge=1)] = None,
) -> Response:
    return await _forward(request, "/api/insights/run-timeline/link-content")


@router.get(
    "/api/insights/{rest:path}",
    include_in_schema=False,
    dependencies=[Depends(deny_isolated_result_read)],
    description="GET /api/insights/{rest} \u2014 forward to the insights service (routes not yet typed here).",
)
async def insights_proxy_get(rest: str, request: Request) -> Response:
    _validate_rest(rest)
    return await _forward(request, f"/api/insights/{quote(rest, safe='/')}")
