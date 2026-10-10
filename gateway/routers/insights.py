"""Insights reverse proxy — the read models served by the insights service.

The run timeline and the cluster insights are built by `services/derived/insights`, a
daemon of their own: rebuilding an agent's history is seconds of CPU that must not
compete with the gateway's event loop, thread pool or GIL. The browser still dials only
the gateway. This module authenticates nothing itself (the cluster middleware has already
admitted the caller, like every `/api` route); it applies the one policy that reads a
caller's identity (`deny_isolated_result_read`) and forwards the request over the
service's Unix socket (`base.paths.insights_socket`).

The URL is the public one, unchanged: `/api/agents/{id}/run-timeline[/messages|/context]`,
`/api/insights/run-timeline/links` and everything under `/api/insights/`. The typed routes declare the service's query
parameters and response model so the gateway's OpenAPI (and the generated frontend types)
describe them; the service owns validation, and a request the gateway's declaration
rejects never reaches it. A route added to the service is proxied by declaring it here
the same way; `/api/insights/{rest}` forwards the rest of that namespace untyped.

Responses are buffered, not streamed: the service replies with one finite JSON document.
502 and 504 mean the service is down or slow (`ava status` shows it as `insights`);
every other status is the service's own and passes through with its body.
"""

from __future__ import annotations

from datetime import datetime
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

# A cold build of a long-lived agent's history takes seconds; the read timeout bounds a hung
# service, connect fails fast when the socket has no listener.
_TIMEOUT = httpx.Timeout(connect=2.0, read=120.0, write=5.0, pool=5.0)

# Only what the service reads is forwarded; the browser's cookies and credentials belong to
# the gateway.
_FORWARDED_REQUEST_HEADERS = frozenset({"accept"})
_FORWARDED_RESPONSE_HEADERS = frozenset({"content-type", "cache-control", "etag"})


def build_client() -> httpx.AsyncClient:
    """The shared upstream client — one pool for every proxied request; the gateway
    lifespan creates and closes it, handlers reach it as `request.app.state.insights_client`."""
    return httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(
            uds=str(insights_socket()), limits=httpx.Limits(max_connections=32)
        ),
        base_url="http://insights",
        timeout=_TIMEOUT,
        trust_env=False,
    )


def _validate_rest(rest: str) -> None:
    """Reject path segments that could address another route of the service."""
    for seg in rest.split("/"):
        if seg in (".", "..") or "\\" in seg or any(ord(c) < 0x20 for c in seg):
            raise HTTPException(status_code=400, detail=f"invalid insights path segment {seg!r}")


async def _forward(request: Request, path: str) -> Response:
    """GET `path` (plus the request's query string, untouched) from the insights service."""
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
)
async def get_run_timeline_messages(
    request: Request,
    agent_id: int,
    start: Annotated[int, Query(ge=0)],  # noqa: ARG001
    end: Annotated[int, Query(ge=0)],  # noqa: ARG001
    limit: Annotated[int, Query(ge=1, le=200)] = 50,  # noqa: ARG001
    full: Annotated[bool, Query()] = False,  # noqa: ARG001, FBT002 — FastAPI query param
) -> Response:
    """Messages ``start..end`` of the agent's stitched history, at most ``limit`` of them."""
    return await _forward(request, f"/api/agents/{agent_id}/run-timeline/messages")


@router.get(
    "/api/agents/{agent_id}/run-timeline/context",
    response_model=RunTimelineContext,
    dependencies=[Depends(deny_isolated_result_read)],
)
async def get_run_timeline_context(
    request: Request,
    agent_id: int,
    at: Annotated[int, Query(ge=0)],  # noqa: ARG001
) -> Response:
    """The context breakdown of the LLM request at (or next after) message index `at`."""
    return await _forward(request, f"/api/agents/{agent_id}/run-timeline/context")


@router.get(
    "/api/agents/{agent_id}/run-timeline",
    response_model=RunTimelineResponse,
    dependencies=[Depends(deny_isolated_result_read)],
)
async def get_run_timeline(
    request: Request,
    agent_id: int,
    from_: Annotated[datetime | None, Query(alias="from")] = None,  # noqa: ARG001
    to: Annotated[datetime | None, Query()] = None,  # noqa: ARG001
) -> Response:
    """The understanding tree and the message units in a window; no window means the agent's whole lifetime."""
    return await _forward(request, f"/api/agents/{agent_id}/run-timeline")


@router.get(
    "/api/insights/run-timeline/links",
    response_model=RunTimelineLinks,
    dependencies=[Depends(deny_isolated_result_read)],
)
async def get_run_timeline_links(
    request: Request,
    agents: Annotated[str, Query()],  # noqa: ARG001
    from_: Annotated[datetime, Query(alias="from")],  # noqa: ARG001
    to: Annotated[datetime, Query()],  # noqa: ARG001
) -> Response:
    """The agent-to-agent events in a window with an end among the comma-separated `agents`."""
    return await _forward(request, "/api/insights/run-timeline/links")


@router.get(
    "/api/insights/run-timeline/link-content",
    response_model=RunTimelineLinkContent,
    dependencies=[Depends(deny_isolated_result_read)],
)
async def get_run_timeline_link_content(
    request: Request,
    inbound_id: Annotated[int | None, Query(ge=1)] = None,  # noqa: ARG001
    notice_id: Annotated[int | None, Query(ge=1)] = None,  # noqa: ARG001
) -> Response:
    """The full text of one chat message (`inbound_id`) or the title and text of one notice (`notice_id`)."""
    return await _forward(request, "/api/insights/run-timeline/link-content")


@router.get(
    "/api/insights/{rest:path}",
    include_in_schema=False,
    dependencies=[Depends(deny_isolated_result_read)],
)
async def insights_proxy_get(rest: str, request: Request) -> Response:
    """GET /api/insights/{rest} — forward to the insights service (routes not yet typed here)."""
    _validate_rest(rest)
    return await _forward(request, f"/api/insights/{quote(rest, safe='/')}")
