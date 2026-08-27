"""ava.mcps unit tests — config parsing + namespace dispatch + tool invocation paths.

Do not run real MCP servers (requires external npm/uvx packages, too heavy to add to test deps). Use monkeypatch
to point ava_home() to tmpdir + fake `_connect()` returning mock session to verify
namespace dispatch and result processing.

Real integration tests (`ava.mcps.chrome.navigate_page(...)` running against real server) are left for manual testing.
"""

import inspect
import json
import socket
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import ava
import ava._mcp_remote as remote_mod
import ava.mcps as mcps_mod
from shared.config import settings

# ─── _load_config / servers() ────────────────────────────────────────────


@pytest.fixture
def fake_config(unit_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ava_home() to tmpdir, drop mcp.json in it, do not touch user's real ~/.ava/mcp.json.

    Patches _builtin_mcp_paths to list so tests that expect empty-or-known
    configs are not surprised by the repo's mcps/chrome/.mcp.json built-in.
    """
    import ava._mcp_config as _cfg

    monkeypatch.setattr(_cfg, "_builtin_mcp_paths", list)
    return unit_home / "mcp.json"


def test_servers_empty_when_no_config(fake_config: Path) -> None:
    assert mcps_mod.servers() == []


def test_servers_lists_configured_names_sorted(fake_config: Path) -> None:
    fake_config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "zebra": {"command": "z"},
                    "apple": {"command": "a"},
                    "mango": {"command": "m"},
                }
            }
        ),
        encoding="utf-8",
    )
    assert mcps_mod.servers() == ["apple", "mango", "zebra"]


def test_servers_empty_when_no_section(fake_config: Path) -> None:
    """File exists but no mcpServers section → empty list (compatible with Claude Code generic settings)."""
    fake_config.write_text(json.dumps({"other_key": {}}), encoding="utf-8")
    assert mcps_mod.servers() == []


def test_load_config_raises_on_bad_json(fake_config: Path) -> None:
    fake_config.write_text("not json {{{", encoding="utf-8")
    with pytest.raises(mcps_mod.MCPError, match="Failed to read"):
        mcps_mod._load_config()


def test_description_returns_config_field(fake_config: Path) -> None:
    fake_config.write_text(
        json.dumps({"mcpServers": {"chrome": {"command": "x", "description": "drive a browser"}}}),
        encoding="utf-8",
    )
    assert mcps_mod.description("chrome") == "drive a browser"


def test_description_none_when_field_absent(fake_config: Path) -> None:
    # No `description` field -> the capabilities index renders a bare name.
    fake_config.write_text(
        json.dumps({"mcpServers": {"chrome": {"command": "x"}}}), encoding="utf-8"
    )
    assert mcps_mod.description("chrome") is None


def test_description_raises_for_missing_server(fake_config: Path) -> None:
    fake_config.write_text(json.dumps({"mcpServers": {"fs": {"command": "x"}}}), encoding="utf-8")
    with pytest.raises(mcps_mod.MCPServerNotFound, match="nope"):
        mcps_mod.description("nope")


# ─── module-level __getattr__ / __dir__ namespace dispatch ────────────────


def test_module_getattr_returns_proxy(fake_config: Path) -> None:
    fake_config.write_text(json.dumps({"mcpServers": {"fs": {"command": "x"}}}), encoding="utf-8")
    proxy = mcps_mod.fs  # goes through module-level __getattr__
    assert isinstance(proxy, mcps_mod._ServerProxy)
    assert proxy._server == "fs"


def test_module_getattr_raises_for_missing_server(fake_config: Path) -> None:
    fake_config.write_text(json.dumps({"mcpServers": {"fs": {"command": "x"}}}), encoding="utf-8")
    with pytest.raises(AttributeError, match="nope"):
        mcps_mod.nope  # noqa: B018 — intentionally trigger __getattr__


def test_module_dir_lists_servers(fake_config: Path) -> None:
    fake_config.write_text(
        json.dumps({"mcpServers": {"fs": {"command": "x"}, "github": {"command": "y"}}}),
        encoding="utf-8",
    )
    listing = dir(mcps_mod)
    # server names are present; tool methods are present; other _-prefixed filtered out
    assert "fs" in listing
    assert "github" in listing
    assert "servers" in listing
    assert "description" in listing
    assert "help" in listing


def test_help_on_mcps_module_is_index_only(
    fake_config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`ava.help(ava.mcps)` is an INDEX: each configured server as a name plus at
    most its one-liner, never its tool list or a tool's JSON Schema. Pinning it
    because the `# Capabilities` section is the prompt's single MCP index — a
    render that reached for tools would connect to every configured server and
    put every tool schema in front of the agent. Tools stay one
    `ava.help(ava.mcps.<server>)` away."""
    import ava

    fake_config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fs": {"command": "x", "description": "Local filesystem"},
                    "github": {"command": "y"},
                }
            }
        ),
        encoding="utf-8",
    )
    ava.help(ava.mcps)
    out = capsys.readouterr().out
    assert "from . import fs" in out
    assert "from . import github" in out
    # Positively: each server carries its one-liner. It is the proxy's generic
    # doc, not the `description` from mcp.json — that one reaches the agent
    # through the `# Capabilities` index, which is the MCP index of record.
    assert "Tools of MCP server 'fs'." in out
    # No tool-schema surface: the render must not have connected to a server.
    assert "inputSchema" not in out
    assert "**kwargs" not in out


# ─── tools / call / call_raw via ServerProxy ─────────────────────────────


@pytest.fixture
def mock_session(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """patch `_connect` to directly return a MagicMock session, without starting background thread / real
    subprocess. Also clear sessions cache."""
    session = MagicMock(name="MCPSession")
    session.list_tools = AsyncMock()
    session.call_tool = AsyncMock()

    async def _fake_connect(server: str, **kwargs: object) -> MagicMock:
        return session

    monkeypatch.setattr(mcps_mod, "_connect", _fake_connect)
    monkeypatch.setattr(mcps_mod, "_sessions", {})
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


def test_proxy_dir_lists_tools_plus_raw(mock_session: MagicMock) -> None:
    """`dir(proxy)` goes through list_tools to get tool names + `raw` is also present (visible in ava.help)."""
    mock_session.list_tools.return_value = MagicMock(
        tools=[_make_tool("read_file"), _make_tool("write_file")]
    )
    proxy = mcps_mod._ServerProxy("fs")
    listing = dir(proxy)
    assert listing == ["raw", "read_file", "write_file"]


def test_proxy_attribute_returns_callable(mock_session: MagicMock) -> None:
    """`proxy.read_file` returns a callable, calling it goes through call_tool."""
    mock_session.list_tools.return_value = MagicMock(tools=[_make_tool("read_file", "Read a file")])
    mock_session.call_tool.return_value = _result([_content_text("file contents")])
    proxy = mcps_mod._ServerProxy("fs")
    fn = proxy.read_file
    assert callable(fn)
    assert fn.__name__ == "read_file"
    assert "Read a file" in (fn.__doc__ or "")
    result = fn(path="/x")
    assert result == "file contents"
    mock_session.call_tool.assert_awaited_once_with("read_file", {"path": "/x"})


def test_proxy_attribute_works_for_unknown_tool_too(mock_session: MagicMock) -> None:
    """Tool names not in cache also return callable — MCP server side will reject wrong names,
    error message is more reliable."""
    mock_session.list_tools.return_value = MagicMock(tools=[])
    mock_session.call_tool.return_value = _result([_content_text("ok")])
    proxy = mcps_mod._ServerProxy("fs")
    fn = proxy.unknown_tool
    assert callable(fn)
    assert fn() == "ok"


def test_proxy_call_joins_text_blocks(mock_session: MagicMock) -> None:
    mock_session.list_tools.return_value = MagicMock(tools=[_make_tool("do")])
    mock_session.call_tool.return_value = _result(
        [_content_text("line 1"), _content_text("line 2")]
    )
    proxy = mcps_mod._ServerProxy("fs")
    assert proxy.do() == "line 1\nline 2"


def test_proxy_call_raises_on_is_error(mock_session: MagicMock) -> None:
    mock_session.list_tools.return_value = MagicMock(tools=[_make_tool("do")])
    mock_session.call_tool.return_value = _result(
        [_content_text("permission denied")], is_error=True
    )
    proxy = mcps_mod._ServerProxy("fs")
    with pytest.raises(mcps_mod.MCPCallError, match="permission denied"):
        proxy.do()


def test_proxy_call_raises_on_non_text_content(mock_session: MagicMock) -> None:
    """tool returned image / other non-text content → guides to use .raw()."""
    mock_session.list_tools.return_value = MagicMock(tools=[_make_tool("screenshot")])
    mock_session.call_tool.return_value = _result([_content_image("b64==")])
    proxy = mcps_mod._ServerProxy("fs")
    with pytest.raises(mcps_mod.MCPCallError, match=r"\.raw"):
        proxy.screenshot()


def test_proxy_call_empty_content_returns_empty_string(mock_session: MagicMock) -> None:
    mock_session.list_tools.return_value = MagicMock(tools=[_make_tool("noop")])
    mock_session.call_tool.return_value = _result([])
    proxy = mcps_mod._ServerProxy("fs")
    assert proxy.noop() == ""


def test_proxy_call_empty_text_block_returns_empty_string(mock_session: MagicMock) -> None:
    """A response of all-text blocks that join to "" (e.g. chrome list_pages
    with no open pages) is a legitimate "" — not a spurious
    'non-text content (kinds=[text])' error."""
    mock_session.list_tools.return_value = MagicMock(tools=[_make_tool("list_pages")])
    mock_session.call_tool.return_value = _result([_content_text("")])
    proxy = mcps_mod._ServerProxy("fs")
    assert proxy.list_pages() == ""


def test_proxy_call_empty_text_with_image_still_raises(mock_session: MagicMock) -> None:
    """An empty text block alongside a non-text block still points at .raw() —
    the real payload (image) can't be text-joined."""
    mock_session.list_tools.return_value = MagicMock(tools=[_make_tool("shot")])
    mock_session.call_tool.return_value = _result([_content_text(""), _content_image("b64==")])
    proxy = mcps_mod._ServerProxy("fs")
    with pytest.raises(mcps_mod.MCPCallError, match=r"\.raw"):
        proxy.shot()


def test_proxy_raw_returns_full_structure(mock_session: MagicMock) -> None:
    mock_session.list_tools.return_value = MagicMock(tools=[_make_tool("do")])
    mock_session.call_tool.return_value = _result(
        [_content_text("ok"), _content_image("xyz==")],
        is_error=False,
        structured={"counted": 42},
    )
    proxy = mcps_mod._ServerProxy("fs")
    out = proxy.raw("do", arg1="v")
    assert out["isError"] is False
    assert out["structuredContent"] == {"counted": 42}
    assert len(out["content"]) == 2
    assert out["content"][0] == {"type": "text", "text": "ok"}
    assert out["content"][1] == {"type": "image", "data": "xyz==", "mimeType": "image/png"}


def test_proxy_raw_does_not_raise_on_is_error(mock_session: MagicMock) -> None:
    """raw does not convert isError to raise — returns full structure for caller to decide."""
    mock_session.list_tools.return_value = MagicMock(tools=[_make_tool("x")])
    mock_session.call_tool.return_value = _result(
        [_content_text("permission denied")], is_error=True
    )
    proxy = mcps_mod._ServerProxy("fs")
    out = proxy.raw("x")
    assert out["isError"] is True


def test_proxy_call_structured_only_raises(mock_session: MagicMock) -> None:
    """Only returned structuredContent, no text → raise (guides to use .raw())."""
    mock_session.list_tools.return_value = MagicMock(tools=[_make_tool("count")])
    mock_session.call_tool.return_value = _result([], structured={"counted": 42})
    proxy = mcps_mod._ServerProxy("fs")
    with pytest.raises(mcps_mod.MCPCallError, match="structuredContent"):
        proxy.count()


def test_proxy_tool_not_found_classified(mock_session: MagicMock) -> None:
    """Server-side 'tool not found' type error → MCPToolNotFound."""
    mock_session.list_tools.return_value = MagicMock(tools=[])
    mock_session.call_tool.side_effect = RuntimeError("Tool 'xyz' not found")
    proxy = mcps_mod._ServerProxy("fs")
    with pytest.raises(mcps_mod.MCPToolNotFound):
        proxy.xyz()


def test_proxy_other_errors_become_call_error(mock_session: MagicMock) -> None:
    mock_session.list_tools.return_value = MagicMock(tools=[_make_tool("do")])
    mock_session.call_tool.side_effect = RuntimeError("boom")
    proxy = mcps_mod._ServerProxy("fs")
    with pytest.raises(mcps_mod.MCPCallError, match="boom"):
        proxy.do()


# ─── dead-session rebuild (2026-08-13 #1229) ──────────────────────────────


def _patch_local_session_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the local-path caches for one test (daemon absent → local mode)."""
    monkeypatch.setattr(mcps_mod, "_sessions", {})
    monkeypatch.setattr(mcps_mod, "_session_locks", {})
    monkeypatch.setattr(mcps_mod, "_session_stacks", {})
    monkeypatch.setattr(mcps_mod, "_read_cache", lambda _server: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(mcps_mod, "_write_cache", lambda _server, _tools: None)  # pyright: ignore[reportUnknownArgumentType]


def _dying_then_healthy_connect(
    monkeypatch: pytest.MonkeyPatch,
    dying_call: Any,
    healthy_call: Any,
) -> tuple[list[str], MagicMock]:
    """Fake `_connect`: first call returns a dying cached session (whose stack
    tracks closure), the next one a healthy session. Returns (order log, dying
    stack) so tests can assert the old transport was closed."""
    dying_stack = MagicMock()
    dying_stack.aclose = AsyncMock()
    dying = MagicMock()
    dying.call_tool = AsyncMock(side_effect=dying_call)
    dying.list_tools = AsyncMock(side_effect=dying_call)

    healthy = MagicMock()
    healthy.call_tool = AsyncMock(return_value=healthy_call)
    healthy.list_tools = AsyncMock(return_value=healthy_call)

    order: list[str] = []

    async def _fake_connect(server: str, **kwargs: object) -> MagicMock:
        if not order:
            order.append("dying")
            mcps_mod._sessions[server] = dying
            mcps_mod._session_stacks[server] = dying_stack
            return dying
        order.append("healthy")
        mcps_mod._sessions[server] = healthy
        return healthy

    monkeypatch.setattr(mcps_mod, "_connect", _fake_connect)
    return order, dying_stack


def test_call_raw_rebuilds_session_on_connection_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the cached stdio session's peer died (SDK raises MCPError with the
    CONNECTION_CLOSED code, `from None`), the call closes the dead session,
    reconnects, and retries once — instead of failing forever on the cached
    corpse."""
    from mcp import MCPError
    from mcp.types import CONNECTION_CLOSED

    _patch_local_session_state(monkeypatch)
    order, dying_stack = _dying_then_healthy_connect(
        monkeypatch,
        MCPError(CONNECTION_CLOSED, "Connection closed"),
        _result([_content_text("ok")], is_error=False),
    )

    result = mcps_mod._call_raw("fs", "do")
    assert result["content"] == [{"type": "text", "text": "ok"}]
    assert order == ["dying", "healthy"]  # dead session invalidated, rebuilt once
    assert dying_stack.aclose.await_count == 1  # old transport closed


def test_call_raw_does_not_retry_tool_level_mcp_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server-returned JSON-RPC error (MCPError with INVALID_PARAMS) is not a
    transport death: no rebuild, no retry — a side-effectful tool is never
    double-run."""
    from mcp import MCPError
    from mcp.types import INVALID_PARAMS

    _patch_local_session_state(monkeypatch)
    order, dying_stack = _dying_then_healthy_connect(
        monkeypatch,
        MCPError(INVALID_PARAMS, "Bad args"),
        _result([_content_text("ok")], is_error=False),
    )

    with pytest.raises(mcps_mod.MCPCallError, match="Bad args"):
        mcps_mod._call_raw("fs", "do")
    assert order == ["dying"]  # one session only — no rebuild, no retry
    assert dying_stack.aclose.await_count == 0  # nothing invalidated


def test_list_tools_rebuilds_session_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tool-listing path gets the same dead-session rebuild."""
    from mcp import MCPError
    from mcp.types import CONNECTION_CLOSED

    _patch_local_session_state(monkeypatch)
    order, dying_stack = _dying_then_healthy_connect(
        monkeypatch,
        MCPError(CONNECTION_CLOSED, "Connection closed"),
        MagicMock(tools=[_make_tool("do")]),
    )

    tools = mcps_mod._list_tools("fs")
    assert [t["name"] for t in tools] == ["do"]
    assert order == ["dying", "healthy"]
    assert dying_stack.aclose.await_count == 1


# ─── _load_config edge cases ──────────────────────────────────────────────


def test_load_config_raises_when_section_not_dict(fake_config: Path) -> None:
    """`mcpServers` is not a dict (typo / old format) → MCPError immediately, not silently go empty."""
    fake_config.write_text(json.dumps({"mcpServers": [1, 2, 3]}), encoding="utf-8")
    with pytest.raises(mcps_mod.MCPError, match="mcpServers field is not a dict"):
        mcps_mod._load_config()


# ─── disk cache (_read_cache / _write_cache) ──────────────────────────────


def test_cache_write_then_read_roundtrip(fake_config: Path) -> None:
    """`_write_cache` write + `_read_cache` read → get back the same tools."""
    tools: list[mcps_mod.ToolInfo] = [
        {"name": "t1", "description": "d1", "input_schema": {"type": "object"}}
    ]
    mcps_mod._write_cache("srv", tools)
    cached = mcps_mod._read_cache("srv")
    assert cached == tools


def test_read_cache_returns_none_when_missing(fake_config: Path) -> None:
    assert mcps_mod._read_cache("never_written") is None


def test_read_cache_returns_none_on_bad_json(fake_config: Path, tmp_path: Path) -> None:
    cache_dir = tmp_path / "mcp_cache"
    cache_dir.mkdir()
    (cache_dir / "srv.json").write_text("not json {{{", encoding="utf-8")
    assert mcps_mod._read_cache("srv") is None


def test_read_cache_returns_none_when_expired(fake_config: Path, tmp_path: Path) -> None:
    """cached_at=0 → age ≈ now → exceeds 24h TTL, considered expired."""
    cache_dir = tmp_path / "mcp_cache"
    cache_dir.mkdir()
    (cache_dir / "srv.json").write_text(json.dumps({"tools": [], "cached_at": 0}), encoding="utf-8")
    assert mcps_mod._read_cache("srv") is None


def test_read_cache_returns_none_without_cached_at(fake_config: Path, tmp_path: Path) -> None:
    cache_dir = tmp_path / "mcp_cache"
    cache_dir.mkdir()
    (cache_dir / "srv.json").write_text(json.dumps({"tools": []}), encoding="utf-8")
    assert mcps_mod._read_cache("srv") is None


def test_read_cache_returns_none_when_cached_at_wrong_type(
    fake_config: Path, tmp_path: Path
) -> None:
    cache_dir = tmp_path / "mcp_cache"
    cache_dir.mkdir()
    (cache_dir / "srv.json").write_text(
        json.dumps({"tools": [], "cached_at": "yesterday"}), encoding="utf-8"
    )
    assert mcps_mod._read_cache("srv") is None


def test_read_cache_returns_none_when_tools_not_list(fake_config: Path, tmp_path: Path) -> None:
    cache_dir = tmp_path / "mcp_cache"
    cache_dir.mkdir()
    import time as _time

    (cache_dir / "srv.json").write_text(
        json.dumps({"tools": "oops", "cached_at": _time.time()}), encoding="utf-8"
    )
    assert mcps_mod._read_cache("srv") is None


def test_read_cache_returns_none_when_tool_entry_not_dict(
    fake_config: Path, tmp_path: Path
) -> None:
    cache_dir = tmp_path / "mcp_cache"
    cache_dir.mkdir()
    import time as _time

    (cache_dir / "srv.json").write_text(
        json.dumps({"tools": ["bad"], "cached_at": _time.time()}), encoding="utf-8"
    )
    assert mcps_mod._read_cache("srv") is None


def test_read_cache_fills_defaults_for_missing_fields(fake_config: Path, tmp_path: Path) -> None:
    """tool entry missing description / input_schema → fill defaults with empty str / empty dict."""
    cache_dir = tmp_path / "mcp_cache"
    cache_dir.mkdir()
    import time as _time

    (cache_dir / "srv.json").write_text(
        json.dumps({"tools": [{"name": "t1"}], "cached_at": _time.time()}),
        encoding="utf-8",
    )
    cached = mcps_mod._read_cache("srv")
    assert cached == [{"name": "t1", "description": "", "input_schema": {}}]


# ─── _daemon_socket_path (identity-derived, exists()-gated) ────────────────


def test_daemon_socket_path_none_when_identity_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ava._boot, "_agent_id", None)
    assert mcps_mod._daemon_socket_path() is None


def test_daemon_socket_path_none_when_socket_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(remote_mod, "_socket_path_for", lambda: str(tmp_path / "absent.sock"))
    assert mcps_mod._daemon_socket_path() is None


def test_daemon_socket_path_returns_path_when_socket_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sock = tmp_path / "present.sock"
    sock.touch()
    monkeypatch.setattr(remote_mod, "_socket_path_for", lambda: str(sock))
    assert mcps_mod._daemon_socket_path() == str(sock)


# ─── _get_remote_client ────────────────────────────────────────────────────


def test_get_remote_client_none_without_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote_mod, "_daemon_socket_path", lambda: None)
    monkeypatch.setattr(remote_mod, "_remote_client", None)
    assert mcps_mod._get_remote_client() is None


def test_get_remote_client_creates_when_daemon_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote_mod, "_daemon_socket_path", lambda: "fake-socket-path")
    monkeypatch.setattr(remote_mod, "_remote_client", None)
    client = mcps_mod._get_remote_client()
    assert isinstance(client, mcps_mod._RemoteMCPClient)
    assert client._socket_path == "fake-socket-path"


def test_get_remote_client_caches_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote_mod, "_daemon_socket_path", lambda: "fake-socket-path")
    monkeypatch.setattr(remote_mod, "_remote_client", None)
    c1 = mcps_mod._get_remote_client()
    c2 = mcps_mod._get_remote_client()
    assert c1 is c2


# ─── remote-path error routing (transport falls back, tool error propagates) ──


def test_list_tools_propagates_tool_error_from_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    # A server-reported tool error must NOT silently fall back to a local re-run.
    class _Remote:
        def list_tools(self, _server: str) -> object:
            raise mcps_mod.MCPCallError("tool blew up")

    monkeypatch.setattr(mcps_mod, "_get_remote_client", _Remote)
    with pytest.raises(mcps_mod.MCPCallError, match="tool blew up"):
        mcps_mod._list_tools("fs")


def test_list_tools_falls_back_to_cache_on_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A transport failure (daemon unreachable) falls back to cache/local.
    class _Remote:
        def list_tools(self, _server: str) -> object:
            raise mcps_mod.MCPConnectError("daemon gone")

    cached = [{"name": "t1", "description": "", "input_schema": {}}]
    monkeypatch.setattr(mcps_mod, "_get_remote_client", _Remote)
    monkeypatch.setattr(mcps_mod, "_read_cache", lambda _s: cached)  # pyright: ignore[reportUnknownArgumentType]
    assert mcps_mod._list_tools("fs") == cached


def test_call_raw_propagates_tool_error_from_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Remote:
        def call_tool(self, _server: str, _tool: str, _args: dict) -> object:
            raise mcps_mod.MCPCallError("permission denied")

    monkeypatch.setattr(mcps_mod, "_get_remote_client", _Remote)
    with pytest.raises(mcps_mod.MCPCallError, match="permission denied"):
        mcps_mod._call_raw("fs", "do")


# ─── _RemoteMCPClient (mock Unix socket) ───────────────────────────────────


class _FakeSocket:
    """Simulate Unix socket — feeds data from the recv_queue injected per test."""

    def __init__(self) -> None:
        self.timeout: float | None = None
        self.connected_to: str | None = None
        self.sent: list[bytes] = []
        self.recv_queue: list[bytes | BaseException] = []
        self.closed = False

    def settimeout(self, s: float) -> None:
        self.timeout = s

    def connect(self, path: str) -> None:
        self.connected_to = path

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, n: int) -> bytes:
        if not self.recv_queue:
            raise OSError("test queue exhausted")
        item = self.recv_queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


def _patch_socket(monkeypatch: pytest.MonkeyPatch, sock: _FakeSocket) -> None:
    """Replace socket.socket(...) to return our fake."""
    monkeypatch.setattr(socket, "socket", lambda *_a, **_kw: sock)  # pyright: ignore[reportUnknownArgumentType]


def test_remote_client_list_tools_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    sock = _FakeSocket()
    sock.recv_queue = [
        json.dumps(
            {
                "id": 1,
                "ok": True,
                "result": [{"name": "t1", "description": "d1", "input_schema": {"type": "object"}}],
            }
        ).encode()
        + b"\n"
    ]
    _patch_socket(monkeypatch, sock)

    client = mcps_mod._RemoteMCPClient("fake-socket-path")
    tools = client.list_tools("srv")

    assert tools == [{"name": "t1", "description": "d1", "input_schema": {"type": "object"}}]
    assert sock.connected_to == "fake-socket-path"
    sent_req = json.loads(sock.sent[0].decode().rstrip())
    assert sent_req["method"] == "list_tools"
    assert sent_req["params"] == {"server": "srv"}
    assert sent_req["id"] == 1


def test_remote_client_call_tool_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    sock = _FakeSocket()
    sock.recv_queue = [
        json.dumps(
            {
                "id": 1,
                "ok": True,
                "result": {"content": [], "isError": False, "structuredContent": None},
            }
        ).encode()
        + b"\n"
    ]
    _patch_socket(monkeypatch, sock)

    client = mcps_mod._RemoteMCPClient("fake-socket-path")
    out = client.call_tool("srv", "tool_x", {"k": "v"})

    assert out == {"content": [], "isError": False, "structuredContent": None}
    sent_req = json.loads(sock.sent[0].decode().rstrip())
    assert sent_req["method"] == "call_tool"
    assert sent_req["params"] == {"server": "srv", "tool": "tool_x", "args": {"k": "v"}}


def test_remote_client_raises_on_error_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """daemon returns `ok: False` → MCPCallError, error field as message."""
    sock = _FakeSocket()
    sock.recv_queue = [
        json.dumps({"id": 1, "ok": False, "error": "permission denied"}).encode() + b"\n"
    ]
    _patch_socket(monkeypatch, sock)

    client = mcps_mod._RemoteMCPClient("fake-socket-path")
    with pytest.raises(mcps_mod.MCPCallError, match="permission denied"):
        client.list_tools("srv")


def test_remote_client_raises_when_connection_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """recv returns empty chunk → peer closed connection → MCPConnectError."""
    sock = _FakeSocket()
    sock.recv_queue = [b""]
    _patch_socket(monkeypatch, sock)

    client = mcps_mod._RemoteMCPClient("fake-socket-path")
    with pytest.raises(mcps_mod.MCPConnectError, match="connection closed"):
        client.list_tools("srv")


def test_remote_client_raises_on_recv_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """sock.recv raises TimeoutError → translates to MCPConnectError for the upper layer."""
    sock = _FakeSocket()
    sock.recv_queue = [TimeoutError()]
    _patch_socket(monkeypatch, sock)

    client = mcps_mod._RemoteMCPClient("fake-socket-path")
    with pytest.raises(mcps_mod.MCPConnectError, match="timeout"):
        client.list_tools("srv")


def test_remote_client_accumulates_chunked_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """response arrives in two parts (TCP-style) → concatenated until \n then parsed."""
    payload = json.dumps({"id": 1, "ok": True, "result": []}).encode() + b"\n"
    sock = _FakeSocket()
    sock.recv_queue = [payload[:10], payload[10:]]  # split in half
    _patch_socket(monkeypatch, sock)

    client = mcps_mod._RemoteMCPClient("fake-socket-path")
    assert client.list_tools("srv") == []


def test_remote_client_request_ids_increment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two consecutive _request calls → id increments (used by daemon side to match request/response)."""
    sock = _FakeSocket()
    sock.recv_queue = [
        json.dumps({"id": 1, "ok": True, "result": []}).encode() + b"\n",
        json.dumps({"id": 2, "ok": True, "result": []}).encode() + b"\n",
    ]
    _patch_socket(monkeypatch, sock)

    client = mcps_mod._RemoteMCPClient("fake-socket-path")
    client.list_tools("srv")
    client.list_tools("srv")
    id1 = json.loads(sock.sent[0].decode().rstrip())["id"]
    id2 = json.loads(sock.sent[-1].decode().rstrip())["id"]
    assert (id1, id2) == (1, 2)


# --- response-id matching + close-on-failure (MCP cross-talk, Task #1147) ---


def test_remote_client_skips_stale_response_with_foreign_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale line — the daemon's late answer to a request whose client-side
    deadline already fired — carries the OLD request's id. The next request
    must skip it and wait for its own id, or every later response shifts by
    one (response stream permanently misaligned: request N+1 reads request
    N's result). The buffer models the state after a timed-out request 1:
    its response (id=1) arrived late, before request 2's own response."""
    sock = _FakeSocket()
    sock.recv_queue = [
        json.dumps({"id": 1, "ok": True, "result": "stale"}).encode() + b"\n",
        json.dumps({"id": 2, "ok": True, "result": "fresh"}).encode() + b"\n",
    ]
    _patch_socket(monkeypatch, sock)

    client = mcps_mod._RemoteMCPClient("fake-socket-path")
    # Model request 1 as already sent and timed out client-side: its id slot is
    # consumed and its late response is what sits at the head of the buffer.
    client._req_id = 1
    # this call sends id=2; it must NOT consume the id=1 stale line
    assert client.call_tool("srv", "t", {}) == "fresh"
    # the stale line was consumed and discarded, stream stays aligned
    assert sock.closed is False


def test_remote_client_skips_notification_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unsolicited notification (no id) interleaved before the response must
    not be consumed as the response — the id match is the discriminator."""
    sock = _FakeSocket()
    sock.recv_queue = [
        json.dumps({"method": "notifications/message", "params": {}}).encode() + b"\n",
        json.dumps({"id": 1, "ok": True, "result": "ok"}).encode() + b"\n",
    ]
    _patch_socket(monkeypatch, sock)

    client = mcps_mod._RemoteMCPClient("fake-socket-path")
    assert client.call_tool("srv", "t", {}) == "ok"


def test_remote_client_skips_stale_and_notification_then_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both foreign-line classes together, in one buffer, before the matching
    response: the client scans until id matches (request id here is 1)."""
    sock = _FakeSocket()
    sock.recv_queue = [
        json.dumps({"id": 7, "ok": True, "result": "very stale"}).encode() + b"\n",
        json.dumps({"method": "notifications/progress", "params": {}}).encode() + b"\n",
        json.dumps({"id": 1, "ok": True, "result": "mine"}).encode() + b"\n",
    ]
    _patch_socket(monkeypatch, sock)

    client = mcps_mod._RemoteMCPClient("fake-socket-path")
    assert client.call_tool("srv", "t", {}) == "mine"


def test_remote_client_closes_socket_on_recv_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A request-timeout leaves the stream ambiguous: the daemon may still be
    processing and its response may arrive later. Closing the socket (and
    forgetting it) means the next request reconnects fresh — a late response
    can never be consumed by the next request (the \u4e32\u8bdd enabler)."""
    sock = _FakeSocket()
    sock.recv_queue = [TimeoutError()]
    _patch_socket(monkeypatch, sock)

    client = mcps_mod._RemoteMCPClient("fake-socket-path")
    with pytest.raises(mcps_mod.MCPConnectError, match="timeout"):
        client.list_tools("srv")
    assert sock.closed is True
    assert client._sock is None, "next request must reconnect fresh"


def test_remote_client_closes_socket_when_connection_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EOF mid-request: same ambiguous-stream argument — close and forget."""
    sock = _FakeSocket()
    sock.recv_queue = [b""]
    _patch_socket(monkeypatch, sock)

    client = mcps_mod._RemoteMCPClient("fake-socket-path")
    with pytest.raises(mcps_mod.MCPConnectError, match="connection closed"):
        client.list_tools("srv")
    assert sock.closed is True
    assert client._sock is None


def test_remote_client_raises_on_malformed_response_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """A truncated / garbage line is a stream-integrity failure: MCPConnectError
    (transport class, so the caller's local fallback engages) and the socket is
    closed for a fresh reconnect."""
    sock = _FakeSocket()
    sock.recv_queue = [b"not json at all\n"]
    _patch_socket(monkeypatch, sock)

    client = mcps_mod._RemoteMCPClient("fake-socket-path")
    with pytest.raises(mcps_mod.MCPConnectError, match="malformed"):
        client.list_tools("srv")
    assert sock.closed is True
    assert client._sock is None


def test_remote_client_error_response_keeps_socket_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """An `ok: False` response is a cleanly-consumed response — the stream stays
    aligned, so the connection is kept (no need to pay a reconnect)."""
    sock = _FakeSocket()
    sock.recv_queue = [json.dumps({"id": 1, "ok": False, "error": "nope"}).encode() + b"\n"]
    _patch_socket(monkeypatch, sock)

    client = mcps_mod._RemoteMCPClient("fake-socket-path")
    with pytest.raises(mcps_mod.MCPCallError, match="nope"):
        client.list_tools("srv")
    assert sock.closed is False
    assert client._sock is sock


# ─── _list_tools / _call_raw remote daemon path + cache fallback ─────────────


def test_list_tools_uses_remote_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """daemon available → directly use remote.list_tools, not reading cache / not connecting subprocess."""
    fake_remote = MagicMock()
    fake_remote.list_tools.return_value = [{"name": "t1", "description": "d", "input_schema": {}}]
    monkeypatch.setattr(mcps_mod, "_get_remote_client", lambda: fake_remote)

    tools = mcps_mod._list_tools("srv")
    assert tools == [{"name": "t1", "description": "d", "input_schema": {}}]
    fake_remote.list_tools.assert_called_once_with("srv")


def test_list_tools_falls_back_to_cache_when_remote_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """daemon throws → falls back to disk cache."""
    fake_remote = MagicMock()
    fake_remote.list_tools.side_effect = OSError("daemon down")
    monkeypatch.setattr(mcps_mod, "_get_remote_client", lambda: fake_remote)

    cached: list[mcps_mod.ToolInfo] = [
        {"name": "cached_tool", "description": "", "input_schema": {}}
    ]
    monkeypatch.setattr(mcps_mod, "_read_cache", lambda _s: cached)  # pyright: ignore[reportUnknownArgumentType]

    tools = mcps_mod._list_tools("srv")
    assert tools == cached


def test_call_raw_uses_remote_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_remote = MagicMock()
    fake_remote.call_tool.return_value = {
        "content": [{"type": "text", "text": "ok"}],
        "isError": False,
        "structuredContent": None,
    }
    monkeypatch.setattr(mcps_mod, "_get_remote_client", lambda: fake_remote)

    out = mcps_mod._call_raw("srv", "tool_x", arg="v")
    assert out["content"][0]["text"] == "ok"
    fake_remote.call_tool.assert_called_once_with("srv", "tool_x", {"arg": "v"})


def test_call_raw_falls_back_to_local_when_remote_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """daemon crashed → fallback to local _connect path."""
    fake_remote = MagicMock()
    fake_remote.call_tool.side_effect = OSError("daemon down")
    monkeypatch.setattr(mcps_mod, "_get_remote_client", lambda: fake_remote)

    # Use existing mock_session flavor patch
    fake_session = MagicMock()
    fake_session.call_tool = AsyncMock(
        return_value=MagicMock(
            content=[_content_text("ok")],
            is_error=False,
            structured_content=None,
        )
    )

    async def _fake_connect(_server: str, **_kw: object) -> MagicMock:
        return fake_session

    monkeypatch.setattr(mcps_mod, "_connect", _fake_connect)
    monkeypatch.setattr(mcps_mod, "_sessions", {})

    out = mcps_mod._call_raw("srv", "tool_x", k="v")
    assert out["content"][0]["text"] == "ok"


# ─── _connect bad-config branches (running real _connect, inside background loop) ───


def test_list_tools_raises_when_server_not_in_config(
    fake_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_connect` sees server not in mcp.json → MCPServerNotFound leaks to _list_tools."""
    fake_config.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    monkeypatch.setattr(remote_mod, "_daemon_socket_path", lambda: None)
    monkeypatch.setattr(remote_mod, "_remote_client", None)
    monkeypatch.setattr(mcps_mod, "_sessions", {})
    monkeypatch.setattr(mcps_mod, "_session_locks", {})
    monkeypatch.setattr(mcps_mod, "_read_cache", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]

    with pytest.raises(mcps_mod.MCPServerNotFound, match="nope"):
        mcps_mod._list_tools("nope")


def test_list_tools_raises_when_command_field_invalid(
    fake_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Config missing the command field → MCPError immediately (fail-fast)."""
    fake_config.write_text(json.dumps({"mcpServers": {"bad": {}}}), encoding="utf-8")
    monkeypatch.setattr(remote_mod, "_daemon_socket_path", lambda: None)
    monkeypatch.setattr(remote_mod, "_remote_client", None)
    monkeypatch.setattr(mcps_mod, "_sessions", {})
    monkeypatch.setattr(mcps_mod, "_session_locks", {})
    monkeypatch.setattr(mcps_mod, "_read_cache", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]

    with pytest.raises(mcps_mod.MCPError, match="command"):
        mcps_mod._list_tools("bad")


def test_list_tools_raises_when_command_not_str(
    fake_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_config.write_text(json.dumps({"mcpServers": {"bad": {"command": 123}}}), encoding="utf-8")
    monkeypatch.setattr(remote_mod, "_daemon_socket_path", lambda: None)
    monkeypatch.setattr(remote_mod, "_remote_client", None)
    monkeypatch.setattr(mcps_mod, "_sessions", {})
    monkeypatch.setattr(mcps_mod, "_session_locks", {})
    monkeypatch.setattr(mcps_mod, "_read_cache", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]

    with pytest.raises(mcps_mod.MCPError, match="command"):
        mcps_mod._list_tools("bad")


def test_list_tools_uses_url_for_remote_server(
    fake_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `url` entry connects via the HTTP path, not the stdio child path."""
    fake_config.write_text(
        json.dumps({"mcpServers": {"remote": {"url": "https://mcp.example.com/mcp"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(remote_mod, "_daemon_socket_path", lambda: None)
    monkeypatch.setattr(remote_mod, "_remote_client", None)
    monkeypatch.setattr(mcps_mod, "_sessions", {})
    monkeypatch.setattr(mcps_mod, "_session_locks", {})
    monkeypatch.setattr(mcps_mod, "_read_cache", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(mcps_mod, "_write_cache", lambda _s, _t: None)  # pyright: ignore[reportUnknownArgumentType]

    tool = MagicMock(spec=["name", "description", "input_schema"])
    tool.name = "scrape"
    tool.description = "d"
    tool.input_schema = {"type": "object"}
    connect_http = AsyncMock(
        return_value=MagicMock(list_tools=AsyncMock(return_value=MagicMock(tools=[tool])))
    )
    monkeypatch.setattr(mcps_mod, "_connect_http", connect_http)

    tools = mcps_mod._list_tools("remote")

    connect_http.assert_awaited_once_with("https://mcp.example.com/mcp", None, server="remote")
    assert tools == [{"name": "scrape", "description": "d", "input_schema": {"type": "object"}}]


def test_connect_http_local_fallback_initializes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local-fallback HTTP connect mirrors the daemon: transport -> session -> initialize."""
    read, write = object(), object()
    streams = MagicMock()
    streams.__aenter__ = AsyncMock(return_value=(read, write))
    streams.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=streams)
    monkeypatch.setattr("mcp.client.streamable_http.streamable_http_client", factory)
    session = MagicMock()
    session.initialize = AsyncMock()
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    client_session_cls = MagicMock(return_value=session_cm)
    monkeypatch.setattr("mcp.ClientSession", client_session_cls)
    client_factory = MagicMock(return_value=object())
    monkeypatch.setattr("mcp.client.streamable_http.create_mcp_http_client", client_factory)

    got = mcps_mod._run_async(
        mcps_mod._connect_http("https://mcp.example.com/mcp", {"x-api-key": "k"})
    )

    assert got is session
    session.initialize.assert_awaited_once()
    client_factory.assert_called_once_with(headers={"x-api-key": "k"})
    assert factory.call_args.kwargs["http_client"] is client_factory.return_value
    # every local-fallback request must be bounded by the same timeout knob —
    # the SDK default (None) would block the calling agent forever on a hung server.
    assert client_session_cls.call_args.kwargs["read_timeout_seconds"] == (
        settings.sandbox.mcp_connect_timeout_seconds
    )


def test_connect_http_local_fallback_timeout_raises_connect_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _hang(*_a: Any, **_k: Any) -> Any:
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=TimeoutError("slow endpoint"))
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    monkeypatch.setattr(
        "mcp.client.streamable_http.streamable_http_client",
        MagicMock(return_value=_hang()),
    )

    with pytest.raises(mcps_mod.MCPConnectError, match="timed out"):
        mcps_mod._run_async(mcps_mod._connect_http("https://mcp.example.com/mcp", None))


def test_connect_stdio_sets_request_timeout_on_client_session(
    fake_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local-fallback stdio session gets a per-request timeout — without it
    (SDK default None) a hung server blocks `fut.result()` forever."""
    fake_config.write_text(
        json.dumps({"mcpServers": {"fs": {"command": "uvx", "args": ["mcp-server-filesystem"]}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(remote_mod, "_daemon_socket_path", lambda: None)
    monkeypatch.setattr(remote_mod, "_remote_client", None)
    monkeypatch.setattr(mcps_mod, "_sessions", {})
    monkeypatch.setattr(mcps_mod, "_session_locks", {})

    read, write = object(), object()
    streams = MagicMock()
    streams.__aenter__ = AsyncMock(return_value=(read, write))
    streams.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("mcp.client.stdio.stdio_client", MagicMock(return_value=streams))
    session = MagicMock()
    session.initialize = AsyncMock()
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    client_session_cls = MagicMock(return_value=session_cm)
    monkeypatch.setattr("mcp.ClientSession", client_session_cls)

    got = mcps_mod._run_async(mcps_mod._connect("fs"))

    assert got is session
    session.initialize.assert_awaited_once()
    assert client_session_cls.call_args.kwargs["read_timeout_seconds"] == (
        settings.sandbox.mcp_connect_timeout_seconds
    )


# ─── _dump_content fail-fast on unknown content type ──────────────────────


def test_dump_content_raises_on_unknown_block() -> None:
    """content block without model_dump → MCPError (fail-fast, not silently swallowed)."""

    class _Unknown:
        pass

    with pytest.raises(mcps_mod.MCPError, match="Unrecognized"):
        mcps_mod._dump_content(_Unknown())


# ─── help() ────────────────────────────────────────────────────────────────


def test_help_prints_empty_message_when_no_servers(
    fake_config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mcps_mod.help()
    out = capsys.readouterr().out
    assert "no MCP servers configured" in out


def test_help_lists_each_configured_server(
    fake_config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fs": {"command": "uvx", "args": ["mcp-server-filesystem", "/some/path"]},
                    "chrome": {"command": "npx", "args": ["chrome-mcp@latest"]},
                }
            }
        ),
        encoding="utf-8",
    )
    mcps_mod.help()
    out = capsys.readouterr().out
    assert "fs/" in out
    assert "chrome/" in out
    assert "uvx" in out
    assert "npx" in out
    assert "mcp-server-filesystem" in out


# ─── _ServerProxy detail paths ────────────────────────────────────────────


def test_proxy_getattr_rejects_underscore_names(mock_session: MagicMock) -> None:
    """`proxy._anything` (dunder / private) goes through AttributeError — not treated as tool call."""
    proxy = mcps_mod._ServerProxy("fs")
    with pytest.raises(AttributeError):
        proxy._not_a_tool  # noqa: B018 — intentionally trigger __getattr__


def test_proxy_tool_docstring_includes_schema(mock_session: MagicMock) -> None:
    """tool has input_schema → docstring includes JSON schema (for agent to see)."""
    mock_session.list_tools.return_value = MagicMock(
        tools=[
            _make_tool(
                "read_file",
                "Read a file",
                schema={"type": "object", "properties": {"path": {"type": "string"}}},
            )
        ]
    )
    proxy = mcps_mod._ServerProxy("fs")
    fn = proxy.read_file
    doc = fn.__doc__ or ""
    assert "Input schema" in doc
    assert "path" in doc
    assert "string" in doc


def test_proxy_all_prefers_disk_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """`__all_for_ava__` hits disk cache then stops — should not trigger _connect (server startup is expensive)."""
    cached: list[mcps_mod.ToolInfo] = [
        {"name": "alpha", "description": "", "input_schema": {}},
        {"name": "beta", "description": "", "input_schema": {}},
    ]
    monkeypatch.setattr(mcps_mod, "_read_cache", lambda _s: cached)  # pyright: ignore[reportUnknownArgumentType]

    async def _fail_connect(*_a: object, **_kw: object) -> None:
        raise AssertionError("_connect should not be called when disk cache hits")

    monkeypatch.setattr(mcps_mod, "_connect", _fail_connect)

    proxy = mcps_mod._ServerProxy("fs")
    assert proxy.__all_for_ava__ == ["alpha", "beta"]


def test_proxy_load_tool_names_uses_in_memory_cache_on_second_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second call to _load_tool_names → directly reads self._tools_cache without hitting disk again."""
    calls = {"n": 0}

    def _spy_read_cache(_s: str) -> list[mcps_mod.ToolInfo]:
        calls["n"] += 1
        return [{"name": "t1", "description": "", "input_schema": {}}]

    monkeypatch.setattr(mcps_mod, "_read_cache", _spy_read_cache)

    proxy = mcps_mod._ServerProxy("fs")
    proxy._load_tool_names()  # first: disk cache fills _tools_cache
    proxy._load_tool_names()  # second: uses in-memory cache
    assert calls["n"] == 1  # disk read only once


# ─── _schema_to_signature (display-only, no validation) ───────────────────


def test_schema_to_signature_renders_required_and_optional() -> None:
    """Has properties → synthesizes keyword-only signature: required no default, optional default None,
    returns str. Purely display, no validation."""
    schema = {
        "type": "object",
        "properties": {"url": {"type": "string"}, "timeout": {"type": "integer"}},
        "required": ["url"],
    }
    sig = mcps_mod._schema_to_signature(schema)
    assert sig is not None
    params = sig.parameters
    assert list(params) == ["url", "timeout"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())
    assert params["url"].default is inspect.Parameter.empty
    assert params["url"].annotation is str
    assert params["timeout"].default is None
    assert params["timeout"].annotation is int
    assert sig.return_annotation is str


def test_schema_to_signature_none_without_properties() -> None:
    """Empty schema / no properties / properties empty dict → None (caller retains (**kwargs))."""
    assert mcps_mod._schema_to_signature({}) is None
    assert mcps_mod._schema_to_signature({"type": "object"}) is None
    assert mcps_mod._schema_to_signature({"properties": {}}) is None


def test_schema_to_signature_none_for_non_identifier_name() -> None:
    """Parameter name is not a valid identifier (hyphen / starts with digit) → overall fallback to (**kwargs)."""
    assert mcps_mod._schema_to_signature({"properties": {"page-size": {"type": "integer"}}}) is None


def test_schema_to_signature_none_for_python_keyword_name() -> None:
    """Parameter name collides with Python keyword (`from`) → fallback (.isidentifier() returns True for keywords)."""
    assert mcps_mod._schema_to_signature({"properties": {"from": {"type": "string"}}}) is None


def test_schema_to_signature_unknown_type_falls_back_to_any() -> None:
    """type is a list (`["string","null"]`) / omitted / non-primitive name → annotation falls back to Any, no crash."""
    schema: dict[str, Any] = {
        "properties": {"a": {"type": ["string", "null"]}, "b": {}, "c": {"type": "geo"}},
        "required": [],
    }
    sig = mcps_mod._schema_to_signature(schema)
    assert sig is not None
    assert all(p.annotation is Any for p in sig.parameters.values())


def test_make_tool_callable_attaches_signature(mock_session: MagicMock) -> None:
    """proxy.<tool> callable carries signature synthesized from schema → inspect.signature sees real names."""
    mock_session.list_tools.return_value = MagicMock(
        tools=[
            _make_tool(
                "navigate",
                "Go to a URL",
                schema={
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
            )
        ]
    )
    proxy = mcps_mod._ServerProxy("chrome")
    sig = inspect.signature(proxy.navigate)
    assert list(sig.parameters) == ["url"]
    assert sig.parameters["url"].annotation is str


def test_unknown_tool_keeps_kwargs_signature(mock_session: MagicMock) -> None:
    """No schema (info=None) → no signature attached, retains (**kwargs)."""
    mock_session.list_tools.return_value = MagicMock(tools=[])
    proxy = mcps_mod._ServerProxy("chrome")
    params = inspect.signature(proxy.whatever).parameters
    assert list(params) == ["kwargs"]
    assert params["kwargs"].kind is inspect.Parameter.VAR_KEYWORD


def test_help_renderer_shows_real_params_for_mcp_tool(mock_session: MagicMock) -> None:
    """ava.help's signature renderer (_format_signature) displays real parameter names for MCP tools,
    no longer (**kwargs: Any)."""
    from ava import _format_signature

    mock_session.list_tools.return_value = MagicMock(
        tools=[
            _make_tool(
                "navigate",
                schema={
                    "properties": {"url": {"type": "string"}, "timeout": {"type": "integer"}},
                    "required": ["url"],
                },
            )
        ]
    )
    proxy = mcps_mod._ServerProxy("chrome")
    rendered = _format_signature(proxy.navigate)
    assert "url: str" in rendered
    assert "timeout: int" in rendered
    assert "**kwargs" not in rendered
