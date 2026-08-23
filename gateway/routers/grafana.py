"""Grafana reverse proxy — `/grafana/*` -> a co-located Grafana instance.

The Grafana web app (dashboard iframes, its `/api/*` calls, static assets)
is served through the gateway so the browser never dials Grafana directly:
one authenticated entry point, same posture as the agent page reverse proxy
(`gateway/routers/pages.py`). Auth is the cluster middleware (session cookie /
bearer) like every other API route; Grafana itself runs in anonymous-viewer
mode (read-only dashboards), so no Grafana-side credentials are forwarded.

URLs:
- `GET /grafana` — 307 to `/grafana/` so relative asset links inside the app
  resolve against `/grafana/`, not the gateway API root.
- `GET|POST|PUT|PATCH|DELETE|HEAD /grafana/{rest:path}` — forwarded to
  `http://{grafana_host}:{grafana_port}/grafana/{rest}` (prefix preserved;
  Grafana runs `serve_from_sub_path=true` so it serves the sub-path directly
  instead of 301-looping on the root). POST covers
  Grafana's datasource-query API (`/api/ds/query`), which the dashboard frontend
  calls same-origin through this proxy.

The proxy streams the upstream response chunk-by-chunk (StreamingResponse) —
SSE and large panel payloads pass through without being buffered whole. All
proxied requests share one `httpx.AsyncClient` (connection pooling instead of
a client per request), created and closed by the gateway lifespan
(`gateway/app.py`) and reached via `request.app.state.grafana_client`.

Disabled by default (`AVA_GRAFANA_PROXY_ENABLED=false`): a cluster without a
Grafana upstream gets 404 on `/grafana/*` and nothing else changes. WebSocket
endpoints (Grafana Live) are not proxied — out of scope for the read-only
dashboard embed.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse, StreamingResponse
from starlette.background import BackgroundTask

from shared.config import settings

router = APIRouter()

# Reverse-proxy dialing: connect fails fast (5s) when Grafana is down; a
# stalled transfer (no bytes for 120s) is severed so a hung upstream cannot
# park the request forever. The read timeout is between-chunks, so a live
# stream keeps flowing.
_PROXY_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=5.0, pool=5.0)

# Grafana datasource queries are JSON requests. Buffering lets httpx replay
# their exact body upstream, but never accept an unbounded client upload.
_MAX_PROXY_REQUEST_BODY_BYTES = 16 * 1024 * 1024


def build_proxy_client() -> httpx.AsyncClient:
    """The shared upstream client — one connection pool for every proxied
    request. Owned by the gateway lifespan (created at startup, closed at
    shutdown); handlers reach it via `request.app.state.grafana_client`."""
    return httpx.AsyncClient(
        timeout=_PROXY_TIMEOUT,
        limits=httpx.Limits(max_connections=32, max_keepalive_connections=10),
    )


# Response headers forwarded from Grafana; everything else (hop-by-hop,
# server internals, Set-Cookie) is dropped. Content-Length is forwarded too
# (Grafana sets it for static assets); an absent one (SSE) makes Starlette
# fall back to chunked transfer.
_FORWARDED_RESPONSE_HEADERS = frozenset(
    {
        "content-type",
        "content-length",
        "cache-control",
        "etag",
        "content-disposition",
        "accept-ranges",
    }
)

# Request headers forwarded to Grafana. Everything else (Host, Cookie,
# Authorization, hop-by-hop) is dropped — the browser's session belongs to
# the gateway, and Grafana's anonymous viewer needs neither.
_FORWARDED_REQUEST_HEADERS = frozenset({"content-type", "accept"})


def _validate_proxy_rest(rest: str) -> None:
    """Reject path segments that could confuse the upstream router.

    Defense in depth: Grafana parses the request path itself, and an encoded
    dot-segment or backslash could address a different resource than the
    browser asked for. Rejects `.` / `..` segments, backslashes (Windows
    separators), and control characters.
    """
    for seg in rest.split("/"):
        if seg in (".", "..") or "\\" in seg or any(ord(c) < 0x20 for c in seg):
            raise HTTPException(status_code=400, detail=f"invalid grafana path segment {seg!r}")


async def _iter_upstream(resp: httpx.Response) -> AsyncGenerator[bytes, None]:
    """Stream the upstream body chunk-by-chunk; an upstream drop mid-body
    ends the stream (the client sees EOF / a truncated body) instead of
    crashing the response."""
    try:
        async for chunk in resp.aiter_bytes():
            yield chunk
    except httpx.HTTPError:
        return


async def _close_upstream(resp: httpx.Response) -> None:
    """Release the upstream connection back to the shared pool once the
    streamed response has been sent (or the client disconnected)."""
    await resp.aclose()


async def _read_proxy_request_body(request: Request) -> bytes:
    """Read a forwardable request body up to the Grafana proxy's 16 MiB cap."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > _MAX_PROXY_REQUEST_BODY_BYTES:
            raise HTTPException(status_code=413, detail="grafana request body exceeds 16 MiB")
        chunks.append(chunk)
    return b"".join(chunks)


@router.get("/grafana")
async def grafana_root() -> RedirectResponse:
    """Trailing-slash canonicalization: `/grafana` redirects to `/grafana/`
    so the app's relative asset links resolve against the proxy root."""
    return RedirectResponse("/grafana/", status_code=307)


async def _proxy(rest: str, request: Request) -> Response:
    """Reverse-proxy one request to the co-located Grafana instance, streaming.

    Only active when `settings.gateway.grafana_proxy_enabled` is on — off (the
    default) yields 404 so a cluster without Grafana is unaffected. 502 when
    the upstream is unreachable; 504 on a stalled transfer. Auth-gated by the
    cluster middleware like every other API route.

    Upstream responses stream chunk-by-chunk. Forwarded request bodies are
    buffered only up to 16 MiB, then rejected with 413, so a caller cannot
    force an unbounded gateway allocation.
    """
    _validate_proxy_rest(rest)
    if not settings.gateway.grafana_proxy_enabled:
        raise HTTPException(status_code=404, detail="grafana proxy is disabled")
    host = settings.gateway.grafana_host
    port = settings.gateway.grafana_port
    # Keep the /grafana prefix on the forwarded path: Grafana runs with
    # serve_from_sub_path=true (root_url = <gateway>/grafana), so it serves
    # sub-path requests directly. Stripping the prefix (the old behaviour)
    # made Grafana see a root-path request, which it answers with a 301 back
    # to root_url — a redirect loop through the proxy. Preserving the prefix
    # is also the Grafana-documented reverse-proxy layout.
    target = f"http://{host}:{port}/grafana/{rest}"
    query = request.url.query
    if query:
        target += f"?{query}"
    client: httpx.AsyncClient = request.app.state.grafana_client
    try:
        # Forward the whitelisted request headers (Content-Type matters for
        # POST /api/ds/query bodies) and ask for an uncompressed response so
        # the forwarded Content-Length stays exact: httpx auto-decodes gzip/br
        # upstream but leaves the original headers in place, and forwarding a
        # decoded body under a stale Content-Encoding/Content-Length would
        # corrupt the response.
        fwd_headers = {
            k: v for k, v in request.headers.items() if k.lower() in _FORWARDED_REQUEST_HEADERS
        }
        fwd_headers["Accept-Encoding"] = "identity"
        req = client.build_request(
            request.method,
            target,
            headers=fwd_headers,
            content=await _read_proxy_request_body(request),
        )
        resp = await client.send(req, stream=True)
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail=f"grafana {host}:{port} timed out") from None
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail=f"grafana {host}:{port} unreachable") from None
    headers = {k: v for k, v in resp.headers.items() if k.lower() in _FORWARDED_RESPONSE_HEADERS}
    # Defense in depth for an upstream that ignores Accept-Encoding: identity:
    # httpx delivers the decoded body, so a forwarded Content-Encoding or its
    # now-wrong Content-Length would corrupt the client-side decode. Drop both.
    if resp.headers.get("content-encoding"):
        headers.pop("content-length", None)
    return StreamingResponse(
        _iter_upstream(resp),
        status_code=resp.status_code,
        headers=headers,
        background=BackgroundTask(_close_upstream, resp),
    )


# One endpoint per HTTP method (not a multi-method api_route) so each gets a
# unique OpenAPI operationId — a shared function registered for six methods
# makes FastAPI emit one duplicated operationId for all of them, which
# poisons the codegen-fresh check and the generated frontend types.
@router.get("/grafana/{rest:path}")
async def grafana_proxy_get(rest: str, request: Request) -> Response:
    """GET /grafana/{rest} — forward to Grafana (dashboard pages, static assets)."""
    return await _proxy(rest, request)


@router.post("/grafana/{rest:path}")
async def grafana_proxy_post(rest: str, request: Request) -> Response:
    """POST /grafana/{rest} — forward (Grafana's datasource-query API)."""
    return await _proxy(rest, request)


@router.put("/grafana/{rest:path}")
async def grafana_proxy_put(rest: str, request: Request) -> Response:
    """PUT /grafana/{rest} — forward."""
    return await _proxy(rest, request)


@router.patch("/grafana/{rest:path}")
async def grafana_proxy_patch(rest: str, request: Request) -> Response:
    """PATCH /grafana/{rest} — forward."""
    return await _proxy(rest, request)


@router.delete("/grafana/{rest:path}")
async def grafana_proxy_delete(rest: str, request: Request) -> Response:
    """DELETE /grafana/{rest} — forward."""
    return await _proxy(rest, request)


@router.head("/grafana/{rest:path}")
async def grafana_proxy_head(rest: str, request: Request) -> Response:
    """HEAD /grafana/{rest} — forward (asset probes)."""
    return await _proxy(rest, request)
