"""Unit tests for the computer MCP bridge's socket link.

Covers _Link (request/response framing + agent-id stamping) and
_ReconnectingLink (automatic reconnect on transport errors). The MCP stdio
surface itself is a thin pass-through (same shape as the browser wrapper),
exercised against a live daemon in dev-cluster testing.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from services.computer.mcp_wrapper import _Link, _ReconnectingLink


class FakeWriter:
    def __init__(self) -> None:
        self.written: list[bytes] = []

    def write(self, b: bytes) -> None:
        self.written.append(b)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


class FakeReader:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


def _line(obj: dict) -> bytes:
    return (json.dumps(obj) + "\n").encode()


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


# ── _ReconnectingLink ───────────────────────────────────────────────────────


async def test_reconnecting_link_redials_on_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[Any] = []

    async def _connect_once() -> _Link:
        reader = FakeReader([_line({"id": 1, "ok": True, "result": "ok"})])
        link = _Link(reader, FakeWriter())  # type: ignore[arg-type]
        attempts.append(link)
        if len(attempts) == 1:
            link._reader = FakeReader([])  # pyright: ignore[reportAttributeAccessIssue]  # first connection dies on request
        return link

    rl = _ReconnectingLink()
    rl._connect_once = _connect_once  # type: ignore[method-assign]
    assert await rl.request({"method": "ping"}) == "ok"
    assert len(attempts) == 2


async def test_reconnecting_link_never_retries_delivered_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request whose response never arrives may have executed on the desktop —
    retrying could double-click. The error surfaces and the link reconnects."""

    async def _connect_once() -> _Link:
        reader = FakeReader([])  # closes without answering
        return _Link(reader, FakeWriter())  # type: ignore[arg-type]

    rl = _ReconnectingLink()
    rl._connect_once = _connect_once  # type: ignore[method-assign]
    with pytest.raises(ConnectionError):
        await rl.request({"method": "call_tool", "tool": "click", "args": {}})
