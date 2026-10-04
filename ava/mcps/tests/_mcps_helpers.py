"""Shared fixtures and helpers for the ava.mcps test files; split from ava/mcps/tests/test_mcps.py (task #4922)."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import ava.mcps as mcps_mod
from ava.mcps._clients import McpClients


def local_mcp_clients(monkeypatch: pytest.MonkeyPatch) -> McpClients:
    """Give this test its own MCP clients (daemon absent -> local mode)."""
    clients = McpClients()
    monkeypatch.setattr(mcps_mod, "_clients", lambda: clients)
    return clients


@pytest.fixture
def fake_config(unit_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ava_home() to tmpdir, drop mcp.json in it, do not touch user's real ~/.ava/mcp.json.

    Patches builtin_mcp_paths to list so tests that expect empty-or-known
    configs are not surprised by the repo's mcps/chrome/.mcp.json built-in.
    """
    import ava.mcp_config as _cfg

    monkeypatch.setattr(_cfg, "builtin_mcp_paths", list)
    return unit_home / "mcp.json"


@pytest.fixture
def mock_session(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """patch `_connect` to directly return a MagicMock session, without starting background thread / real
    subprocess. Also clear sessions cache."""
    session = MagicMock(name="MCPSession")
    session.list_tools = AsyncMock()
    session.call_tool = AsyncMock()

    async def _fake_connect(mcp: McpClients, server: str, **kwargs: object) -> MagicMock:
        return session

    local_mcp_clients(monkeypatch)
    monkeypatch.setattr(mcps_mod, "_connect", _fake_connect)
    # Disable disk cache to avoid cross-test pollution (mock data vs real cached data inconsistency)
    monkeypatch.setattr(mcps_mod, "_read_cache", lambda _server: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(mcps_mod, "_write_cache", lambda _server, _tools: None)  # pyright: ignore[reportUnknownArgumentType]
    return session


def _content_text(text: str) -> MagicMock:
    c = MagicMock()
    c.model_dump = MagicMock(return_value={"type": "text", "text": text})
    return c


def _content_image(b64: str) -> MagicMock:
    c = MagicMock()
    c.model_dump = MagicMock(return_value={"type": "image", "data": b64, "mimeType": "image/png"})
    return c


def _result(content: list, *, is_error: bool = False, structured: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.content = content
    r.is_error = is_error
    r.structured_content = structured
    return r


def _make_tool(name: str, description: str = "", schema: dict | None = None) -> MagicMock:
    t = MagicMock(spec=["name", "description", "input_schema"])
    t.name = name
    t.description = description
    t.input_schema = schema or {}
    return t
