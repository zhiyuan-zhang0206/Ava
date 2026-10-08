"""An MCP tool with an uncertain result must not execute twice."""

from __future__ import annotations

import asyncio
import builtins
import errno
import json
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

import ava.mcps._daemon as daemon
from ava import mcps
from ava.mcp_config import is_transport_error
from ava.mcps.tests._mcps_helpers import local_mcp_clients


@pytest.mark.parametrize("name", ["ClosedResourceError", "MCPError", "JSONRPCError"])
def test_local_tool_does_not_reconnect_for_a_same_named_programming_error(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    from mcp.types import REQUEST_TIMEOUT

    error = type(name, (RuntimeError,), {"code": REQUEST_TIMEOUT})("unexpected tool failure")
    session = MagicMock(call_tool=AsyncMock(side_effect=error))
    stack = MagicMock(aclose=AsyncMock())
    monkeypatch.setattr(mcps, "_get_remote_client", lambda: None)
    clients = local_mcp_clients(monkeypatch)
    clients.sessions["fs"] = session
    clients.session_stacks["fs"] = stack
    connect = AsyncMock(return_value=session)
    monkeypatch.setattr(mcps, "_connect", connect)

    with pytest.raises(mcps.MCPCallError, match="unexpected tool failure") as raised:
        mcps._call_raw("fs", "charge")
    assert "result unknown" not in str(raised.value)
    connect.assert_awaited_once()
    session.call_tool.assert_awaited_once()
    stack.aclose.assert_not_awaited()


def test_transport_subclasses_keep_the_known_retry_contract() -> None:
    from anyio import BrokenResourceError
    from mcp import MCPError
    from mcp.types import CONNECTION_CLOSED

    class PeerDisconnectedError(BrokenResourceError):
        pass

    class SessionDisconnectedError(MCPError):
        pass

    assert is_transport_error(PeerDisconnectedError()) is True
    assert (
        is_transport_error(SessionDisconnectedError(CONNECTION_CLOSED, "Connection closed")) is True
    )


def test_required_dependency_failure_does_not_become_an_empty_retry_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp import MCPError
    from mcp.types import CONNECTION_CLOSED

    original_import = builtins.__import__
    error = ImportError("required MCP types cannot load")

    def blocked_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "mcp.types":
            raise error
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    with pytest.raises(ImportError, match="required MCP types cannot load") as raised:
        is_transport_error(MCPError(CONNECTION_CLOSED, "Connection closed"))
    assert raised.value is error


@pytest.mark.parametrize("code", [errno.EPIPE, errno.ECONNRESET, errno.ECONNREFUSED])
def test_native_transport_errno_is_retryable(code: int) -> None:
    assert is_transport_error(OSError(code, "transport unavailable")) is True


def test_unrelated_io_failure_is_not_a_transport_retry() -> None:
    assert is_transport_error(PermissionError(errno.EACCES, "permission denied")) is False
    assert is_transport_error(FileNotFoundError(errno.ENOENT, "missing configuration")) is False


def test_local_tool_result_lost_after_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    effects = 0

    async def execute_then_disconnect(*_args: Any) -> None:
        nonlocal effects
        effects += 1
        raise BrokenPipeError("reply lost")

    session = MagicMock(call_tool=AsyncMock(side_effect=execute_then_disconnect))
    stack = MagicMock(aclose=AsyncMock())
    monkeypatch.setattr(mcps, "_get_remote_client", lambda: None)
    clients = local_mcp_clients(monkeypatch)
    clients.sessions["fs"] = session
    clients.session_stacks["fs"] = stack
    connect = AsyncMock(return_value=session)
    monkeypatch.setattr(mcps, "_connect", connect)

    with pytest.raises(mcps.MCPCallError, match="result unknown"):
        mcps._call_raw("fs", "charge")
    assert effects == 1
    connect.assert_awaited_once()
    stack.aclose.assert_awaited_once()


def test_daemon_reply_lost_does_not_fall_back_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    sock = MagicMock()
    sock.recv.return_value = b""  # daemon may have executed the tool
    client = mcps._RemoteMCPClient("unused")
    monkeypatch.setattr(client, "_ensure_connected", lambda: sock)
    monkeypatch.setattr(mcps, "_get_remote_client", lambda: client)

    def forbid_local(coro: Any) -> None:
        coro.close()
        raise AssertionError("tool replayed locally")

    local = MagicMock(side_effect=forbid_local)
    monkeypatch.setattr(mcps, "_run_async", local)
    with pytest.raises(mcps.MCPCallError, match="result unknown"):
        mcps._call_raw("fs", "charge")
    sock.sendall.assert_called_once()
    local.assert_not_called()


async def _daemon_call(
    monkeypatch: pytest.MonkeyPatch, session_getter: AsyncMock
) -> dict[str, Any]:
    monkeypatch.setattr(daemon, "_get_session", session_getter)
    monkeypatch.setattr(daemon, "_invalidate_session", AsyncMock())
    request = {"id": 1, "method": "call_tool", "params": {"server": "fs", "tool": "charge"}}
    reader = asyncio.StreamReader()
    reader.feed_data((json.dumps(request) + "\n").encode())
    reader.feed_eof()
    chunks: list[bytes] = []
    writer = MagicMock()
    writer.write.side_effect = chunks.append
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()
    scope = daemon._Scope(local=daemon._Buckets(), shared=daemon._Buckets(), oauth_locks={})
    await daemon._handle_client(reader, cast(asyncio.StreamWriter, writer), scope)
    return json.loads(b"".join(chunks))


async def test_daemon_tool_result_lost_after_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    effects = 0

    async def execute_then_disconnect(*_args: Any) -> None:
        nonlocal effects
        effects += 1
        raise BrokenPipeError("reply lost")

    session = MagicMock(call_tool=AsyncMock(side_effect=execute_then_disconnect))
    response = await _daemon_call(monkeypatch, AsyncMock(return_value=session))
    assert response["ok"] is False
    assert "result unknown" in response["error"]
    assert effects == 1


async def test_daemon_retries_before_tool_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    result = MagicMock(content=[], is_error=False, structured_content=None)
    session = MagicMock(call_tool=AsyncMock(return_value=result))
    get_session = AsyncMock(side_effect=[BrokenPipeError("connect failed"), session])
    monkeypatch.setattr(daemon.asyncio, "sleep", AsyncMock())

    response = await _daemon_call(monkeypatch, get_session)
    assert response["ok"] is True
    assert get_session.await_count == 2
    session.call_tool.assert_awaited_once_with("charge", {})
