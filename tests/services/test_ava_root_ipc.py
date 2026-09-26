"""shared.root_control.ipc + server/client: the K1 wire protocol.

One JSON object per line, validated fail-fast on both ends: a malformed
request or a response that drifts from the agreed shape is rejected at the
boundary. The client/server pair is exercised over a real unix socket,
including concurrent callers.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import psutil
import pytest

from services.ava_root.server import ControlServer
from shared.native_process.ownership import OwnedProcess
from shared.root_control.client import RootClient, RootClientError
from shared.root_control.ipc import (
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


def _root_row() -> dict[str, object]:
    owner = OwnedProcess.capture(psutil.Process())
    return {"pid": owner.pid, "create_time": owner.birth, "starttime": owner.starttime}


async def _echo_handler(request: RequestPayload) -> ResponsePayload:
    return ok_response({"echo": request["verb"], "root": _root_row()})


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

    @pytest.mark.parametrize("verb", ["status", "shutdown"])
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
            b'{"verb": "status", "name": null}',
            b'{"verb": "up", "name": "x", "extra": 1}',
            b'{"verb": "up", "name": "x", "payload": {}}',
            b'{"verb": "resource", "name": "terminal.start"}',
            b'{"verb": "resource", "name": "", "payload": {}}',
            b'{"verb": "resource", "name": "terminal.start", "payload": []}',
        ],
    )
    def test_shape_violations_rejected(self, raw: bytes) -> None:
        with pytest.raises(ProtocolError):
            parse_request(raw)

    def test_resource_requires_its_explicit_operation_and_payload(self) -> None:
        request = {"verb": "resource", "name": "terminal.start", "payload": {"name": "shell"}}
        assert parse_request(encode(request)) == request


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
            assert response == {"ok": True, "result": {"echo": "status", "root": _root_row()}}
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
            assert all(
                r == {"ok": True, "result": {"echo": "status", "root": _root_row()}}
                for r in responses
            )
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


@pytest.mark.parametrize("field", ["pid", "create_time", "starttime"])
async def test_status_authenticates_exact_kernel_peer(short_tmp: Path, field: str) -> None:
    """A socket answering the expected protocol cannot substitute another birth."""

    async def forged(_request: RequestPayload) -> ResponsePayload:
        row = _root_row()
        if field == "pid":
            row[field] = int(str(row[field])) + 100000
        elif field == "create_time":
            row[field] = float(str(row[field])) + 1
            row["starttime"] = None
        else:
            row[field] = 1
        return ok_response({"root": row})

    path = short_tmp / "root.sock"
    server = ControlServer(path, forged)
    await server.start()
    try:
        with pytest.raises(RootClientError, match="exact native IPC peer"):
            await asyncio.to_thread(RootClient(path).status)
    finally:
        await server.close()


@pytest.mark.parametrize("bad", [None, "home", "runtime", "launch", "stopped"])
async def test_serving_reads_bound_runtime_from_native_peer(
    short_tmp: Path, monkeypatch: pytest.MonkeyPatch, bad: str | None
) -> None:
    """Real local transport plus strict local receipt; no application launch."""
    from shared import start_serving
    from shared.runtime_interpreter import LoadedRuntimeIdentity

    short_tmp = short_tmp.resolve()
    runtime = LoadedRuntimeIdentity(
        kind="source",
        code_root=str(short_tmp),
        interpreter=str(short_tmp / "python"),
        prefix=str(short_tmp / "venv"),
        cwd=str(short_tmp),
        source_digest="f" * 64,
    )
    monkeypatch.setattr(start_serving, "ava_home", lambda: short_tmp)
    monkeypatch.setattr(start_serving, "state_path", lambda: short_tmp / "serving.json")
    monkeypatch.setattr(start_serving, "_lock_path", lambda: short_tmp / "serving.lock")
    root = _root_row() | {
        "running": True,
        "home": str(short_tmp),
        "launch_digest": "a" * 64,
        "runtime": runtime.model_dump(mode="json"),
    }
    if bad == "home":
        root["home"] = str(short_tmp / "foreign")
    elif bad == "runtime":
        root["runtime"] = None
    elif bad == "launch":
        root["launch_digest"] = None
    elif bad == "stopped":
        root["running"] = False

    async def status(_request: RequestPayload) -> ResponsePayload:
        return ok_response({"root": root})

    path = short_tmp / "run/ava-root/ava-root.sock"
    server = ControlServer(path, status)
    await server.start()
    try:
        generation = start_serving.begin_start()
        if bad is None:
            assert await asyncio.to_thread(start_serving.mark_serving, generation, runtime=runtime)
            assert await asyncio.to_thread(start_serving.is_serving)
        else:
            with pytest.raises((ValueError, RootClientError)):
                await asyncio.to_thread(start_serving.mark_serving, generation, runtime=runtime)
            assert not start_serving.is_serving()
    finally:
        await server.close()


@pytest.mark.parametrize("recorded,captured", [(None, None), (None, 50), (50, None)])
def test_linux_ipc_requires_both_observed_tick_identities(
    monkeypatch: pytest.MonkeyPatch, recorded: int | None, captured: int | None
) -> None:
    from types import SimpleNamespace

    from shared.root_control import client

    monkeypatch.setattr(client, "sys", SimpleNamespace(platform="linux"))

    def unexpected_liveness(_self: OwnedProcess) -> bool:
        pytest.fail("missing Linux ticks reached timestamp-based liveness")

    monkeypatch.setattr(OwnedProcess, "live", unexpected_liveness)
    response = encode(
        ok_response(
            {
                "root": {
                    "pid": 42,
                    "create_time": 123.0,
                    "starttime": recorded,
                }
            }
        )
    )
    with pytest.raises(RootClientError, match="requires observed Linux start ticks"):
        client._validate_status_peer(response, OwnedProcess(42, 123.0, captured))


@pytest.mark.parametrize(
    "platform,recorded,captured,birth,accepted",
    [
        ("linux", 50, 50, 999.0, True),
        ("linux", 51, 50, 123.0, False),
        ("darwin", None, None, 123.0, True),
        ("darwin", None, None, 124.0, False),
    ],
)
def test_ipc_native_birth_rules_preserve_platform_authority(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    recorded: int | None,
    captured: int | None,
    birth: float,
    accepted: bool,
) -> None:
    from types import SimpleNamespace

    from shared.root_control import client

    monkeypatch.setattr(client, "sys", SimpleNamespace(platform=platform))

    def alive(_self: OwnedProcess) -> bool:
        return True

    monkeypatch.setattr(OwnedProcess, "live", alive)
    response = encode(
        ok_response(
            {
                "root": {
                    "pid": 42,
                    "create_time": birth,
                    "starttime": recorded,
                }
            }
        )
    )
    peer = OwnedProcess(42, 123.0, captured)
    if accepted:
        client._validate_status_peer(response, peer)
    else:
        with pytest.raises(RootClientError, match="exact native IPC peer"):
            client._validate_status_peer(response, peer)
