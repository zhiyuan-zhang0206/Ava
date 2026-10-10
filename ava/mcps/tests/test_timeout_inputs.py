"""Local MCP sessions keep their creation-time deadlines until reconnect."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ava import mcps
from ava.mcps._clients import McpClients
from ava.mcps.tests._mcps_helpers import fake_config as fake_config


async def test_local_session_timeout_is_fixed_until_reconnect(
    fake_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_config.write_text(
        json.dumps({"mcpServers": {"test": {"url": "https://test.invalid/mcp"}}})
    )
    current = [7.5]
    clients = McpClients(lambda: current[0])
    streams = MagicMock()
    streams.__aenter__ = AsyncMock(return_value=(object(), object()))
    streams.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "mcp.client.streamable_http.streamable_http_client", MagicMock(return_value=streams)
    )
    session = MagicMock(initialize=AsyncMock())
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    session_factory = MagicMock(return_value=session_cm)
    monkeypatch.setattr("mcp.ClientSession", session_factory)
    try:
        assert await mcps._connect(clients, "test") is session
        assert session_factory.call_args.kwargs["read_timeout_seconds"] == 7.5
        current[0] = 2.0
        assert await mcps._connect(clients, "test") is session
        assert session_factory.call_count == 1
        assert session_factory.call_args.kwargs["read_timeout_seconds"] == 7.5
        await mcps._invalidate_session(clients, "test")
        assert await mcps._connect(clients, "test") is session
        assert session_factory.call_count == 2
        assert session_factory.call_args.kwargs["read_timeout_seconds"] == 2.0
    finally:
        await mcps._invalidate_session(clients, "test")
