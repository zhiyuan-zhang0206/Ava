"""services.ava_root.ipc + server/client: the K1 wire protocol.

One JSON object per line, validated fail-fast on both ends: a malformed
request or a response that drifts from the agreed shape is rejected at the
boundary. The client/server pair is exercised over a real unix socket,
including concurrent callers.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from services.ava_root.client import RootClient, RootClientError
from services.ava_root.ipc import (
    ErrorCode,
    ProtocolError,
    RequestPayload,
    ResponsePayload,
    UnknownVerbError,
    encode,
    error_response,
    ok_response,
    parse_request,
    parse_response,
)
from services.ava_root.server import ControlServer


async def _echo_handler(request: RequestPayload) -> ResponsePayload:
    return ok_response({"echo": request["verb"]})


class TestEncode:
    def test_encode_is_one_compact_line(self) -> None:
        assert encode({"verb": "status"}) == b'{"verb":"status"}\n'

    def test_encode_escapes_non_ascii(self) -> None:
        raw = encode({"error": "\u00e9t\u00e9"})
        assert raw.decode("ascii")
        assert json.loads(raw) == {"error": "\u00e9t\u00e9"}


class TestParseRequest:
    def test_named_verb(self) -> None:
        assert parse_request(b'{"verb": "up", "name": "gateway"}\n') == {
            "verb": "up",
            "name": "gateway",
        }

    @pytest.mark.parametrize("verb", ["status", "upgrade"])
    def test_unnamed_verb(self, verb: str) -> None:
        assert parse_request(encode({"verb": verb})) == {"verb": verb}

    @pytest.mark.parametrize("raw", [b"not json", b"[1]", b"{}", b'{"verb": 3}'])
    def test_malformed_requests_rejected(self, raw: bytes) -> None:
        with pytest.raises(ProtocolError):
            parse_request(raw)

    def test_unknown_verb_raises_unknown_verb(self) -> None:
        with pytest.raises(UnknownVerbError):
            parse_request(b'{"verb": "explode"}')

    @pytest.mark.parametrize(
        "raw",
        [
            b'{"verb": "up"}',
            b'{"verb": "up", "name": ""}',
            b'{"verb": "up", "name": 3}',
            b'{"verb": "down"}',
            b'{"verb": "restart"}',
            b'{"verb": "status", "name": "x"}',
            b'{"verb": "up", "name": "x", "extra": 1}',
        ],
    )
    def test_shape_violations_rejected(self, raw: bytes) -> None:
        with pytest.raises(ProtocolError):
            parse_request(raw)


class TestParseResponse:
    def test_ok_response_roundtrip(self) -> None:
        payload = ok_response({"tree": "snapshot"})
        assert parse_response(encode(payload)) == {"ok": True, "result": {"tree": "snapshot"}}

    def test_error_response_roundtrip(self) -> None:
        payload = error_response(ErrorCode.UNKNOWN_UNIT, "unknown unit 'x'")
        assert parse_response(encode(payload)) == {
            "ok": False,
            "error": "unknown unit 'x'",
            "code": "unknown_unit",
        }

    @pytest.mark.parametrize(
        "raw",
        [
            b"nope",
            b'{"ok": "yes"}',
            b'{"ok": false}',
            b'{"ok": false, "error": "x", "code": "made-up"}',
            b'{"ok": true, "unknown_field": 1}',
        ],
    )
    def test_malformed_responses_rejected(self, raw: bytes) -> None:
        with pytest.raises(ProtocolError):
            parse_response(raw)


class TestServerClientRoundtrip:
    async def test_roundtrip_over_unix_socket(self, short_tmp: Path) -> None:
        sock_path = short_tmp / "root.sock"
        server = ControlServer(sock_path, _echo_handler)
        await server.start()
        try:
            client = RootClient(sock_path, timeout=5.0)
            response = await asyncio.to_thread(client.call, "status")
            assert response == {"ok": True, "result": {"echo": "status"}}
        finally:
            await server.close()
        assert not sock_path.exists()

    async def test_concurrent_calls(self, short_tmp: Path) -> None:
        sock_path = short_tmp / "root.sock"
        server = ControlServer(sock_path, _echo_handler)
        await server.start()
        try:
            client = RootClient(sock_path, timeout=5.0)
            responses = await asyncio.gather(
                *(asyncio.to_thread(client.call, "status") for _ in range(16))
            )
            assert all(r == {"ok": True, "result": {"echo": "status"}} for r in responses)
        finally:
            await server.close()

    async def test_malformed_message_gets_error_response(self, short_tmp: Path) -> None:
        sock_path = short_tmp / "root.sock"
        server = ControlServer(sock_path, _echo_handler)
        await server.start()
        try:
            reader, writer = await asyncio.open_unix_connection(str(sock_path))
            writer.write(b"not json\n")
            await writer.drain()
            raw = await reader.readline()
            writer.close()
            parsed = parse_response(raw)
            assert parsed["ok"] is False
            assert parsed.get("code") == ErrorCode.INVALID_REQUEST.value
        finally:
            await server.close()

    async def test_unknown_verb_gets_unknown_verb_code(self, short_tmp: Path) -> None:
        sock_path = short_tmp / "root.sock"
        server = ControlServer(sock_path, _echo_handler)
        await server.start()
        try:
            async with asyncio.timeout(5.0):
                reader, writer = await asyncio.open_unix_connection(str(sock_path))
                writer.write(encode({"verb": "explode"}))
                await writer.drain()
                raw = await reader.readline()
                writer.close()
            parsed = parse_response(raw)
            assert parsed.get("code") == ErrorCode.UNKNOWN_VERB.value
        finally:
            await server.close()

    def test_unreachable_socket_raises(self, short_tmp: Path) -> None:
        client = RootClient(short_tmp / "nobody.sock", timeout=2.0)
        with pytest.raises(RootClientError, match="unreachable"):
            client.status()

    async def test_oversized_message_is_dropped(self, short_tmp: Path) -> None:
        """A line over the cap breaks the stream boundary; the server hangs up."""
        sock_path = short_tmp / "root.sock"
        server = ControlServer(sock_path, _echo_handler)
        await server.start()
        try:
            async with asyncio.timeout(5.0):
                reader, writer = await asyncio.open_unix_connection(str(sock_path))
                writer.write(b'{"verb": "status", "pad": "' + b"x" * 70_000 + b'"}\n')
                await writer.drain()
                raw = await reader.read()
            assert raw == b""  # connection dropped without a response
            writer.close()
        finally:
            await server.close()
