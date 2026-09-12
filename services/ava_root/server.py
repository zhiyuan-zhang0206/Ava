"""The K1 control-plane server: one unix socket, one JSON line per request.

The handler passed in owns all business semantics (the supervisor's
`dispatch`); this class owns the transport — read one line, validate it,
forward it, write one line back. A malformed message is answered with an error
response; a message that overruns the line cap breaks the stream boundary and
the connection is dropped instead.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path

from services.ava_root.ipc import (
    MAX_MESSAGE_BYTES,
    ErrorCode,
    ProtocolError,
    RequestPayload,
    ResponsePayload,
    UnknownVerbError,
    encode,
    error_response,
    parse_request,
)

_log = logging.getLogger(__name__)

RequestHandler = Callable[[RequestPayload], Awaitable[ResponsePayload]]


class ControlServer:
    """Serves the control protocol on a unix socket."""

    def __init__(self, socket_path: Path, handler: RequestHandler) -> None:
        self._socket_path = socket_path
        self._handler = handler
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        """Bind the socket.

        The caller owns the instance lock, so a leftover socket file can only
        be a corpse from a previous process and is reclaimed here.
        """
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        self._socket_path.unlink(missing_ok=True)
        self._server = await asyncio.start_unix_server(
            self._serve_connection,
            path=str(self._socket_path),
            limit=MAX_MESSAGE_BYTES,
        )
        self._socket_path.chmod(0o600)
        _log.info("control socket listening at %s", self._socket_path)

    async def close(self) -> None:
        """Stop accepting connections and remove the socket file."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._socket_path.unlink(missing_ok=True)

    async def _serve_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """One connection: read a line, dispatch, write a line."""
        try:
            try:
                raw = await reader.readline()
            except ValueError:
                _log.warning(
                    "control connection dropped: message exceeded %d bytes",
                    MAX_MESSAGE_BYTES,
                )
                return
            if not raw.strip():
                return
            try:
                request = parse_request(raw)
            except UnknownVerbError as exc:
                await self._write(writer, error_response(ErrorCode.UNKNOWN_VERB, str(exc)))
                return
            except ProtocolError as exc:
                await self._write(writer, error_response(ErrorCode.INVALID_REQUEST, str(exc)))
                return
            try:
                response = await self._handler(request)
            except Exception:
                _log.exception("control handler failed for %r", request)
                response = error_response(ErrorCode.INTERNAL, "internal error")
            await self._write(writer, response)
        finally:
            writer.close()
            with suppress(ConnectionError, OSError):
                await writer.wait_closed()

    @staticmethod
    async def _write(writer: asyncio.StreamWriter, response: ResponsePayload) -> None:
        """Write one response line and flush it."""
        writer.write(encode(response))
        await writer.drain()
