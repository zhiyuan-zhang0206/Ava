"""Shared fixtures and helpers for the ava.mcps test files; split from ava/mcps/tests/test_mcps.py (task #4922)."""

from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import ava.mcps as mcps_mod
from ava.mcps._clients import McpClients
from ava.mcps.config import ToolInfo
from base.config import settings


def local_mcp_clients(
    monkeypatch: pytest.MonkeyPatch, timeout_seconds: Callable[[], float] | None = None
) -> McpClients:
    """Give this test its own MCP clients (a daemon client per test)."""
    clients = McpClients(
        timeout_seconds
        if timeout_seconds is not None
        else lambda: settings.sandbox.mcp_connect_timeout_seconds
    )
    monkeypatch.setattr(mcps_mod, "_clients", lambda: clients)
    return clients


@pytest.fixture
def fake_config(unit_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ava_home() to tmpdir, drop mcp.json in it, do not touch user's real ~/.ava/mcp.json.

    Patches builtin_mcp_paths to list so tests that expect empty-or-known
    configs are not surprised by the repo's mcps/chrome/.mcp.json built-in.
    """
    import ava.mcps.config as _cfg

    monkeypatch.setattr(_cfg, "builtin_mcp_paths", list)
    return unit_home / "mcp.json"


class _DaemonFixture:
    def __init__(self, session: MagicMock) -> None:
        self.session = session

    def list_tools(self, server: str, *, timeout_seconds: float) -> list[ToolInfo]:
        return [
            {
                "name": t.name,
                "description": t.description or "",
                "input_schema": t.input_schema or {},
            }
            for t in self.session.list_tools().tools
        ]

    def call_tool(
        self, server: str, tool: str, args: dict[str, Any], *, timeout_seconds: float
    ) -> dict[str, Any]:
        result = self.session.call_tool(tool, args or None)
        return {
            "content": [c.model_dump() for c in result.content],
            "isError": bool(result.is_error),
            "structuredContent": result.structured_content,
        }


@pytest.fixture
def mock_session(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Simulate the daemon's wire result; never construct a local SDK session."""
    session = MagicMock(name="MCPSession")
    local_mcp_clients(monkeypatch)
    monkeypatch.setattr(mcps_mod, "_get_remote_client", lambda: _DaemonFixture(session))
    monkeypatch.setattr(mcps_mod, "_read_cache", MagicMock(return_value=None))
    monkeypatch.setattr(mcps_mod, "_write_cache", MagicMock(return_value=None))
    return session


def content_text(text: str) -> MagicMock:
    c = MagicMock()
    c.model_dump = MagicMock(return_value={"type": "text", "text": text})
    return c


def content_image(b64: str) -> MagicMock:
    c = MagicMock()
    c.model_dump = MagicMock(return_value={"type": "image", "data": b64, "mimeType": "image/png"})
    return c


def _result(content: list, *, is_error: bool = False, structured: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.content = content
    r.is_error = is_error
    r.structured_content = structured
    return r


def make_tool(name: str, description: str = "", schema: dict | None = None) -> MagicMock:
    t = MagicMock(spec=["name", "description", "input_schema"])
    t.name = name
    t.description = description
    t.input_schema = schema or {}
    return t
