"""The K1 local control server: one JSON line per request over a socket or pipe.

The handler passed in owns all business semantics (the supervisor's
`dispatch`); this class owns the transport — read one line, validate it,
forward it, write one line back. A malformed message is answered with an error
response; a message that overruns the line cap breaks the stream boundary and
the connection is dropped instead.

An optional `after_response` hook starts daemon shutdown after the accepted
response is delivered. Resource payloads can contain child environments and
must never appear in request logging.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path

from shared.root_control.ipc import (
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
from shared.root_control.windows.transport import PipeServer

_log = logging.getLogger(__name__)

RequestHandler = Callable[[RequestPayload], Awaitable[ResponsePayload]]


class ControlServer:
    """Serves the control protocol over the platform's private local transport."""

    def __init__(
        self,
        socket_path: Path,
        handler: RequestHandler,
        *,
        after_response: Callable[[RequestPayload], None] | None = None,
    ) -> None:
        self._socket_path = socket_path
        self._handler = handler
        self._after_response = after_response
        self._server: asyncio.AbstractServer | None = None
        self._pipe: PipeServer | None = None

    async def start(self) -> None:
        """Bind the socket.

        The caller owns the instance lock, so a leftover socket file can only
        be a corpse from a previous process and is reclaimed here.
        """
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            self._pipe = PipeServer(self._socket_path, self._serve_bytes, self._after_bytes)
            await self._pipe.start()
            return
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
        if self._pipe is not None:
            await self._pipe.close()
            self._pipe = None
            return
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._socket_path.unlink(missing_ok=True)

    async def _serve_bytes(self, raw: bytes) -> bytes:
        """Transport-neutral validation and business dispatch for the native pipe."""
        try:
            request = parse_request(raw)
        except UnknownVerbError as exc:
            return encode(error_response(ErrorCode.UNKNOWN_VERB, str(exc)))
        except ProtocolError as exc:
            return encode(error_response(ErrorCode.INVALID_REQUEST, str(exc)))
        return encode(await self._invoke(request))

    async def _invoke(self, request: RequestPayload) -> ResponsePayload:
        try:
            return await self._handler(request)
        except Exception as exc:
            if request["verb"] == "resource":
                # Exception text can itself contain a rejected environment value.
                _log.error("resource handler failed (%s)", type(exc).__name__)
            else:
                _log.exception("control handler failed for verb %s", request["verb"])
            return error_response(ErrorCode.INTERNAL, "internal error")

    def _after_bytes(self, raw: bytes) -> None:
        if self._after_response is not None:
            with suppress(ProtocolError):
                self._after_response(parse_request(raw))

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
            response = await self._invoke(request)
            await self._write(writer, response)
            if self._after_response is not None:
                self._after_response(request)
        finally:
            writer.close()
            with suppress(ConnectionError, OSError):
                await writer.wait_closed()

    @staticmethod
    async def _write(writer: asyncio.StreamWriter, response: ResponsePayload) -> None:
        """Write one response line and flush it."""
        writer.write(encode(response))
        await writer.drain()
