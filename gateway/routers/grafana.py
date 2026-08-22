"""Authenticated bounded Grafana proxy and Live bridge — Ava's observability UI edge."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncGenerator
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, HTTPException, Request, Response, WebSocket
from fastapi.responses import RedirectResponse, StreamingResponse
from starlette.types import Receive, Scope, Send
from starlette.websockets import WebSocketState
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.typing import Origin

from gateway.routers._grafana_origin import normalized_origin as _normalized_origin
from shared.cluster_auth import cookie_name, verify_bearer, verify_session
from shared.config import settings

router = APIRouter()
_log = logging.getLogger(__name__)
_AUTH_PROXY_USER_HEADER = "X-Ava-Grafana-User"
_AUTH_PROXY_ROLE_HEADER = "X-Ava-Grafana-Role"
_AUTH_PROXY_USER = "ava-cluster-viewer"
_AUTH_PROXY_ROLE = "Viewer"
_MAX_REQUEST_BODY_BYTES = 2 * 1024 * 1024
_REQUEST_BODY_TIMEOUT_SECONDS = 30.0
MAX_WS_MESSAGE_BYTES = 8 * 1024 * 1024
_MAX_WS_CONNECTIONS = 32
_WRITE_LIMIT = 64 * 1024

# Headers and ordinary response chunks each have a bounded wait. The transport
# read timeout is disabled because a confirmed ``text/event-stream`` response
# may be legitimately quiet; the relay applies the correct policy after seeing
# the trusted upstream Content-Type. Caller-controlled Accept never selects it.
_HEADER_TIMEOUT_SECONDS = 120.0
_CHUNK_TIMEOUT_SECONDS = 120.0
_PROXY_TIMEOUT = httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0)
_MAX_SSE_CONNECTIONS = 4

# A process restart (including cluster-secret rotation) runs lifespan cleanup
# and closes every accepted Grafana Live connection before the old process exits.
_live_websockets: set[WebSocket] = set()
_capacity_lock = threading.Lock()
_reserved_capacity = {"websocket": 0, "sse": 0}


def build_proxy_client() -> httpx.AsyncClient:
    """Build the lifespan-owned upstream pool; no socket opens until use."""
    return httpx.AsyncClient(
        timeout=_PROXY_TIMEOUT,
        limits=httpx.Limits(max_connections=32, max_keepalive_connections=10),
        trust_env=False,
    )


def _upstream_http_origin() -> str:
    host = settings.gateway.grafana_host
    if host != "127.0.0.1":
        raise HTTPException(status_code=500, detail="grafana upstream must be 127.0.0.1")
    return f"http://{host}:{settings.gateway.grafana_port}"


def _upstream_ws_origin() -> str:
    return _upstream_http_origin().replace("http://", "ws://", 1)


_FORWARDED_RESPONSE_HEADERS = frozenset(
    {
        "content-type",
        "content-length",
        "cache-control",
        "etag",
        "content-disposition",
        "accept-ranges",
        "content-security-policy",
        "x-content-type-options",
        "x-frame-options",
    }
)
_FORWARDED_REQUEST_HEADERS = frozenset({"content-type", "accept"})
_ALLOWED_POST_PATHS = frozenset({"api/ds/query"})
_ALLOWED_WS_QUERY_KEYS = frozenset({"orgid"})


def _validate_proxy_rest(rest: str) -> None:
    """Reject path forms that can address a different upstream resource."""
    for segment in rest.split("/"):
        if (
            segment in (".", "..")
            or "\\" in segment
            or any(ord(character) < 0x20 for character in segment)
        ):
            raise HTTPException(
                status_code=400,
                detail=f"invalid grafana path segment {segment!r}",
            )


async def _bounded_request_body(request: Request) -> bytes:
    length = request.headers.get("content-length")
    if length is not None:
        try:
            if int(length) > _MAX_REQUEST_BODY_BYTES:
                raise HTTPException(status_code=413, detail="grafana request body too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid Content-Length") from None

    body = bytearray()
    try:
        async with asyncio.timeout(_REQUEST_BODY_TIMEOUT_SECONDS):
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > _MAX_REQUEST_BODY_BYTES:
                    raise HTTPException(status_code=413, detail="grafana request body too large")
    except TimeoutError:
        raise HTTPException(status_code=408, detail="grafana request body timed out") from None
    return bytes(body)


def _reserve_capacity(counter: str, limit: int) -> bool:
    with _capacity_lock:
        if _reserved_capacity[counter] >= limit:
            return False
        _reserved_capacity[counter] += 1
        return True


def _release_capacity(counter: str) -> None:
    with _capacity_lock:
        if _reserved_capacity[counter] <= 0:
            raise RuntimeError(f"grafana {counter} capacity released without reservation")
        _reserved_capacity[counter] -= 1


def _is_event_stream(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "")
    return content_type.partition(";")[0].strip().lower() == "text/event-stream"


def _proxy_request_headers(request: Request) -> dict[str, str]:
    """Build upstream headers from scratch; caller credentials never survive."""
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() in _FORWARDED_REQUEST_HEADERS
    }
    headers.update(
        {
            "Accept-Encoding": "identity",
            _AUTH_PROXY_USER_HEADER: _AUTH_PROXY_USER,
            _AUTH_PROXY_ROLE_HEADER: _AUTH_PROXY_ROLE,
        }
    )
    return headers


def _safe_redirect(location: str, target: str, upstream_origin: str) -> str:
    """Rewrite an upstream-local redirect to the gateway path; reject others."""
    resolved = urlsplit(urljoin(target, location))
    upstream = urlsplit(upstream_origin)
    try:
        resolved_port = resolved.port
        upstream_port = upstream.port
    except ValueError:
        raise HTTPException(status_code=502, detail="grafana returned an unsafe redirect") from None
    try:
        _validate_proxy_rest(unquote(unquote(resolved.path)))
    except HTTPException:
        raise HTTPException(status_code=502, detail="grafana returned an unsafe redirect") from None
    if (
        resolved.scheme != upstream.scheme
        or resolved.hostname != upstream.hostname
        or resolved_port != upstream_port
        or not (resolved.path == "/grafana" or resolved.path.startswith("/grafana/"))
    ):
        raise HTTPException(status_code=502, detail="grafana returned an unsafe redirect")
    return urlunsplit(("", "", resolved.path, resolved.query, resolved.fragment))


async def _iter_upstream(
    resp: httpx.Response,
    path: str,
    *,
    event_stream: bool,
) -> AsyncGenerator[bytes, None]:
    """Relay bytes and always release the pool slot on EOF or cancellation."""
    try:
        iterator = resp.aiter_bytes().__aiter__()
        while True:
            try:
                if event_stream:
                    chunk = await anext(iterator)
                else:
                    async with asyncio.timeout(_CHUNK_TIMEOUT_SECONDS):
                        chunk = await anext(iterator)
            except StopAsyncIteration:
                break
            yield chunk
    except TimeoutError:
        _log.warning("grafana response stream timed out path=%s", path)
    except httpx.HTTPError:
        _log.warning("grafana response stream failed path=%s", path, exc_info=True)


class _GrafanaStreamingResponse(StreamingResponse):
    """Own upstream cleanup even when response-start fails before iteration."""

    def __init__(
        self,
        content: AsyncGenerator[bytes, None],
        upstream: httpx.Response,
        *,
        event_stream: bool,
        **kwargs: object,
    ) -> None:
        super().__init__(content, **kwargs)  # pyright: ignore[reportArgumentType]
        self._upstream = upstream
        self._event_stream = event_stream
        self._cleaned = False

    async def _cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        try:
            await self._upstream.aclose()
        finally:
            if self._event_stream:
                _release_capacity("sse")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._cleanup()


@router.get("/grafana")
async def grafana_root() -> RedirectResponse:
    """Canonicalize only after Ava's HTTP auth middleware has succeeded."""
    return RedirectResponse("/grafana/", status_code=307)


async def _send_upstream(request: Request, target: str) -> httpx.Response:
    """Open the bounded upstream response, translating transport failures."""
    client: httpx.AsyncClient = request.app.state.grafana_client
    try:
        upstream_request = httpx.Request(
            request.method,
            target,
            headers=_proxy_request_headers(request),
            content=await _bounded_request_body(request),
        )
        async with asyncio.timeout(_HEADER_TIMEOUT_SECONDS):
            return await client.send(upstream_request, stream=True)
    except (TimeoutError, httpx.TimeoutException):
        raise HTTPException(status_code=504, detail="grafana upstream timed out") from None
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="grafana upstream unreachable") from None


async def _response_headers(
    response: httpx.Response,
    target: str,
    upstream_origin: str,
) -> dict[str, str]:
    """Select safe response headers and reject redirects outside Grafana."""
    headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() in _FORWARDED_RESPONSE_HEADERS
    }
    location = response.headers.get("location")
    if location is not None:
        try:
            headers["location"] = _safe_redirect(location, target, upstream_origin)
        except HTTPException:
            await response.aclose()
            raise
    if response.headers.get("content-encoding"):
        headers.pop("content-length", None)
    return headers


async def _streaming_response(
    response: httpx.Response,
    request_path: str,
    headers: dict[str, str],
) -> _GrafanaStreamingResponse:
    """Reserve the optional SSE slot and transfer response ownership."""
    event_stream = _is_event_stream(response)
    if event_stream and not _reserve_capacity("sse", _MAX_SSE_CONNECTIONS):
        await response.aclose()
        raise HTTPException(status_code=503, detail="grafana stream capacity reached")
    return _GrafanaStreamingResponse(
        _iter_upstream(response, request_path, event_stream=event_stream),
        response,
        event_stream=event_stream,
        status_code=response.status_code,
        headers=headers,
    )


async def _proxy(rest: str, request: Request) -> Response:
    _validate_proxy_rest(rest)
    if not settings.gateway.grafana_proxy_enabled:
        raise HTTPException(status_code=404, detail="grafana proxy is disabled")
    if request.method == "POST" and rest not in _ALLOWED_POST_PATHS:
        raise HTTPException(status_code=403, detail="grafana write endpoint is not exposed")

    upstream_origin = _upstream_http_origin()
    target = f"{upstream_origin}/grafana/{rest}"
    if request.url.query:
        target += f"?{request.url.query}"
    response = await _send_upstream(request, target)
    return await _streaming_response(
        response,
        request.url.path,
        await _response_headers(response, target, upstream_origin),
    )


@router.get("/grafana/{rest:path}")
async def grafana_proxy_get(rest: str, request: Request) -> Response:
    """Serve Grafana UI, dashboards, datasource reads, and static assets."""
    return await _proxy(rest, request)


@router.post("/grafana/{rest:path}")
async def grafana_proxy_post(rest: str, request: Request) -> Response:
    """Forward only the datasource-query POST required by the read-only UI."""
    return await _proxy(rest, request)


@router.head("/grafana/{rest:path}")
async def grafana_proxy_head(rest: str, request: Request) -> Response:
    """Forward credentialed asset/dashboard probes."""
    return await _proxy(rest, request)


def _websocket_authenticated(websocket: WebSocket) -> bool:
    secret = settings.data_plane.cluster_secret
    if not settings.gateway.auth_middleware_enabled or not secret:
        return True
    return verify_session(websocket.cookies.get(cookie_name()), secret) or verify_bearer(
        websocket.headers.get("authorization"), secret
    )


def _websocket_origin_allowed(websocket: WebSocket) -> bool:
    """Match a configured public origin exactly, including effective port."""
    origin = websocket.headers.get("origin")
    if origin is None:
        return False
    candidate = _normalized_origin(origin, require_origin_form=True)
    allowed = _normalized_origin(settings.gateway.gateway_url, require_origin_form=False)
    return candidate is not None and candidate == allowed


def _websocket_query_allowed(query: str) -> bool:
    if not query:
        return True
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return False
    return (
        len(pairs) == 1
        and pairs[0][0].lower() in _ALLOWED_WS_QUERY_KEYS
        and pairs[0][1].isascii()
        and pairs[0][1].isdecimal()
        and int(pairs[0][1]) > 0
    )


async def _client_to_grafana(websocket: WebSocket, upstream: ClientConnection) -> None:
    while True:
        message = await websocket.receive()
        kind = message["type"]
        if kind == "websocket.disconnect":
            await upstream.close(code=message.get("code") or 1000)
            return
        payload = message.get("text")
        if payload is None:
            payload = message.get("bytes", b"")
        size = len(payload.encode()) if isinstance(payload, str) else len(payload)
        if size > MAX_WS_MESSAGE_BYTES:
            await upstream.close(code=1009, reason="message too large")
            await websocket.close(code=1009, reason="message too large")
            return
        await upstream.send(payload)


async def _grafana_to_client(websocket: WebSocket, upstream: ClientConnection) -> None:
    try:
        async for message in upstream:
            if isinstance(message, str):
                await websocket.send_text(message)
            else:
                await websocket.send_bytes(message)
    except ConnectionClosed:
        pass  # fail-fast-ok: normal protocol close; relay its code/reason below
    if (
        websocket.client_state is not WebSocketState.DISCONNECTED
        and websocket.application_state is not WebSocketState.DISCONNECTED
    ):
        await websocket.close(
            code=upstream.close_code or 1000,
            reason=upstream.close_reason or "",
        )


async def _relay_websocket(websocket: WebSocket, upstream: ClientConnection) -> None:
    tasks = {
        asyncio.create_task(_client_to_grafana(websocket, upstream)),
        asyncio.create_task(_grafana_to_client(websocket, upstream)),
    }
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        if not task.cancelled():
            task.result()


@router.websocket("/grafana/api/live/ws")
async def grafana_live(websocket: WebSocket) -> None:
    """Bridge Grafana Live without exposing a backend listener or secret query."""
    if not settings.gateway.grafana_proxy_enabled:
        await websocket.close(code=1008, reason="grafana proxy is disabled")
        return
    if not _websocket_authenticated(websocket):
        await websocket.close(code=1008, reason="authentication required")
        return
    if not _websocket_origin_allowed(websocket):
        await websocket.close(code=1008, reason="origin rejected")
        return
    if not _websocket_query_allowed(websocket.url.query):
        await websocket.close(code=1008, reason="credentials are forbidden in query strings")
        return
    if not _reserve_capacity("websocket", _MAX_WS_CONNECTIONS):
        await websocket.close(code=1013, reason="grafana live capacity reached")
        return

    try:
        target = f"{_upstream_ws_origin()}/grafana/api/live/ws"
        if websocket.url.query:
            target += f"?{websocket.url.query}"
        try:
            async with connect(
                target,
                origin=Origin(websocket.headers["origin"]),
                additional_headers={
                    _AUTH_PROXY_USER_HEADER: _AUTH_PROXY_USER,
                    _AUTH_PROXY_ROLE_HEADER: _AUTH_PROXY_ROLE,
                },
                proxy=None,
                open_timeout=5,
                close_timeout=5,
                max_size=MAX_WS_MESSAGE_BYTES,
                max_queue=16,
                write_limit=_WRITE_LIMIT,
            ) as upstream:
                await websocket.accept()
                _live_websockets.add(websocket)
                try:
                    await _relay_websocket(websocket, upstream)
                finally:
                    _live_websockets.discard(websocket)
        except (OSError, TimeoutError, WebSocketException):
            _log.warning("grafana live upstream failed", exc_info=True)
            if (
                websocket.client_state is not WebSocketState.DISCONNECTED
                and websocket.application_state is not WebSocketState.DISCONNECTED
            ):
                await websocket.close(code=1011, reason="grafana live unavailable")
    finally:
        _release_capacity("websocket")


async def close_live_websockets() -> None:
    """Close accepted connections during restart/secret rotation."""
    sockets = tuple(_live_websockets)
    if not sockets:
        return
    await asyncio.gather(
        *(
            socket.close(code=1012, reason="gateway restarting")
            for socket in sockets
            if socket.application_state is not WebSocketState.DISCONNECTED
        ),
        return_exceptions=True,
    )
    _live_websockets.difference_update(sockets)
