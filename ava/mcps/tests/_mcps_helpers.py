"""Shared fixtures and helpers for the ava.mcps test files; split from ava/mcps/tests/test_mcps.py (task #4922)."""

from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from ava.mcps._clients import McpClients
from ava.mcps._tools import RemoteClient, ToolSource
from ava.mcps.config import ToolInfo
from base.config import settings


def local_mcp_clients(timeout_seconds: Callable[[], float] | None = None) -> McpClients:
    """This test's own MCP clients (a daemon client per test)."""
    return McpClients(
        timeout_seconds
        if timeout_seconds is not None
        else lambda: settings.sandbox.mcp_connect_timeout_seconds
    )


class NoCache:
    """A tool cache that keeps nothing: every read misses, every write is dropped."""

    def read(self, server: str) -> list[ToolInfo] | None:
        return None

    def write(self, server: str, tools: list[ToolInfo]) -> None:
        return None


class StoredTools:
    """A tool cache that always holds `tools` and counts its reads."""

    def __init__(self, tools: list[ToolInfo]) -> None:
        self.tools = tools
        self.reads = 0

    def read(self, server: str) -> list[ToolInfo] | None:
        self.reads += 1
        return self.tools

    def write(self, server: str, tools: list[ToolInfo]) -> None:
        return None


def tool_source(
    remote: Callable[[], RemoteClient],
    *,
    timeout_seconds: Callable[[], float] = lambda: 5.0,
    cache: object | None = None,
    validate_identity: Callable[[], object] = lambda: None,
) -> ToolSource:
    """A source over the given daemon client: no cache unless one is passed, no identity check
    unless `validate_identity` is."""
    return ToolSource(
        remote=remote,
        timeout_seconds=timeout_seconds,
        cache=cache if cache is not None else NoCache(),  # pyright: ignore[reportArgumentType]
        validate_identity=validate_identity,
    )


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
def mock_session() -> MagicMock:
    """Simulate the daemon's wire result; never construct a local SDK session.

    `session.source` is the `ToolSource` over it: a daemon client backed by the mock and no
    cache, ready to hand to `ServerProxy` / `call_raw`.
    """
    session = MagicMock(name="MCPSession")
    session.source = tool_source(lambda: _DaemonFixture(session))
    return session


def content_text(text: str) -> MagicMock:
    c = MagicMock()
    c.model_dump = MagicMock(return_value={"type": "text", "text": text})
    return c


def content_image(b64: str) -> MagicMock:
    c = MagicMock()
    c.model_dump = MagicMock(return_value={"type": "image", "data": b64, "mimeType": "image/png"})
    return c


def call_tool_result(
    content: list, *, is_error: bool = False, structured: dict | None = None
) -> MagicMock:
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
