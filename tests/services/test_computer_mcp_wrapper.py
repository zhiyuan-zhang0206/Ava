"""Unit tests for the computer MCP bridge's socket link.

Covers _Link (request/response framing + agent-id stamping) and
_ReconnectingLink (automatic reconnect on transport errors). The MCP stdio
surface itself is a thin pass-through (same shape as the browser wrapper),
exercised against a live daemon in dev-cluster testing.
"""

from __future__ import annotations

import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from services.computer.mcp_wrapper import _Link, _ReconnectingLink
from services.permissions_helper import client
from services.permissions_helper.client import PermissionsHelperError
from shared import resilience
from shared.config import settings


class FakeWriter:
    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.closed = False

    def write(self, b: bytes) -> None:
        self.written.append(b)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed


class FailingWriter(FakeWriter):
    def __init__(self, fail_stage: str) -> None:
        super().__init__()
        self.fail_stage = fail_stage

    def write(self, b: bytes) -> None:
        if self.fail_stage == "write":
            raise BrokenPipeError("socket broke")
        super().write(b)

    async def drain(self) -> None:
        if self.fail_stage == "drain":
            raise BrokenPipeError("socket broke")


class FakeReader:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


class HangingReader(FakeReader):
    async def readline(self) -> bytes:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def _line(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj) + "\n").encode()


@pytest.fixture
def no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _sleep(_delay: float) -> None:
        pass

    monkeypatch.setattr("services.computer.mcp_wrapper.asyncio.sleep", _sleep)
    monkeypatch.setattr(resilience, "_asleep", _sleep)


async def test_request_returns_result_on_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AVA_AGENT_ID", "42")
    reader = FakeReader([_line({"id": 1, "ok": True, "result": {"x": 1}})])
    writer = FakeWriter()
    link = _Link(reader, writer)  # type: ignore[arg-type]
    assert await link.request({"method": "list_tools"}) == {"x": 1}
    sent = json.loads(writer.written[0])
    assert sent["agent_id"] == 42  # identity stamped on every request


async def test_request_without_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)
    reader = FakeReader([_line({"id": 1, "ok": True, "result": None})])
    writer = FakeWriter()
    link = _Link(reader, writer)  # type: ignore[arg-type]
    await link.request({"method": "call_tool", "tool": "click", "args": {}})
    sent = json.loads(writer.written[0])
    assert sent["agent_id"] is None
    assert sent["tool"] == "click"


async def test_agent_identity_is_injected_for_each_request(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = FakeReader(
        [
            _line({"id": 1, "ok": True, "result": None}),
            _line({"id": 2, "ok": True, "result": None}),
        ]
    )
    writer = FakeWriter()
    link = _Link(reader, writer)  # type: ignore[arg-type]
    monkeypatch.setenv("AVA_AGENT_ID", "7")
    await link.request({"method": "list_tools", "agent_id": 99})
    monkeypatch.delenv("AVA_AGENT_ID")
    await link.request({"method": "call_tool", "tool": "click"})
    assert [json.loads(line)["agent_id"] for line in writer.written] == [7, None]


async def test_request_raises_on_error_response() -> None:
    reader = FakeReader([_line({"id": 1, "ok": False, "error": "quota exceeded"})])
    link = _Link(reader, FakeWriter())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="quota exceeded"):
        await link.request({"method": "call_tool", "tool": "x", "args": {}})


async def test_request_raises_on_closed_connection() -> None:
    link = _Link(FakeReader([]), FakeWriter())  # type: ignore[arg-type]
    with pytest.raises(ConnectionError, match="closed"):
        await link.request({"method": "list_tools"})


async def test_request_raises_on_id_mismatch() -> None:
    reader = FakeReader([_line({"id": 99, "ok": True, "result": {}})])
    link = _Link(reader, FakeWriter())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="response id"):
        await link.request({"method": "list_tools"})


@pytest.mark.parametrize("fail_stage", ["write", "drain"])
async def test_request_preserves_unknown_delivery_error(fail_stage: str) -> None:
    writer = FailingWriter(fail_stage)
    link = _Link(FakeReader([]), writer)  # type: ignore[arg-type]
    with pytest.raises(BrokenPipeError, match="socket broke"):
        await link.request({"method": "call_tool", "tool": "click", "args": {}})
    assert len(writer.written) == (1 if fail_stage == "drain" else 0)


# ── _ReconnectingLink ───────────────────────────────────────────────────────


async def test_reconnecting_link_redials_on_connection_error(
    no_retry_delay: None,
) -> None:
    attempts = 0
    writer = FakeWriter()

    async def _connect_once() -> _Link:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionRefusedError("daemon not listening")
        return _Link(FakeReader([_line({"id": 1, "ok": True, "result": "ok"})]), writer)  # type: ignore[arg-type]

    rl = _ReconnectingLink()
    rl._connect_once = _connect_once  # type: ignore[method-assign]
    assert await rl.request({"method": "ping"}) == "ok"
    assert attempts == 2
    assert len(writer.written) == 1


async def test_reconnecting_link_never_retries_delivered_call(
    no_retry_delay: None,
) -> None:
    """A request whose response never arrives may have executed on the desktop —
    retrying could double-click. The error surfaces and the link reconnects."""

    attempts = 0
    writers: list[FakeWriter] = []

    async def _connect_once() -> _Link:
        nonlocal attempts
        attempts += 1
        writer = FakeWriter()
        writers.append(writer)
        reader = (
            FakeReader([])
            if attempts == 1
            else FakeReader([_line({"id": 1, "ok": True, "result": "next"})])
        )
        return _Link(reader, writer)  # type: ignore[arg-type]

    rl = _ReconnectingLink()
    rl._connect_once = _connect_once  # type: ignore[method-assign]
    with pytest.raises(ConnectionError):
        await rl.request({"method": "call_tool", "tool": "click", "args": {}})
    assert attempts == 1
    assert [len(writer.written) for writer in writers] == [1]
    assert writers[0].closed
    assert await rl.request({"method": "list_tools"}) == "next"
    assert attempts == 2


@pytest.mark.parametrize("fail_stage", ["write", "drain"])
async def test_reconnecting_link_does_not_retry_write_or_drain_failure(
    no_retry_delay: None, fail_stage: str
) -> None:
    attempts = 0
    failed_writer = FailingWriter(fail_stage)
    success_writer = FakeWriter()

    async def _connect_once() -> _Link:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return _Link(FakeReader([]), failed_writer)  # type: ignore[arg-type]
        return _Link(FakeReader([_line({"id": 1, "ok": True, "result": "ok"})]), success_writer)  # type: ignore[arg-type]

    rl = _ReconnectingLink()
    rl._connect_once = _connect_once  # type: ignore[method-assign]
    with pytest.raises(BrokenPipeError, match="socket broke"):
        await rl.request({"method": "call_tool", "tool": "type", "args": {}})
    assert attempts == 1
    assert failed_writer.closed
    assert len(success_writer.written) == 0
    assert len(failed_writer.written) == (1 if fail_stage == "drain" else 0)
    assert await rl.request({"method": "list_tools"}) == "ok"
    assert attempts == 2


@pytest.mark.parametrize(
    "line,error",
    [
        (b"bad json\n", json.JSONDecodeError),
        (_line({"id": 99, "ok": True, "result": "wrong"}), RuntimeError),
    ],
)
async def test_reconnecting_link_does_not_retry_bad_response(
    line: bytes, error: type[Exception]
) -> None:
    attempts = 0
    writer = FakeWriter()

    async def _connect_once() -> _Link:
        nonlocal attempts
        attempts += 1
        return _Link(FakeReader([line]), writer)  # type: ignore[arg-type]

    rl = _ReconnectingLink()
    rl._connect_once = _connect_once  # type: ignore[method-assign]
    with pytest.raises(error):
        await rl.request({"method": "call_tool", "tool": "click", "args": {}})
    assert attempts == 1
    assert len(writer.written) == 1
    assert writer.closed


async def test_reconnecting_link_times_out_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.sandbox, "mcp_connect_timeout_seconds", 0.01)
    attempts = 0
    writer = FakeWriter()

    async def _connect_once() -> _Link:
        nonlocal attempts
        attempts += 1
        return _Link(HangingReader([]), writer)  # type: ignore[arg-type]

    rl = _ReconnectingLink()
    rl._connect_once = _connect_once  # type: ignore[method-assign]
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            rl.request({"method": "call_tool", "tool": "click", "args": {}}), timeout=0.2
        )
    assert attempts == 1
    assert len(writer.written) == 1
    assert writer.closed


# The computer MCP daemon uses this helper; pin its nested connection budgets here.


def test_connect_exhaustion_closes_every_socket_without_trailing_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sockets: list[Mock] = []
    sleeps: list[float] = []

    def make_socket(*_args: object) -> Mock:
        sock = Mock()
        sock.connect.side_effect = FileNotFoundError("absent")
        sockets.append(sock)
        return sock

    monkeypatch.setattr(client.socket, "socket", make_socket)
    monkeypatch.setattr(client.time, "sleep", sleeps.append)
    monkeypatch.setattr(resilience, "_sleep", sleeps.append)
    with pytest.raises(PermissionsHelperError) as error:
        client._connect("test.sock")
    assert str(error.value) == "permissions helper not reachable at test.sock: absent"
    assert error.value.__context__ is None
    assert len(sockets) == 5
    assert all(sock.close.call_count == 1 for sock in sockets)
    assert sleeps == [0.2] * 4


def test_helper_connect_non_retryable_error_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sock = Mock()
    failure = PermissionError("forbidden")
    sock.connect.side_effect = failure
    factory = Mock(return_value=sock)
    monkeypatch.setattr(client.socket, "socket", factory)
    with pytest.raises(PermissionError) as error:
        client._connect("test.sock")
    assert error.value is failure
    assert factory.call_count == 1
    sock.close.assert_not_called()


def test_helper_socket_creation_failure_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    failure = FileNotFoundError("socket factory failed")
    factory = Mock(side_effect=failure)
    monkeypatch.setattr(client.socket, "socket", factory)
    with pytest.raises(FileNotFoundError) as error:
        client._connect("test.sock")
    assert error.value is failure
    assert factory.call_count == 1


def test_helper_failed_socket_close_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    sock = Mock()
    sock.connect.side_effect = FileNotFoundError("socket absent")
    failure = ConnectionRefusedError("close failed")
    sock.close.side_effect = failure
    factory = Mock(return_value=sock)
    monkeypatch.setattr(client.socket, "socket", factory)
    with pytest.raises(ConnectionRefusedError) as error:
        client._connect("test.sock")
    assert error.value is failure
    assert factory.call_count == 1


def test_pipe_outer_connect_exhaustion_has_no_trailing_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.permissions_helper import _win_pipe

    sleeps: list[float] = []
    connect = Mock(side_effect=ConnectionError("pipe absent"))
    monkeypatch.setattr(_win_pipe, "connect", connect)
    monkeypatch.setattr(client.time, "sleep", sleeps.append)
    monkeypatch.setattr(resilience, "_sleep", sleeps.append)
    with pytest.raises(PermissionsHelperError) as error:
        client._call_pipe({"id": 1, "method": "ping"})
    assert str(error.value) == (f"permissions helper not reachable at pipe {_win_pipe.PIPE_NAME!r}")
    assert error.value.__context__ is None
    assert connect.call_count == 5
    assert sleeps == [0.2] * 4


def test_pipe_outer_non_retryable_error_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.permissions_helper import _win_pipe

    failure = RuntimeError("bad pipe API")
    connect = Mock(side_effect=failure)
    monkeypatch.setattr(_win_pipe, "connect", connect)
    with pytest.raises(RuntimeError) as error:
        client._call_pipe({"id": 1, "method": "ping"})
    assert error.value is failure
    assert connect.call_count == 1


@pytest.mark.parametrize("errno,attempts", [(2, 5), (121, 5), (5, 1)])
def test_win_pipe_wait_retry_errno_and_terminal_message(
    monkeypatch: pytest.MonkeyPatch, errno: int, attempts: int
) -> None:
    from services.permissions_helper import _win_pipe

    wait = Mock(return_value=False)
    kernel32 = SimpleNamespace(WaitNamedPipeW=wait, CreateFileW=Mock())
    sleeps: list[float] = []
    monkeypatch.setitem(sys.modules, "msvcrt", ModuleType("msvcrt"))

    def fake_windll(_name: str, *, use_last_error: bool) -> SimpleNamespace:
        assert use_last_error
        return kernel32

    monkeypatch.setattr(_win_pipe.ctypes, "WinDLL", fake_windll, raising=False)
    monkeypatch.setattr(_win_pipe.ctypes, "get_last_error", lambda: errno, raising=False)
    monkeypatch.setattr(_win_pipe.time, "sleep", sleeps.append)
    monkeypatch.setattr(resilience, "_sleep", sleeps.append)
    with pytest.raises(ConnectionError) as error:
        _win_pipe.connect("test-pipe")
    assert str(error.value) == "permissions helper not reachable at pipe 'test-pipe'"
    assert error.value.__context__ is None
    assert wait.call_count == attempts
    assert sleeps == [0.2] * (attempts - 1)
    kernel32.CreateFileW.assert_not_called()


def test_pipe_outer_retry_waits_for_each_inner_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.permissions_helper import _win_pipe

    wait = Mock(return_value=False)
    kernel32 = SimpleNamespace(WaitNamedPipeW=wait, CreateFileW=Mock())
    sleeps: list[float] = []
    monkeypatch.setitem(sys.modules, "msvcrt", ModuleType("msvcrt"))

    def fake_windll(_name: str, *, use_last_error: bool) -> SimpleNamespace:
        assert use_last_error
        return kernel32

    monkeypatch.setattr(_win_pipe.ctypes, "WinDLL", fake_windll, raising=False)
    monkeypatch.setattr(_win_pipe.ctypes, "get_last_error", lambda: 2, raising=False)
    monkeypatch.setattr(_win_pipe, "_CONNECT_ATTEMPTS", 3)
    monkeypatch.setattr(_win_pipe, "_CONNECT_DELAY_S", 0.3)
    monkeypatch.setattr(client, "_CONNECT_ATTEMPTS", 2)
    monkeypatch.setattr(_win_pipe.time, "sleep", sleeps.append)
    monkeypatch.setattr(resilience, "_sleep", sleeps.append)
    with pytest.raises(PermissionsHelperError):
        client._call_pipe({"id": 1, "method": "ping"})
    assert wait.call_count == 6
    assert sleeps == [0.3, 0.3, 0.2, 0.3, 0.3]


def test_win_pipe_open_failure_keeps_its_errno_message(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.permissions_helper import _win_pipe

    wait = Mock(return_value=True)
    create = Mock(return_value=_win_pipe.wintypes.HANDLE(-1).value)
    kernel32 = SimpleNamespace(WaitNamedPipeW=wait, CreateFileW=create)
    monkeypatch.setitem(sys.modules, "msvcrt", ModuleType("msvcrt"))

    def fake_windll(_name: str, *, use_last_error: bool) -> SimpleNamespace:
        assert use_last_error
        return kernel32

    monkeypatch.setattr(_win_pipe.ctypes, "WinDLL", fake_windll, raising=False)
    monkeypatch.setattr(_win_pipe.ctypes, "get_last_error", lambda: 6, raising=False)
    with pytest.raises(ConnectionError) as error:
        _win_pipe.connect("test-pipe")
    assert str(error.value) == "permissions helper pipe 'test-pipe' open failed: 6"
    assert wait.call_count == create.call_count == 1
