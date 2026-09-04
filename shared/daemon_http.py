"""Settings-free HTTP transport shared by normal and bootstrap daemon modes.

Callers validate their own unit/image and provide explicit bind/auth policy.
This transport never initializes Settings, registers a unit, or opens a database.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping

from shared.cluster_auth import verify_bearer

RouteHandler = Callable[[bytes], Awaitable[tuple[int, bytes, str]]]
_MAX_BODY_BYTES = 64 * 1024
_log = logging.getLogger("shared.daemon_http")


def _header_value(header_lines: list[bytes], name: bytes) -> str | None:
    """Extract a header value by (case-insensitive) name from raw request lines.
    `header_lines[0]` is the request line, so the scan starts at index 1."""
    prefix = name.lower() + b":"
    for h in header_lines[1:]:
        if h.lower().startswith(prefix):
            return h.split(b":", 1)[1].strip().decode("latin-1")
    return None


def _content_length(header_lines: list[bytes]) -> int:
    """Declared body size, 0 when absent or unparseable (a malformed
    Content-Length is treated as "no body" rather than raising — the peer may be
    any process that got the port)."""
    for h in header_lines[1:]:
        if h.lower().startswith(b"content-length:"):
            try:
                return int(h.split(b":", 1)[1].strip())
            except ValueError:
                return 0
    return 0


def _build_response(status: int, body: bytes, content_type: str) -> bytes:
    reason = {
        200: "OK",
        400: "Bad Request",
        401: "Unauthorized",
        404: "Not Found",
        500: "Internal Server Error",
        503: "Service Unavailable",
    }.get(status, "Unknown")
    return (
        f"HTTP/1.1 {status} {reason}\r\n".encode("ascii")
        + f"Content-Type: {content_type}\r\n".encode("ascii")
        + f"Content-Length: {len(body)}\r\n".encode("ascii")
        + b"Connection: close\r\n\r\n"
        + body
    )


async def start_daemon_http(
    *,
    host: str,
    port: int,
    health_response: Callable[[], tuple[int, bytes]],
    extra_routes: Mapping[tuple[str, str], RouteHandler] | None = None,
    auth_token: str | None = None,
) -> asyncio.Server:
    """Bind only the explicitly supplied routes and health response."""
    routes = dict(extra_routes) if extra_routes else {}

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            # Client disconnected early / did not send valid HTTP — silent drop;
            # the caller (healthcheck / SDK) takes the outer fallback. The
            # finally still closes our writer.
            with contextlib.suppress(
                TimeoutError,
                asyncio.IncompleteReadError,
                ConnectionError,
                asyncio.LimitOverrunError,
            ):
                data = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2.0)
                header_lines = data.split(b"\r\n")
                request_line = header_lines[0].decode("ascii", errors="replace")
                parts = request_line.split(" ")
                method = parts[0] if parts else ""
                path = parts[1] if len(parts) > 1 else ""

                # POST body — read Content-Length-specified bytes, with a cap.
                body_bytes = b""
                content_length = _content_length(header_lines)
                if content_length > _MAX_BODY_BYTES:
                    response = _build_response(
                        400,
                        json.dumps(
                            {"error": f"body too large: {content_length} > {_MAX_BODY_BYTES}"}
                        ).encode(),
                        "application/json",
                    )
                else:
                    if content_length > 0:
                        body_bytes = await asyncio.wait_for(
                            reader.readexactly(content_length), timeout=10.0
                        )

                    if method == "GET" and path == "/healthz":
                        status, healthz_body = health_response()
                        response = _build_response(status, healthz_body, "application/json")
                    elif (method, path) in routes:
                        if auth_token is not None and not verify_bearer(
                            _header_value(header_lines, b"authorization"), auth_token
                        ):
                            response = _build_response(
                                401,
                                json.dumps({"error": "unauthorized"}).encode(),
                                "application/json",
                            )
                        else:
                            try:
                                status, resp_body, ctype = await routes[(method, path)](body_bytes)
                                response = _build_response(status, resp_body, ctype)
                            except Exception as exc:
                                _log.exception(
                                    "[daemon_health] route %s %s handler raised", method, path
                                )
                                response = _build_response(
                                    500,
                                    json.dumps({"error": type(exc).__name__}).encode(),
                                    "application/json",
                                )
                    else:
                        response = _build_response(404, b"", "text/plain")

                writer.write(response)
                await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    # Default bind 127.0.0.1 — healthcheck / local RPC; no LAN port. The
    # agent-runner ops server overrides host=0.0.0.0 so the gateway can
    # reach its POST /ops route over the private network (same posture as the gateway).
    return await asyncio.start_server(handle, host=host, port=port)
