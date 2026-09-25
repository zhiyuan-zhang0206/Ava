"""Unit tests for the chrome MCP bridge's socket link.

Tests cover both _Link (request/response framing) and _ReconnectingLink
(automatic reconnect on transport errors).
"""

import asyncio
import json
from importlib import import_module
from typing import Any
from unittest.mock import AsyncMock

import pytest

from services.browser.mcp_socket_bridge import NotDeliveredError
from services.browser.mcp_wrapper import _Link, _ReconnectingLink
from shared.config import settings

# ---------------------------------------------------------------------------
# Fake stream helpers
# ---------------------------------------------------------------------------


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


class FakeReader:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


def _line(obj: dict[str, Any]) -> bytes:
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
    writer = FakeWriter()
    link = _Link(reader, writer)  # type: ignore[arg-type]
    assert await link.request({"method": "list_tools"}) == {"x": 1}
    assert "agent_id" not in json.loads(writer.written[0])


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

            async def not_delivered(payload: dict[str, Any]) -> Any:
                raise NotDeliveredError("simulated dead socket on write")

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

        async def always_raise(payload: dict[str, Any]) -> Any:
            raise NotDeliveredError("always broken")

        link_.request = always_raise  # type: ignore[assignment]
        return link_

    link._connect_once = _always_broken  # type: ignore[assignment]

    with pytest.raises(NotDeliveredError, match="always broken"):
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

            async def not_delivered(payload: dict[str, Any]) -> Any:
                raise NotDeliveredError("simulated")

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

    async def req(payload: dict[str, Any]) -> None:
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

        async def reset_request(payload: dict[str, Any]) -> Any:
            raise ConnectionResetError("daemon died mid-round-trip")

        link_.request = reset_request  # type: ignore[assignment]
        return link_

    reconnecting = _ReconnectingLink(max_retries=3, base_delay=0.0)
    reconnecting._connect_once = _fake_connect  # type: ignore[assignment]

    with pytest.raises(ConnectionResetError):
        await reconnecting.request({"method": "call_tool", "tool": "x", "args": {}})
    assert connect_calls == 1, "a may-have-executed reset must not be retried"


async def test_link_drain_reset_preserves_unknown_delivery_error() -> None:
    """A reset after write must surface without a retryable classification."""

    class DeadWriter(FakeWriter):
        async def drain(self) -> None:
            raise ConnectionResetError("peer gone")

    writer = DeadWriter()
    link = _Link(FakeReader([]), writer)  # type: ignore[arg-type]
    with pytest.raises(ConnectionResetError, match="peer gone"):
        await link.request({"method": "list_tools"})
    assert len(writer.written) == 1


async def test_shared_socket_bridge_dials_with_bounded_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = import_module("services.browser.mcp_socket_bridge")
    failures = [FileNotFoundError("absent"), ConnectionRefusedError("refused")]
    reader, writer = FakeReader([]), FakeWriter()
    calls: list[tuple[str, int]] = []
    sleeps: list[float] = []

    async def open_socket(*, path: str, limit: int):
        calls.append((path, limit))
        if failures:
            raise failures.pop(0)
        return reader, writer

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(bridge.asyncio, "open_unix_connection", open_socket)
    monkeypatch.setattr(bridge.asyncio, "sleep", sleep)
    assert await bridge.dial_unix_socket("test.sock", service_label="chrome MCP daemon") == (
        reader,
        writer,
    )
    assert calls == [("test.sock", 64 * 1024 * 1024)] * 3
    assert sleeps == [0.5, 0.5]

    async def always_refused(*, path: str, limit: int):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(bridge.asyncio, "open_unix_connection", always_refused)
    calls.clear()
    sleeps.clear()
    with pytest.raises(ConnectionError) as error:
        await bridge.dial_unix_socket("test.sock", service_label="chrome MCP daemon")
    assert str(error.value) == "chrome MCP daemon not reachable at test.sock: refused"
    assert sleeps == [0.5] * 10


@pytest.mark.parametrize("side", ["browser", "computer"])
async def test_wrapper_retry_budget_and_backoff(monkeypatch: pytest.MonkeyPatch, side: str) -> None:
    wrapper = import_module(f"services.{side}.mcp_wrapper")
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(wrapper.asyncio, "sleep", sleep)
    reconnecting = wrapper._ReconnectingLink()
    connect = AsyncMock(side_effect=ConnectionRefusedError("offline"))
    reconnecting._connect_once = connect
    with pytest.raises(ConnectionRefusedError, match="offline"):
        await reconnecting.request({"method": "list_tools"})
    assert connect.await_count == 6
    base = 1.0 if side == "browser" else 0.5
    assert sleeps == [base * 2**attempt for attempt in range(5)]


@pytest.mark.parametrize("side", ["browser", "computer"])
async def test_wrapper_failure_policy_and_wire_messages(side: str) -> None:
    wrapper = import_module(f"services.{side}.mcp_wrapper")
    label = "chrome MCP daemon" if side == "browser" else "computer MCP daemon"
    cases = [
        (b"", ConnectionError, f"{label} closed the connection", True),
        (
            _line({"id": 9, "ok": True, "result": None}),
            RuntimeError,
            f"{label} response id 9 != request 1",
            side == "computer",
        ),
        (
            _line({"id": 1, "ok": False, "error": "daemon refused"}),
            RuntimeError,
            "daemon refused",
            side == "computer",
        ),
        (_line({"id": 1, "ok": False}), RuntimeError, f"{label} error", side == "computer"),
    ]
    for line, error_type, message, should_close in cases:
        writer = FakeWriter()
        closed: list[bool] = []
        writer.close = lambda closed=closed: closed.append(True)
        reconnecting = wrapper._ReconnectingLink()
        connect = AsyncMock(return_value=wrapper._Link(FakeReader([line]), writer))
        reconnecting._connect_once = connect
        with pytest.raises(error_type) as error:
            await reconnecting.request({"method": "call_tool", "tool": "x", "args": {}})
        assert str(error.value) == message
        assert connect.await_count == 1
        assert len(writer.written) == 1
        assert bool(closed) is should_close
        assert (reconnecting._link is None) is should_close


@pytest.mark.parametrize("side", ["browser", "computer"])
async def test_upstream_rejection_is_browser_only(
    monkeypatch: pytest.MonkeyPatch, side: str
) -> None:
    wrapper = import_module(f"services.{side}.mcp_wrapper")
    rejection = "chrome upstream session is down; browser-mcp will restart"
    writers = [FakeWriter(), FakeWriter()]
    closed: list[int] = []
    for index, writer in enumerate(writers):
        writer.close = lambda index=index: closed.append(index)
    links = [
        wrapper._Link(FakeReader([_line({"id": 1, "ok": False, "error": rejection})]), writers[0]),
        wrapper._Link(FakeReader([_line({"id": 1, "ok": True, "result": "ok"})]), writers[1]),
    ]
    connect = AsyncMock(side_effect=links)
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(wrapper.asyncio, "sleep", sleep)
    reconnecting = wrapper._ReconnectingLink()
    reconnecting._connect_once = connect
    if side == "browser":
        assert await reconnecting.request({"method": "call_tool", "tool": "x"}) == "ok"
        assert connect.await_count == 2
        assert [len(writer.written) for writer in writers] == [1, 1]
        assert sleeps == [1.0]
    else:
        with pytest.raises(RuntimeError, match=rejection):
            await reconnecting.request({"method": "call_tool", "tool": "x"})
        assert connect.await_count == 1
        assert [len(writer.written) for writer in writers] == [1, 0]
        assert sleeps == []
    assert closed == [0]


@pytest.mark.parametrize("side", ["browser", "computer"])
async def test_stalled_drain_is_unknown_delivery_without_retry(
    monkeypatch: pytest.MonkeyPatch, side: str
) -> None:
    wrapper = import_module(f"services.{side}.mcp_wrapper")
    monkeypatch.setattr(settings.sandbox, "mcp_connect_timeout_seconds", 0.01)

    class StalledWriter(FakeWriter):
        def __init__(self) -> None:
            super().__init__()
            self.closed = False

        async def drain(self) -> None:
            await asyncio.Event().wait()

        def close(self) -> None:
            self.closed = True

    writer = StalledWriter()
    connect = AsyncMock(return_value=wrapper._Link(FakeReader([]), writer))
    reconnecting = wrapper._ReconnectingLink()
    reconnecting._connect_once = connect
    with pytest.raises(TimeoutError):
        await reconnecting.request({"method": "call_tool", "tool": "x"})
    assert connect.await_count == 1
    assert len(writer.written) == 1
    assert writer.closed


@pytest.mark.parametrize("side", ["browser", "computer"])
@pytest.mark.parametrize("fail_stage", ["write", "drain"])
async def test_write_or_drain_failure_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, side: str, fail_stage: str
) -> None:
    wrapper = import_module(f"services.{side}.mcp_wrapper")

    class FailingWriter(FakeWriter):
        def write(self, b: bytes) -> None:
            if fail_stage == "write":
                raise BrokenPipeError("socket broke")
            super().write(b)

        async def drain(self) -> None:
            if fail_stage == "drain":
                raise BrokenPipeError("socket broke")

    failed_writer = FailingWriter()
    success_writer = FakeWriter()
    closed: list[bool] = []
    failed_writer.close = lambda: closed.append(True)
    connect = AsyncMock(
        side_effect=[
            wrapper._Link(FakeReader([]), failed_writer),
            wrapper._Link(
                FakeReader([_line({"id": 1, "ok": True, "result": "ok"})]), success_writer
            ),
        ]
    )
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(wrapper.asyncio, "sleep", sleep)
    reconnecting = wrapper._ReconnectingLink()
    reconnecting._connect_once = connect
    with pytest.raises(BrokenPipeError, match="socket broke"):
        await reconnecting.request({"method": "call_tool", "tool": "x"})
    assert connect.await_count == 1
    assert [len(failed_writer.written), len(success_writer.written)] == [
        0 if fail_stage == "write" else 1,
        0,
    ]
    assert closed == [True]
    assert sleeps == []


@pytest.mark.parametrize("side", ["browser", "computer"])
async def test_drain_reset_surfaces_unknown_delivery_without_retry(side: str) -> None:
    wrapper = import_module(f"services.{side}.mcp_wrapper")

    class ResetOnDrain(FakeWriter):
        async def drain(self) -> None:
            raise ConnectionResetError("reset during drain")

    failed = ResetOnDrain()
    fresh = FakeWriter()
    connect = AsyncMock(
        side_effect=[
            wrapper._Link(FakeReader([]), failed),
            wrapper._Link(FakeReader([_line({"id": 1, "ok": True, "result": "next"})]), fresh),
        ]
    )
    reconnecting = wrapper._ReconnectingLink()
    reconnecting._connect_once = connect
    with pytest.raises(ConnectionResetError, match="reset during drain"):
        await reconnecting.request({"method": "call_tool", "tool": "click"})
    assert connect.await_count == 1
    assert [len(failed.written), len(fresh.written)] == [1, 0]
    assert failed.closed
    assert await reconnecting.request({"method": "list_tools"}) == "next"
    assert connect.await_count == 2


@pytest.mark.parametrize("side", ["browser", "computer"])
async def test_closed_before_write_retries_without_sending(
    monkeypatch: pytest.MonkeyPatch, side: str
) -> None:
    wrapper = import_module(f"services.{side}.mcp_wrapper")
    closed = FakeWriter()
    closed.close()
    fresh = FakeWriter()
    connect = AsyncMock(
        side_effect=[
            wrapper._Link(FakeReader([]), closed),
            wrapper._Link(FakeReader([_line({"id": 1, "ok": True, "result": "ok"})]), fresh),
        ]
    )
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(wrapper.asyncio, "sleep", sleep)
    reconnecting = wrapper._ReconnectingLink()
    reconnecting._connect_once = connect
    assert await reconnecting.request({"method": "call_tool", "tool": "click"}) == "ok"
    assert connect.await_count == 2
    assert [len(closed.written), len(fresh.written)] == [0, 1]
    assert sleeps == [1.0 if side == "browser" else 0.5]
