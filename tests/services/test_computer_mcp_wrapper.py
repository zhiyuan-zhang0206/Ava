"""Unit tests for the computer MCP bridge's socket link.

Covers _Link (request/response framing + agent-id stamping) and
_ReconnectingLink (automatic reconnect on transport errors). The MCP stdio
surface itself is a thin pass-through (same shape as the browser wrapper),
exercised against a live daemon in dev-cluster testing.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from services.computer.mcp_wrapper import _Link, _ReconnectingLink
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


class FailingWriter(FakeWriter):
    def __init__(self, fail_stage: str) -> None:
        super().__init__()
        self.fail_stage = fail_stage

    def write(self, b: bytes) -> None:
        if self.fail_stage == "write":
            raise BrokenPipeError("socket closed before delivery")
        super().write(b)

    async def drain(self) -> None:
        if self.fail_stage == "drain":
            raise BrokenPipeError("socket closed before delivery")


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
async def test_request_classifies_pre_delivery_failure(fail_stage: str) -> None:
    from services.computer.mcp_wrapper import _NotDeliveredError

    writer = FailingWriter(fail_stage)
    link = _Link(FakeReader([]), writer)  # type: ignore[arg-type]
    with pytest.raises(_NotDeliveredError) as error:
        await link.request({"method": "call_tool", "tool": "click", "args": {}})
    assert isinstance(error.value.__cause__, BrokenPipeError)


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
async def test_reconnecting_link_retries_not_delivered(
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
    assert await rl.request({"method": "call_tool", "tool": "type", "args": {}}) == "ok"
    assert attempts == 2
    assert failed_writer.closed
    assert len(success_writer.written) == 1
    assert len(failed_writer.written) == (1 if fail_stage == "drain" else 0)


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
