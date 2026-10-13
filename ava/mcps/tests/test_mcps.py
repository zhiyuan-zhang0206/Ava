"""ava.mcps unit tests — config parsing + namespace dispatch + tool invocation paths.

Do not run real MCP servers (requires external npm/uvx packages, too heavy to add to test deps). Use monkeypatch
to point ava_home() to tmpdir + fake `_connect()` returning mock session to verify
namespace dispatch and result processing.

Real integration tests (`ava.mcps.chrome.navigate_page(...)` running against real server) are left for manual testing.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import ava.mcps as mcps_mod
import ava.mcps._remote as remote_mod
from ava.mcps._tools import DiskCache, ServerProxy, call_raw, list_tools
from ava.mcps.tests._mcps_helpers import (
    StoredTools,
    _result,
    content_image,
    content_text,
    local_mcp_clients,
    make_tool,
    tool_source,
)
from ava.mcps.tests._mcps_helpers import fake_config as fake_config
from ava.mcps.tests._mcps_helpers import mock_session as mock_session
from tests.fixtures.pin_agent import pin_no_identity

# ─── _load_config / servers() ────────────────────────────────────────────


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
        mcps_mod.load_config()


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
    assert isinstance(proxy, ServerProxy)
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


def test_the_agent_visible_surface_is_the_helpers_plus_the_configured_servers(
    fake_config: Path,
) -> None:
    """`__all_for_ava__` is declared on the module class and computed live, so `help()` and the
    metering wrappers see exactly `servers` / `description` and each configured server, however
    many names the module itself gains."""
    assert mcps_mod.__all_for_ava__ == ["description", "servers"]
    fake_config.write_text(
        json.dumps({"mcpServers": {"fs": {"command": "x"}, "github": {"command": "y"}}}),
        encoding="utf-8",
    )
    assert mcps_mod.__all_for_ava__ == ["description", "fs", "github", "servers"]


# ─── tools / call / call_raw via ServerProxy ─────────────────────────────


def test_proxy_dir_lists_tools_plus_raw(mock_session: MagicMock) -> None:
    """`dir(proxy)` goes through list_tools to get tool names + `raw` is also present (visible in ava.help)."""
    mock_session.list_tools.return_value = MagicMock(
        tools=[make_tool("read_file"), make_tool("write_file")]
    )
    proxy = ServerProxy("fs", mock_session.source)
    listing = dir(proxy)
    assert listing == ["raw", "read_file", "write_file"]


def test_proxy_attribute_returns_callable(mock_session: MagicMock) -> None:
    """`proxy.read_file` returns a callable, calling it goes through call_tool."""
    mock_session.list_tools.return_value = MagicMock(tools=[make_tool("read_file", "Read a file")])
    mock_session.call_tool.return_value = _result([content_text("file contents")])
    proxy = ServerProxy("fs", mock_session.source)
    fn = proxy.read_file
    assert callable(fn)
    assert fn.__name__ == "read_file"
    assert "Read a file" in (fn.__doc__ or "")
    result = fn(path="/x")
    assert result == "file contents"
    mock_session.call_tool.assert_called_once_with("read_file", {"path": "/x"})


def test_proxy_attribute_works_for_unknown_tool_too(mock_session: MagicMock) -> None:
    """Tool names not in cache also return callable — MCP server side will reject wrong names,
    error message is more reliable."""
    mock_session.list_tools.return_value = MagicMock(tools=[])
    mock_session.call_tool.return_value = _result([content_text("ok")])
    proxy = ServerProxy("fs", mock_session.source)
    fn = proxy.unknown_tool
    assert callable(fn)
    assert fn() == "ok"


def test_proxy_call_joins_text_blocks(mock_session: MagicMock) -> None:
    mock_session.list_tools.return_value = MagicMock(tools=[make_tool("do")])
    mock_session.call_tool.return_value = _result([content_text("line 1"), content_text("line 2")])
    proxy = ServerProxy("fs", mock_session.source)
    assert proxy.do() == "line 1\nline 2"


def test_proxy_call_raises_on_is_error(mock_session: MagicMock) -> None:
    mock_session.list_tools.return_value = MagicMock(tools=[make_tool("do")])
    mock_session.call_tool.return_value = _result(
        [content_text("permission denied")], is_error=True
    )
    proxy = ServerProxy("fs", mock_session.source)
    with pytest.raises(mcps_mod.MCPCallError, match="permission denied"):
        proxy.do()


def test_proxy_call_raises_on_non_text_content(mock_session: MagicMock) -> None:
    """tool returned image / other non-text content → guides to use .raw()."""
    mock_session.list_tools.return_value = MagicMock(tools=[make_tool("screenshot")])
    mock_session.call_tool.return_value = _result([content_image("b64==")])
    proxy = ServerProxy("fs", mock_session.source)
    with pytest.raises(mcps_mod.MCPCallError, match=r"\.raw"):
        proxy.screenshot()


def test_proxy_call_empty_content_returns_empty_string(mock_session: MagicMock) -> None:
    mock_session.list_tools.return_value = MagicMock(tools=[make_tool("noop")])
    mock_session.call_tool.return_value = _result([])
    proxy = ServerProxy("fs", mock_session.source)
    assert proxy.noop() == ""


def test_proxy_call_empty_text_block_returns_empty_string(mock_session: MagicMock) -> None:
    """A response of all-text blocks that join to "" (e.g. chrome list_pages
    with no open pages) is a legitimate "" — not a spurious
    'non-text content (kinds=[text])' error."""
    mock_session.list_tools.return_value = MagicMock(tools=[make_tool("list_pages")])
    mock_session.call_tool.return_value = _result([content_text("")])
    proxy = ServerProxy("fs", mock_session.source)
    assert proxy.list_pages() == ""


def test_proxy_call_empty_text_with_image_still_raises(mock_session: MagicMock) -> None:
    """An empty text block alongside a non-text block still points at .raw() —
    the real payload (image) can't be text-joined."""
    mock_session.list_tools.return_value = MagicMock(tools=[make_tool("shot")])
    mock_session.call_tool.return_value = _result([content_text(""), content_image("b64==")])
    proxy = ServerProxy("fs", mock_session.source)
    with pytest.raises(mcps_mod.MCPCallError, match=r"\.raw"):
        proxy.shot()


def test_proxy_raw_returns_full_structure(mock_session: MagicMock) -> None:
    mock_session.list_tools.return_value = MagicMock(tools=[make_tool("do")])
    mock_session.call_tool.return_value = _result(
        [content_text("ok"), content_image("xyz==")],
        is_error=False,
        structured={"counted": 42},
    )
    proxy = ServerProxy("fs", mock_session.source)
    out = proxy.raw("do", arg1="v")
    assert out["isError"] is False
    assert out["structuredContent"] == {"counted": 42}
    assert len(out["content"]) == 2
    assert out["content"][0] == {"type": "text", "text": "ok"}
    assert out["content"][1] == {"type": "image", "data": "xyz==", "mimeType": "image/png"}


def test_proxy_raw_does_not_raise_on_is_error(mock_session: MagicMock) -> None:
    """raw does not convert isError to raise — returns full structure for caller to decide."""
    mock_session.list_tools.return_value = MagicMock(tools=[make_tool("x")])
    mock_session.call_tool.return_value = _result(
        [content_text("permission denied")], is_error=True
    )
    proxy = ServerProxy("fs", mock_session.source)
    out = proxy.raw("x")
    assert out["isError"] is True


def test_proxy_call_structured_only_raises(mock_session: MagicMock) -> None:
    """Only returned structuredContent, no text → raise (guides to use .raw())."""
    mock_session.list_tools.return_value = MagicMock(tools=[make_tool("count")])
    mock_session.call_tool.return_value = _result([], structured={"counted": 42})
    proxy = ServerProxy("fs", mock_session.source)
    with pytest.raises(mcps_mod.MCPCallError, match="structuredContent"):
        proxy.count()


# ─── dead-session rebuild (2026-08-13 #1229) ──────────────────────────────


# ─── _load_config edge cases ──────────────────────────────────────────────


def test_load_config_raises_when_section_not_dict(fake_config: Path) -> None:
    """`mcpServers` is not a dict (typo / old format) → MCPError immediately, not silently go empty."""
    fake_config.write_text(json.dumps({"mcpServers": [1, 2, 3]}), encoding="utf-8")
    with pytest.raises(mcps_mod.MCPError, match="mcpServers field is not a dict"):
        mcps_mod.load_config()


def _disk_cache(tmp_path: Path) -> DiskCache:
    return DiskCache(lambda: tmp_path / "mcp_cache")


# ─── disk cache (DiskCache.read / write) ──────────────────────────────


def test_cache_write_then_read_roundtrip(tmp_path: Path) -> None:
    """`_write_cache` write + `_read_cache` read → get back the same tools."""
    tools: list[mcps_mod.ToolInfo] = [
        {"name": "t1", "description": "d1", "input_schema": {"type": "object"}}
    ]
    _disk_cache(tmp_path).write("srv", tools)
    cached = _disk_cache(tmp_path).read("srv")
    assert cached == tools


def test_read_cache_returns_none_when_missing(tmp_path: Path) -> None:
    assert _disk_cache(tmp_path).read("never_written") is None


def test_read_cache_returns_none_on_bad_json(fake_config: Path, tmp_path: Path) -> None:
    cache_dir = tmp_path / "mcp_cache"
    cache_dir.mkdir()
    (cache_dir / "srv.json").write_text("not json {{{", encoding="utf-8")
    assert _disk_cache(tmp_path).read("srv") is None


def test_read_cache_returns_none_when_expired(fake_config: Path, tmp_path: Path) -> None:
    """cached_at=0 → age ≈ now → exceeds 24h TTL, considered expired."""
    cache_dir = tmp_path / "mcp_cache"
    cache_dir.mkdir()
    (cache_dir / "srv.json").write_text(json.dumps({"tools": [], "cached_at": 0}), encoding="utf-8")
    assert _disk_cache(tmp_path).read("srv") is None


def test_read_cache_returns_none_without_cached_at(fake_config: Path, tmp_path: Path) -> None:
    cache_dir = tmp_path / "mcp_cache"
    cache_dir.mkdir()
    (cache_dir / "srv.json").write_text(json.dumps({"tools": []}), encoding="utf-8")
    assert _disk_cache(tmp_path).read("srv") is None


def test_read_cache_returns_none_when_cached_at_wrong_type(
    fake_config: Path, tmp_path: Path
) -> None:
    cache_dir = tmp_path / "mcp_cache"
    cache_dir.mkdir()
    (cache_dir / "srv.json").write_text(
        json.dumps({"tools": [], "cached_at": "yesterday"}), encoding="utf-8"
    )
    assert _disk_cache(tmp_path).read("srv") is None


def test_read_cache_returns_none_when_tools_not_list(fake_config: Path, tmp_path: Path) -> None:
    cache_dir = tmp_path / "mcp_cache"
    cache_dir.mkdir()
    import time as _time

    (cache_dir / "srv.json").write_text(
        json.dumps({"tools": "oops", "cached_at": _time.time()}), encoding="utf-8"
    )
    assert _disk_cache(tmp_path).read("srv") is None


def test_read_cache_returns_none_when_tool_entry_not_dict(
    fake_config: Path, tmp_path: Path
) -> None:
    cache_dir = tmp_path / "mcp_cache"
    cache_dir.mkdir()
    import time as _time

    (cache_dir / "srv.json").write_text(
        json.dumps({"tools": ["bad"], "cached_at": _time.time()}), encoding="utf-8"
    )
    assert _disk_cache(tmp_path).read("srv") is None


def test_read_cache_fills_defaults_for_missing_fields(fake_config: Path, tmp_path: Path) -> None:
    """tool entry missing description / input_schema → fill defaults with empty str / empty dict."""
    cache_dir = tmp_path / "mcp_cache"
    cache_dir.mkdir()
    import time as _time

    (cache_dir / "srv.json").write_text(
        json.dumps({"tools": [{"name": "t1"}], "cached_at": _time.time()}),
        encoding="utf-8",
    )
    cached = _disk_cache(tmp_path).read("srv")
    assert cached == [{"name": "t1", "description": "", "input_schema": {}}]


# ─── daemon_socket_path (identity-derived, exists()-gated) ────────────────


def test_daemon_socket_path_none_when_identity_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    pin_no_identity()
    assert remote_mod.daemon_socket_path() is None


def test_daemon_socket_path_none_when_socket_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(remote_mod, "_socket_path_for", lambda: str(tmp_path / "absent.sock"))
    assert remote_mod.daemon_socket_path() is None


def test_daemon_socket_path_returns_path_when_socket_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sock = tmp_path / "present.sock"
    sock.touch()
    monkeypatch.setattr(remote_mod, "_socket_path_for", lambda: str(sock))
    assert remote_mod.daemon_socket_path() == str(sock)


# ─── McpClients.remote ─────────────────────────────────────────────────────


def test_remote_none_without_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote_mod, "daemon_socket_path", lambda: None)
    with pytest.raises(mcps_mod.MCPConnectError, match="unavailable"):
        local_mcp_clients().remote()


def test_remote_creates_when_daemon_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote_mod, "daemon_socket_path", lambda: "fake-socket-path")
    client = local_mcp_clients().remote()
    assert isinstance(client, remote_mod.RemoteMCPClient)
    assert client._socket_path == "fake-socket-path"


def test_remote_caches_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote_mod, "daemon_socket_path", lambda: "fake-socket-path")
    clients = local_mcp_clients()
    assert clients.remote() is clients.remote()


# ─── remote-path error routing (transport falls back, tool error propagates) ──


def test_list_tools_propagates_tool_error_from_remote() -> None:
    # A server-reported tool error must NOT silently fall back to a local re-run.
    class _Remote:
        def list_tools(self, _server: str, *, timeout_seconds: float) -> object:
            raise mcps_mod.MCPCallError("tool blew up")

    with pytest.raises(mcps_mod.MCPCallError, match="tool blew up"):
        list_tools(tool_source(_Remote), "fs")  # pyright: ignore[reportArgumentType]


def test_list_tools_falls_back_to_cache_on_transport_error() -> None:
    # A transport failure (daemon unreachable) falls back to cache/local.
    class _Remote:
        def list_tools(self, _server: str, *, timeout_seconds: float) -> object:
            raise mcps_mod.MCPConnectError("daemon gone")

    cached: list[mcps_mod.ToolInfo] = [{"name": "t1", "description": "", "input_schema": {}}]
    source = tool_source(_Remote, cache=StoredTools(cached))  # pyright: ignore[reportArgumentType]
    assert list_tools(source, "fs") == cached


def test_call_raw_propagates_tool_error_from_remote() -> None:
    class _Remote:
        def call_tool(
            self, _server: str, _tool: str, _args: dict, *, timeout_seconds: float
        ) -> object:
            raise mcps_mod.MCPCallError("permission denied")

    with pytest.raises(mcps_mod.MCPCallError, match="permission denied"):
        call_raw(tool_source(_Remote), "fs", "do")  # pyright: ignore[reportArgumentType]
