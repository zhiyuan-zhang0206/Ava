"""Unit tests for the chrome MCP bridge's socket link.

Tests cover both _Link (request/response framing) and _ReconnectingLink
(automatic reconnect on transport errors).
"""

import asyncio
import json

import pytest

from services.browser.mcp_wrapper import _Link, _NotDeliveredError, _ReconnectingLink
from shared.config import settings

# ---------------------------------------------------------------------------
# Fake stream helpers
# ---------------------------------------------------------------------------


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


def _ok_link(result: object = "ok") -> _Link:
    """Return a _Link whose first request returns *result*."""
    reader = FakeReader([_line({"id": 1, "ok": True, "result": result})])
    return _Link(reader, FakeWriter())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _Link tests
# ---------------------------------------------------------------------------


async def test_request_returns_result_on_ok() -> None:
    reader = FakeReader([_line({"id": 1, "ok": True, "result": {"x": 1}})])
    link = _Link(reader, FakeWriter())  # type: ignore[arg-type]
    assert await link.request({"method": "list_tools"}) == {"x": 1}


async def test_request_ids_are_monotonic() -> None:
    reader = FakeReader(
        [_line({"id": 1, "ok": True, "result": None}), _line({"id": 2, "ok": True, "result": None})]
    )
    writer = FakeWriter()
    link = _Link(reader, writer)  # type: ignore[arg-type]
    await link.request({"method": "list_tools"})
    await link.request({"method": "list_tools"})
    sent = [json.loads(b) for b in writer.written]
    assert [s["id"] for s in sent] == [1, 2]


async def test_request_raises_on_error_response() -> None:
    reader = FakeReader([_line({"id": 1, "ok": False, "error": "boom"})])
    link = _Link(reader, FakeWriter())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="boom"):
        await link.request({"method": "call_tool", "tool": "x", "args": {}})


async def test_request_raises_on_closed_connection() -> None:
    link = _Link(FakeReader([]), FakeWriter())  # type: ignore[arg-type]
    with pytest.raises(ConnectionError, match="closed"):
        await link.request({"method": "list_tools"})


async def test_request_raises_on_id_mismatch() -> None:
    """A response carrying the wrong id (stream desync) fails loud, never returns
    another call's result."""
    reader = FakeReader([_line({"id": 99, "ok": True, "result": "wrong"})])
    link = _Link(reader, FakeWriter())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="!= request"):
        await link.request({"method": "list_tools"})


class HangingReader(FakeReader):
    """A reader whose readline never returns — simulates a wedged daemon."""

    async def readline(self) -> bytes:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def test_request_times_out_on_wedged_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """A daemon that accepts the request but never answers must surface as a
    TimeoutError instead of hanging the calling agent forever."""
    monkeypatch.setattr(settings.sandbox, "mcp_connect_timeout_seconds", 0.05)
    link = _Link(HangingReader([]), FakeWriter())  # type: ignore[arg-type]
    with pytest.raises(TimeoutError):
        await link.request({"method": "list_tools"})


async def test_reconnecting_link_surfaces_timeout_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (audit round 2, P1): a request that timed out after being
    written may have been EXECUTED by the daemon (response lost in transit);
    retrying a browser write would double-execute it. The wrapper must
    surface the timeout and close the dead link (so the NEXT call
    reconnects), not retry."""
    monkeypatch.setattr(settings.sandbox, "mcp_connect_timeout_seconds", 0.05)
    connect_calls = 0

    async def _fake_connect():
        nonlocal connect_calls
        connect_calls += 1
        if connect_calls == 1:
            return _Link(HangingReader([]), FakeWriter())  # type: ignore[arg-type]
        return _ok_link("recovered")

    reconnecting = _ReconnectingLink(max_retries=2, base_delay=0.0)
    reconnecting._connect_once = _fake_connect  # type: ignore[assignment]

    with pytest.raises(TimeoutError):
        await reconnecting.request({"method": "call_tool", "tool": "x", "args": {}})
    assert connect_calls == 1, "a may-have-executed timeout must not be retried"

    # the dead link is closed: the next request reconnects and succeeds
    result = await reconnecting.request({"method": "list_tools"})
    assert result == "recovered"
    assert connect_calls == 2


# ---------------------------------------------------------------------------
# _ReconnectingLink tests
# ---------------------------------------------------------------------------


async def test_reconnecting_link_success_first_try() -> None:
    """Happy path: connect succeeds, request returns."""
    link = _ReconnectingLink(max_retries=1, base_delay=0.0)
    connect_calls = 0

    async def _fake_connect():
        nonlocal connect_calls
        connect_calls += 1
        return _ok_link("result1")

    link._connect_once = _fake_connect  # type: ignore[assignment]

    result = await link.request({"method": "list_tools"})
    assert result == "result1"
    assert connect_calls == 1


async def test_reconnecting_link_retries_on_connect_failure() -> None:
    """Connect raises ConnectionRefusedError on first attempt, succeeds on retry."""
    link = _ReconnectingLink(max_retries=2, base_delay=0.0)
    connect_calls = 0

    async def _fake_connect():
        nonlocal connect_calls
        connect_calls += 1
        if connect_calls == 1:
            raise ConnectionRefusedError("simulated connect failure")
        return _ok_link("result2")

    link._connect_once = _fake_connect  # type: ignore[assignment]

    result = await link.request({"method": "list_tools"})
    assert result == "result2"
    assert connect_calls == 2


async def test_reconnecting_link_retries_on_not_delivered() -> None:
    """A request that never reached the daemon (_NotDeliveredError) is retried on
    a fresh connection — nothing was executed, so retrying is safe."""
    connect_calls = 0

    async def _fake_connect():
        nonlocal connect_calls
        connect_calls += 1
        if connect_calls == 1:
            # Return a _Link whose request() raises _NotDeliveredError.
            link_ = _ok_link("unused")

            async def not_delivered(payload):
                raise _NotDeliveredError("simulated dead socket on write")

            link_.request = not_delivered  # type: ignore[assignment]
            return link_
        return _ok_link("recovered")

    reconnecting = _ReconnectingLink(max_retries=2, base_delay=0.0)
    reconnecting._connect_once = _fake_connect  # type: ignore[assignment]

    result = await reconnecting.request({"method": "call_tool", "tool": "x", "args": {}})
    assert result == "recovered"
    assert connect_calls == 2


async def test_reconnecting_link_exhausts_retries() -> None:
    """All requests fail before delivery; the retry budget is exhausted and
    the last error propagates."""
    link = _ReconnectingLink(max_retries=2, base_delay=0.0)

    async def _always_broken():
        link_ = _ok_link("unused")

        async def always_raise(payload):
            raise _NotDeliveredError("always broken")

        link_.request = always_raise  # type: ignore[assignment]
        return link_

    link._connect_once = _always_broken  # type: ignore[assignment]

    with pytest.raises(_NotDeliveredError, match="always broken"):
        await link.request({"method": "list_tools"})


async def test_reconnecting_link_does_not_retry_non_transport_error() -> None:
    """RuntimeError from daemon (e.g. bad tool name) is NOT retried."""
    reader = FakeReader([_line({"id": 1, "ok": False, "error": "unknown tool: bad_tool"})])
    link = _ReconnectingLink(max_retries=3, base_delay=0.0)

    async def _fake_connect():
        return _Link(reader, FakeWriter())  # type: ignore[arg-type]

    link._connect_once = _fake_connect  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="unknown tool"):
        await link.request({"method": "call_tool", "tool": "bad_tool", "args": {}})


async def test_reconnecting_link_closes_old_link_on_reconnect() -> None:
    """When a transport error triggers reconnect, the old _Link is closed."""
    close_calls = []

    connect_calls = 0

    async def _fake_connect():
        nonlocal connect_calls
        connect_calls += 1
        if connect_calls == 1:
            link_ = _ok_link("unused")

            async def not_delivered(payload):
                raise _NotDeliveredError("simulated")

            link_.request = not_delivered  # type: ignore[assignment]
            # Track close calls on the old link
            link_.close = lambda: close_calls.append(1)  # type: ignore[assignment]
            return link_
        return _ok_link("recovered")

    reconnecting = _ReconnectingLink(max_retries=2, base_delay=0.0)
    reconnecting._connect_once = _fake_connect  # type: ignore[assignment]

    result = await reconnecting.request({"method": "list_tools"})
    assert result == "recovered"
    assert len(close_calls) == 1  # pyright: ignore[reportUnknownArgumentType]


async def test_reconnecting_link_serializes_requests() -> None:
    """Concurrent requests are serialized; reconnect doesn't race."""
    results = []

    reader = FakeReader(
        [
            _line({"id": 1, "ok": True, "result": "first"}),
            _line({"id": 2, "ok": True, "result": "second"}),
        ]
    )

    async def _fake_connect():
        return _Link(reader, FakeWriter())  # type: ignore[arg-type]

    link = _ReconnectingLink(max_retries=1, base_delay=0.0)
    link._connect_once = _fake_connect  # type: ignore[assignment]

    async def req(payload):
        r = await link.request(payload)  # pyright: ignore[reportUnknownArgumentType]
        results.append(r)  # pyright: ignore[reportUnknownMemberType]

    await asyncio.gather(
        req({"method": "first"}),
        req({"method": "second"}),
    )
    assert results == ["first", "second"]


async def test_reconnecting_link_does_not_retry_after_delivery_loss() -> None:
    """Regression (audit round 2, P1): a transport error raised AFTER the
    payload was written (connection reset mid-round-trip) means the daemon
    may have executed the call — the wrapper must surface, not retry."""
    connect_calls = 0

    async def _fake_connect():
        nonlocal connect_calls
        connect_calls += 1
        link_ = _ok_link("unused")

        async def reset_request(payload):
            raise ConnectionResetError("daemon died mid-round-trip")

        link_.request = reset_request  # type: ignore[assignment]
        return link_

    reconnecting = _ReconnectingLink(max_retries=3, base_delay=0.0)
    reconnecting._connect_once = _fake_connect  # type: ignore[assignment]

    with pytest.raises(ConnectionResetError):
        await reconnecting.request({"method": "call_tool", "tool": "x", "args": {}})
    assert connect_calls == 1, "a may-have-executed reset must not be retried"


async def test_link_write_failure_is_not_delivered() -> None:
    """A socket that dies on write/drain raises _NotDeliveredError (safe to retry),
    not a generic transport error (which the wrapper would surface)."""

    class DeadWriter(FakeWriter):
        async def drain(self) -> None:
            raise ConnectionResetError("peer gone")

    link = _Link(FakeReader([]), DeadWriter())  # type: ignore[arg-type]
    with pytest.raises(_NotDeliveredError):
        await link.request({"method": "list_tools"})
