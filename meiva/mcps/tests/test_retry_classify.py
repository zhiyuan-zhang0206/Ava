from __future__ import annotations

import asyncio
import errno
import json
import socket
import sys
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

import meiva.mcps._daemon as daemon
import meiva.mcps._remote as remote_mod
from base.host.system.tests.fake_commands import fake_host
from meiva import mcps
from meiva.mcps._tools import call_raw
from meiva.mcps.config import is_transport_error
from meiva.mcps.tests._mcps_helpers import tool_source


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

    monkeypatch.setitem(sys.modules, "mcp.types", None)
    with pytest.raises(ImportError, match=r"mcp\.types"):
        is_transport_error(MCPError(CONNECTION_CLOSED, "Connection closed"))


@pytest.mark.parametrize("code", [errno.EPIPE, errno.ECONNRESET, errno.ECONNREFUSED])
def test_native_transport_errno_is_retryable(code: int) -> None:
    assert is_transport_error(OSError(code, "transport unavailable")) is True


def test_unrelated_io_failure_is_not_a_transport_retry() -> None:
    assert is_transport_error(PermissionError(errno.EACCES, "permission denied")) is False
    assert is_transport_error(FileNotFoundError(errno.ENOENT, "missing configuration")) is False


def test_daemon_reply_lost_does_not_fall_back_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    sock = MagicMock()
    sock.recv.return_value = b""
    client = remote_mod.RemoteMCPClient("unused")
    monkeypatch.setattr(socket, "socket", MagicMock(return_value=sock))

    with pytest.raises(mcps.MCPCallError, match="result unknown"):
        call_raw(tool_source(lambda: client), "fs", "charge")
    sock.sendall.assert_called_once()


async def _daemon_call(
    monkeypatch: pytest.MonkeyPatch, session_getter: AsyncMock
) -> dict[str, Any]:
    monkeypatch.setattr(daemon, "get_session", session_getter)
    monkeypatch.setattr(daemon, "invalidate_session", AsyncMock())
    request = {"id": 1, "method": "call_tool", "params": {"server": "fs", "tool": "charge"}}
    reader = asyncio.StreamReader()
    reader.feed_data((json.dumps(request) + "\n").encode())
    reader.feed_eof()
    chunks: list[bytes] = []
    writer = MagicMock()
    writer.write.side_effect = chunks.append
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()
    scope = daemon.Scope(
        local=daemon.Buckets(),
        shared=daemon.Buckets(),
        oauth_locks={},
        timeout_seconds=lambda: 15.0,
    )
    await daemon.handle_client(reader, cast(asyncio.StreamWriter, writer), scope, host=fake_host())
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
