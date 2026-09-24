"""Shared client transport for the browser and computer MCP Unix sockets.

Only connection failures and a socket closed before writing can be retried.
Once a write begins, the daemon may have executed the request; errors surface
without resending it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from services.browser.protocol import Response
from shared.config import settings

LINE_LIMIT = 64 * 1024 * 1024
_TRANSPORT_ERRNOS = frozenset({32, 54, 61})  # EPIPE, ECONNRESET, ECONNREFUSED


class NotDeliveredError(Exception):
    """The writer was already closed before this request attempted a write."""


def is_transport_error(exc: BaseException) -> bool:
    """Whether a delivered request failed because its connection died."""
    if type(exc).__name__ in (
        "ConnectionError",
        "BrokenPipeError",
        "ConnectionResetError",
        "ConnectionRefusedError",
        "TimeoutError",
    ):
        return True
    return isinstance(exc, OSError) and getattr(exc, "errno", None) in _TRANSPORT_ERRNOS


async def dial_unix_socket(
    sock: str,
    *,
    service_label: str,
    line_limit: int = LINE_LIMIT,
    attempts: int = 10,
    delay: float = 0.5,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Dial a supervised daemon, allowing a cold start to finish."""
    last: Exception | None = None
    for _ in range(attempts):
        try:
            return await asyncio.open_unix_connection(path=sock, limit=line_limit)
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            last = exc
            await asyncio.sleep(delay)
    raise ConnectionError(f"{service_label} not reachable at {sock}: {last}")


class SocketLink:
    """One connection, with serialized request and response pairs."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        service_label: str,
        extra_fields: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._service_label = service_label
        self._extra_fields = extra_fields
        self._lock = asyncio.Lock()
        self._id = 0

    async def request(self, payload: dict[str, Any]) -> Any:
        async with self._lock:
            self._id += 1
            request = {"id": self._id, **payload}
            if self._extra_fields is not None:
                request.update(self._extra_fields())

            async def roundtrip() -> Any:
                if self._writer.is_closing():
                    raise NotDeliveredError("socket closed before write")
                self._writer.write((json.dumps(request, ensure_ascii=False) + "\n").encode())
                await self._writer.drain()

                line = await self._reader.readline()
                if not line:
                    raise ConnectionError(f"{self._service_label} closed the connection")
                resp: Response = json.loads(line)
                if resp.get("id") != self._id:
                    raise RuntimeError(
                        f"{self._service_label} response id {resp.get('id')} != request {self._id}"
                    )
                if resp["ok"] is False:
                    raise RuntimeError(resp.get("error", f"{self._service_label} error"))
                return resp["result"]

            return await asyncio.wait_for(
                roundtrip(), timeout=settings.sandbox.mcp_connect_timeout_seconds
            )

    def close(self) -> None:
        with suppress(Exception):
            self._writer.close()


class ReconnectingLink:
    """Apply each wrapper's retry and close policy to its socket link."""

    def __init__(
        self,
        connect: Callable[[], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]],
        link_factory: Callable[[asyncio.StreamReader, asyncio.StreamWriter], SocketLink],
        *,
        max_attempts: int,
        base_delay: float,
        retryable_rejection: Callable[[Exception], bool] | None = None,
        close_on_error: Callable[[Exception], bool],
    ) -> None:
        self._connect = connect
        self._link_factory = link_factory
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._retryable_rejection = retryable_rejection
        self._close_on_error = close_on_error
        self._link: SocketLink | None = None
        self._lock = asyncio.Lock()

    async def _connect_once(self) -> SocketLink:
        reader, writer = await self._connect()
        return self._link_factory(reader, writer)

    def _drop_link(self) -> None:
        if self._link is None:
            raise RuntimeError("no connection to close")
        self._link.close()
        self._link = None

    async def request(self, payload: dict[str, Any]) -> Any:
        async with self._lock:
            for attempt in range(self._max_attempts):
                if self._link is None:
                    try:
                        self._link = await self._connect_once()
                    except Exception:
                        if attempt == self._max_attempts - 1:
                            raise
                        await asyncio.sleep(self._base_delay * (2**attempt))
                        continue
                try:
                    return await self._link.request(payload)
                except NotDeliveredError:
                    self._drop_link()
                    if attempt == self._max_attempts - 1:
                        raise
                    await asyncio.sleep(self._base_delay * (2**attempt))
                except Exception as exc:
                    if self._retryable_rejection is not None and self._retryable_rejection(exc):
                        self._drop_link()
                        if attempt == self._max_attempts - 1:
                            raise
                        await asyncio.sleep(self._base_delay * (2**attempt))
                        continue
                    if self._close_on_error(exc):
                        self._drop_link()
                    raise
            raise RuntimeError("unreachable")
