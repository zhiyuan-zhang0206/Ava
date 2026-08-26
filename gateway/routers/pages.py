"""ava.ui.show / .close page endpoints + the page reverse proxy.

URLs:
- `/api/agents/{agent_id}/pages` — register / list / close (registry).
- `/pages/{agent_id}-{name}/...` — reverse proxy (one page per agent):
  the gateway forwards GETs to the agent's page server (host:port from
  the registry) and returns the content.
The browser never dials the page server directly — the registered URL is
the gateway's own, auth-gated by the cluster middleware (session cookie /
bearer) like every other API route.

The proxy streams the upstream response chunk-by-chunk (StreamingResponse)
— SSE and arbitrarily large files pass through without being buffered whole.
"""

from __future__ import annotations

import asyncio
import html as _html
import ipaddress
import re as _re
import threading
import time
from collections.abc import AsyncGenerator
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse, StreamingResponse
from psycopg_pool import ConnectionPool
from starlette.background import BackgroundTask

from gateway.schemas import PageRegisterRequest
from ops.pages import (
    close_all_agent_pages,
    close_page,
    get_open_page_target,
    list_all_open_pages,
    list_open_pages,
    register_page,
)
from ops.rpc_schemas import PageRow
from shared.agents import AgentStatus
from shared.config import settings
from shared.db import agent_exists
from shared.live_events import PageClosed, PageOpened
from shared.redis_client import publish_best_effort

router = APIRouter()

# Reverse-proxy dialing: connect fails fast (5s) when the page server is
# down; a stalled transfer (no bytes for 120s) is severed so a hung backend
# cannot park the request forever. The read timeout is between-chunks, so a
# live SSE stream or a slow large download keeps flowing.
_PROXY_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=5.0, pool=5.0)
_PAGE_HOST_CACHE_TTL_S = 60.0
_page_host_cache: dict[str, tuple[float, frozenset[str]]] = {}
_page_host_cache_lock = threading.Lock()


def _EXPIRED_PAGE_HTML(agent_id: int, name: str) -> str:  # noqa: N802 — fixed contract name
    """Small self-contained response for links whose page TTL elapsed."""
    escaped_name = _html.escape(name)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Page expired</title></head><body><main>"
        "<h1>Page expired</h1>"
        "<p>页面已过期，请让 agent 重新 serve</p>"  # noqa: RUF001 — PM-specified user-facing copy (fullwidth comma is part of the text)
        f"<p>Page {escaped_name} · agent {agent_id}</p>"
        "</main></body></html>"
    )


# Response headers forwarded from the page server; everything else
# (hop-by-hop, server internals) is dropped. Content-Length is forwarded too
# (the upstream sets it for static files); an absent one (SSE) makes
# Starlette fall back to chunked transfer.
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


def _absolute_url(request: Request, path: str) -> str:
    """Render a gateway-relative path as the absolute URL the browser dials.

    The base is the configured Gateway URL (`AVA_GATEWAY_URL` /
    `$AVA_HOME/gateway_url`), NOT the request's own Host: the SDK dials the
    gateway at its local loopback URL on a single box, so absolutizing
    against the request would hand every SDK caller a `localhost` link that
    is unreachable from the user's other devices. The Gateway URL variable is
    the single source of truth for how the gateway is reached from outside —
    whatever the caller's dial address is (localhost / VPN overlay / LAN), the
    delivered URL is the same for everyone.

    Falls back to the request base when no Gateway URL is configured (tests /
    unprovisioned checkout) — a gateway that serves pages is expected to be
    configured, so this is a dev-only convenience, not a supported mode.
    """
    base = settings.gateway.gateway_url.strip().rstrip("/")
    if not base:
        base = str(request.base_url).rstrip("/")
    return base + path


def _absolutize(request: Request, record: PageRow) -> PageRow:
    """Replace a PageRow's gateway-relative url with the absolute form."""
    return record.model_copy(update={"url": _absolute_url(request, record.url)})


def _validate_proxy_rest(rest: str) -> None:
    """Reject path segments that could escape the page's served directory.

    Defense in depth: the page server (SimpleHTTPRequestHandler) skips `..`
    segments itself, and `name` is already restricted to ^[a-zA-Z0-9_-]+$;
    this keeps a crafted path from even reaching the backend. Rejects
    `.` / `..` segments, backslashes (Windows separators), and control
    characters.
    """
    for seg in rest.split("/"):
        if seg in (".", "..") or "\\" in seg or any(ord(c) < 0x20 for c in seg):
            raise HTTPException(status_code=400, detail=f"invalid page path segment {seg!r}")


async def _publish_page_event(event: PageOpened | PageClosed) -> None:
    """Publish a page lifecycle event to the events channel — best-effort, never raises."""
    await publish_best_effort(
        settings.data_plane.events_channel, event.model_dump_json(), context="page_event"
    )


@router.post("/api/agents/{agent_id}/pages", status_code=201, response_model=PageRow)
async def post_page_register(agent_id: int, body: PageRegisterRequest, request: Request) -> PageRow:
    """ava.ui.show register / update a page — SDK main entry.

    Each agent can have at most one open page. Before registering the new
    page, any existing open pages for this agent are auto-closed (one
    agent, one page). The closed pages get individual PageClosed events
    so the frontend removes them from the popover.

    Returns 409 when agent is terminated — dead agents do not show UI.
    """
    record, closed_names = await asyncio.to_thread(
        _register_page_blocking,
        request.app.state.db_pool,
        agent_id,
        body,
    )
    record = _absolutize(request, record)
    # Publish PageClosed events for any pages that were auto-closed.
    for name in closed_names:
        await _publish_page_event(PageClosed(agent_id=agent_id, name=name))
    await _publish_page_event(
        PageOpened(
            agent_id=agent_id,
            page_id=record.id,
            name=record.name,
            port=record.port,
            title=record.title,
            url=record.url,
        )
    )
    return record


@router.delete("/api/agents/{agent_id}/pages/{name}")
async def delete_page(agent_id: int, name: str, request: Request) -> PageRow:
    """ava.ui.close close a page — CAS open->closed.

    404: agent does not exist / page name does not exist / already closed
    (single 404 does not distinguish them; caller only cares that "the
    target page is no longer in open state").
    """
    record = await asyncio.to_thread(
        _close_page_blocking, request.app.state.db_pool, agent_id, name
    )
    record = _absolutize(request, record)
    await _publish_page_event(PageClosed(agent_id=agent_id, name=name))
    return record


@router.get("/api/pages", response_model=list[PageRow])
def get_all_pages(request: Request) -> list[PageRow]:
    """Fleet view page-load: all agents' currently open pages in one fetch, so
    each fleet row can surface its agent's panels without a per-row request.
    SSE PageOpened/PageClosed handles deltas."""
    with request.app.state.db_pool.connection() as conn:
        return [_absolutize(request, r) for r in list_all_open_pages(conn)]


@router.get("/api/agents/{agent_id}/pages", response_model=list[PageRow])
def get_pages(agent_id: int, request: Request) -> list[PageRow]:
    """UI page-load fetches the agent's currently open page list; SSE PageOpened/Closed handles deltas."""
    with request.app.state.db_pool.connection() as conn:
        if not agent_exists(conn, agent_id):
            raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
        return [_absolutize(request, r) for r in list_open_pages(conn, agent_id)]


async def _iter_upstream(resp: httpx.Response) -> AsyncGenerator[bytes, None]:
    """Stream the upstream body chunk-by-chunk; an upstream drop mid-body
    ends the stream (the client sees EOF / a truncated body) instead of
    crashing the response."""
    try:
        async for chunk in resp.aiter_bytes():
            yield chunk
    except httpx.HTTPError:
        return


async def _close_upstream(resp: httpx.Response, client: httpx.AsyncClient) -> None:
    """Release the upstream connection + client once the streamed response
    has been sent (or the client disconnected)."""
    await resp.aclose()
    await client.aclose()


async def _proxy_page_get_impl(agent_id: int, name: str, rest: str, request: Request) -> Response:
    """Reverse-proxy one GET to the agent's page server, streaming.

    The page must be currently open (registry lookup by agent + name). 404
    for unknown/closed page or missing agent; 502 when the page server is
    unreachable; 504 on a stalled transfer. Auth-gated by the cluster
    middleware like every other API route.

    Streaming (not buffering): SSE and arbitrarily large files pass through
    chunk-by-chunk — the gateway never holds a whole body in memory.
    """
    _validate_proxy_rest(rest)
    target_row = await asyncio.to_thread(
        _page_target_blocking, request.app.state.db_pool, agent_id, name
    )
    if target_row is None:
        expired = await asyncio.to_thread(
            _page_expired_blocking, request.app.state.db_pool, agent_id, name
        )
        if expired:
            return Response(
                status_code=410,
                media_type="text/html",
                content=_EXPIRED_PAGE_HTML(agent_id, name),
            )
        raise HTTPException(status_code=404, detail=f"page {name!r} not open (agent {agent_id})")
    host, port = target_row
    await asyncio.to_thread(_validate_proxy_page_target, request.app.state.db_pool, agent_id, host)
    target = f"http://{host}:{port}/{rest}"
    query = request.url.query
    if query:
        target += f"?{query}"
    client = httpx.AsyncClient(timeout=_PROXY_TIMEOUT)
    try:
        req = client.build_request("GET", target)
        resp = await client.send(req, stream=True)
    except httpx.TimeoutException:
        await client.aclose()
        raise HTTPException(
            status_code=504, detail=f"page server {host}:{port} timed out"
        ) from None
    except httpx.HTTPError:
        await client.aclose()
        raise HTTPException(
            status_code=502, detail=f"page server {host}:{port} unreachable"
        ) from None
    headers = {k: v for k, v in resp.headers.items() if k.lower() in _FORWARDED_RESPONSE_HEADERS}
    return StreamingResponse(
        _iter_upstream(resp),
        status_code=resp.status_code,
        headers=headers,
        background=BackgroundTask(_close_upstream, resp, client),
    )


# --- Page URL: /pages/<agent_id>-<name>/ ---
# One page per agent is the mental model: the URL carries the agent, the
# name is secondary. The composite key is parseable because agent_id is
# numeric — split on the FIRST dash even when the name contains dashes.

_PAGE_KEY_RE = _re.compile(r"^(\d+)-(.*)$")


def _parse_page_key(page_key: str) -> tuple[int, str] | None:
    """Split `<agent_id>-<name>` on the first dash → (agent_id, name).

    None for a malformed key (non-numeric prefix, no dash).
    """
    m = _PAGE_KEY_RE.match(page_key)
    if m is None:
        return None
    return int(m.group(1)), m.group(2)


@router.get("/pages/{page_key}")
async def proxy_page_root_new(page_key: str) -> Response:
    """Trailing-slash canonicalization: `/pages/1-foo` redirects to
    `/pages/1-foo/` so relative asset links resolve."""
    return RedirectResponse(f"/pages/{page_key}/", status_code=307)


@router.get("/pages/{page_key}/{rest:path}")
async def proxy_page_get_new(page_key: str, rest: str, request: Request) -> Response:
    """Page URL reverse-proxy: `/pages/<agent_id>-<name>/...`.

    404 for a malformed key, unknown/closed page, or missing agent.
    """
    parsed = _parse_page_key(page_key)
    if parsed is None:
        raise HTTPException(
            status_code=404, detail=f"invalid page key {page_key!r} (expected <agent_id>-<name>)"
        )
    agent_id, name = parsed
    return await _proxy_page_get_impl(agent_id, name, rest, request)


def _is_loopback_host(host: str) -> bool:
    """Whether `host` names the local machine (localhost / 127.0.0.1 / ::1).

    The literal names plus an ipaddress check so every loopback spelling
    (127.0.0.0/8, ::1, IPv4-mapped ::ffff:127.x) counts as loopback.
    """
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def reset_page_host_cache_for_tests() -> None:
    """Clear memoized machine dial hosts between isolated page-proxy tests."""
    with _page_host_cache_lock:
        _page_host_cache.clear()


def _agent_machine(pool: ConnectionPool, agent_id: int) -> str | None:
    """Return a page agent's registered home machine, if it is usable."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT machine FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    machine = row[0] if row is not None else None
    return str(machine) if machine and machine != "unknown" else None


def _machine_dial_hosts(pool: ConnectionPool, machine: str) -> frozenset[str]:
    """Return the short-lived cached host allowlist advertised by one machine."""
    now = time.monotonic()
    with _page_host_cache_lock:
        cached = _page_host_cache.get(machine)
        if cached is not None and cached[0] > now:
            return cached[1]
    hosts = {machine}
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT url FROM machine_units "
            "WHERE machine_name = %s AND stopped_at IS NULL AND url IS NOT NULL",
            (machine,),
        )
        for (url,) in cur.fetchall():
            hostname = urlparse(str(url)).hostname
            if hostname:
                hosts.add(hostname)
    allowed = frozenset(hosts)
    with _page_host_cache_lock:
        _page_host_cache[machine] = (now + _PAGE_HOST_CACHE_TTL_S, allowed)
    return allowed


def _validate_nonloopback_page_host(
    pool: ConnectionPool,
    agent_id: int,
    host: str,
    *,
    status_code: int,
) -> None:
    """Fail closed unless `host` is this agent machine's advertised address."""
    machine = _agent_machine(pool, agent_id)
    if machine is None:
        raise HTTPException(
            status_code=status_code,
            detail=(
                f"page host {host!r} is not loopback and agent {agent_id}'s home "
                "machine is unknown — refusing to proxy to it"
            ),
        )
    if host not in _machine_dial_hosts(pool, machine):
        raise HTTPException(
            status_code=status_code,
            detail=(
                f"page host {host!r} is neither loopback nor {machine!r}'s "
                "registered address — refusing to proxy to it"
            ),
        )


def _validate_proxy_page_target(pool: ConnectionPool, agent_id: int, host: str) -> None:
    """Revalidate non-loopback registry rows immediately before proxy dialing.

    Registration validates new rows, but ops writes may predate or bypass that
    path. This short-lived allowlist is read from the same machine registry and
    fails closed before the gateway makes an untrusted network connection.
    """
    if not _is_loopback_host(host):
        _validate_nonloopback_page_host(pool, agent_id, host, status_code=403)


def _validate_page_dial_target(pool: ConnectionPool, agent_id: int, host: str, port: int) -> None:
    """SSRF guard for the page reverse proxy (audit round-2 P1-4).

    The gateway dials `http://{host}:{port}/...` for any registered page, so
    a compromised agent must not be able to point the proxy at arbitrary
    addresses (another machine's services, or the cluster's own Postgres /
    Redis / IM bridge / agent-ops on loopback-adjacent ports — a standing
    internal-network probe+access primitive). The dial target is restricted
    to:

    - loopback (localhost / 127.0.0.1 / ::1) — the single-box posture, where
      the page server binds the machine's own address; and
    - the registering agent's home machine — its `agents_meta.machine` name
      or any host its `machine_units` rows advertise (`url`), i.e. the
      address the cluster itself uses to reach that machine.

    A port below 1024 is rejected outright: a page server is an unprivileged
    user-space HTTP server, and privileged ports are the system's own
    services. Raises HTTPException 400/404; register_page is never reached
    with a foreign target.
    """
    if port < 1024:
        raise HTTPException(
            status_code=400,
            detail=f"page port {port} is privileged — page servers run on ports >= 1024",
        )
    if _is_loopback_host(host):
        return
    _validate_nonloopback_page_host(pool, agent_id, host, status_code=400)


def _register_page_blocking(
    pool: ConnectionPool, agent_id: int, body: PageRegisterRequest
) -> tuple[PageRow, list[str]]:
    """Sync page-register transaction — via to_thread: agent-exists + status
    guard, auto-close of prior pages, registry INSERT. Returns (record,
    closed_names)."""
    _validate_page_dial_target(pool, agent_id, body.host, body.port)
    with pool.connection() as conn:
        if not agent_exists(conn, agent_id):
            raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
            row = cur.fetchone()
        if row is not None and row[0] == AgentStatus.TERMINATED:
            raise HTTPException(
                status_code=409,
                detail=f"agent {agent_id} is terminated, cannot register page",
            )
        # Close any existing open pages for this agent before registering a new one.
        closed_names = close_all_agent_pages(conn, agent_id)
        record = register_page(
            conn,
            agent_id,
            body.name,
            body.port,
            body.host,
            body.title,
            body.serve_dir,
            body.ttl_seconds,
        )
    return record, closed_names


def _close_page_blocking(pool: ConnectionPool, agent_id: int, name: str) -> PageRow:
    """Sync page-close CAS — via to_thread (404 guards included)."""
    with pool.connection() as conn:
        if not agent_exists(conn, agent_id):
            raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
        record = close_page(conn, agent_id, name)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail=f"page {name!r} not found or already closed (agent {agent_id})",
            )
    return record


def _page_target_blocking(pool: ConnectionPool, agent_id: int, name: str) -> tuple[str, int] | None:
    """Sync open-page target lookup — via to_thread (the proxy dial path)."""
    with pool.connection() as conn:
        return get_open_page_target(conn, agent_id, name)


def _page_expired_blocking(pool: ConnectionPool, agent_id: int, name: str) -> bool:
    """Whether the newest registry row for this page name expired."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT expired_at IS NOT NULL FROM agent_pages "
            "WHERE agent_id = %s AND name = %s ORDER BY id DESC LIMIT 1",
            (agent_id, name),
        )
        row = cur.fetchone()
    return bool(row[0]) if row is not None else False
