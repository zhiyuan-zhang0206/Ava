"""`ava._mcp_config` — the shared MCP config loader.

Covers the three concerns the loader factors out of the two `_load_config`
call sites: reading one file's `mcpServers` section (with fail-fast on bad
input), discovering plugin-bundled `.mcp.json`, and merging plugin defaults
under the machine config (machine wins on a name collision).

`unit_home` points `ava_home()` (and therefore `plugins_dir()`) at a tmp dir.
Merge/precedence tests monkeypatch `_plugin_config_paths` so they are isolated
from whatever the real repo `plugins/` ship.
"""

import json
from pathlib import Path
from typing import Any

import pytest

import ava._mcp_config as cfg_mod


def _write(path: Path, servers: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


# ─── _machine_config_path ────────────────────────────────────────────────


def test_machine_config_path_uses_ava_home(unit_home: Path) -> None:
    assert cfg_mod._machine_config_path() == unit_home / "mcp.json"


# ─── _read_servers ───────────────────────────────────────────────────────


def test_read_servers_empty_when_no_file(tmp_path: Path) -> None:
    assert cfg_mod._read_servers(tmp_path / "absent.json") == {}


def test_read_servers_empty_when_no_section(tmp_path: Path) -> None:
    """A generic settings file lacking `mcpServers` contributes nothing."""
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps({"other": {}}), encoding="utf-8")
    assert cfg_mod._read_servers(p) == {}


def test_read_servers_parses_section(tmp_path: Path) -> None:
    p = tmp_path / "mcp.json"
    _write(p, {"fs": {"command": "x"}, "github": {"command": "y"}})
    assert cfg_mod._read_servers(p) == {"fs": {"command": "x"}, "github": {"command": "y"}}


def test_read_servers_raises_on_bad_json(tmp_path: Path) -> None:
    p = tmp_path / "mcp.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(cfg_mod.MCPError, match="Failed to read"):
        cfg_mod._read_servers(p)


def test_read_servers_raises_when_section_not_dict(tmp_path: Path) -> None:
    """`mcpServers` present but not an object → fail fast, don't silently empty."""
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps({"mcpServers": [1, 2, 3]}), encoding="utf-8")
    with pytest.raises(cfg_mod.MCPError, match="mcpServers field is not a dict"):
        cfg_mod._read_servers(p)


def test_read_servers_raises_when_top_level_not_object(tmp_path: Path) -> None:
    """Valid JSON but not an object (e.g. a list) → MCPError, not a raw AttributeError."""
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(cfg_mod.MCPError, match="is not a JSON object"):
        cfg_mod._read_servers(p)


def test_read_servers_empty_when_section_null(tmp_path: Path) -> None:
    """Explicit `"mcpServers": null` reads as "no servers" (same as omitting it) —
    the section is optional, so null collapses to empty rather than raising."""
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps({"mcpServers": None}), encoding="utf-8")
    assert cfg_mod._read_servers(p) == {}


# ─── _plugin_config_paths (discovery) ────────────────────────────────────


def test_plugin_config_paths_finds_installed_plugin(unit_home: Path) -> None:
    """An installed plugin's `.mcp.json` under `$AVA_HOME/plugins/<name>/` is discovered."""
    mcp_json = unit_home / "plugins" / "myplugin" / ".mcp.json"
    _write(mcp_json, {"fs": {"command": "x"}})
    assert mcp_json in cfg_mod._plugin_config_paths()


def test_plugin_config_paths_ignores_plugin_without_mcp_json(unit_home: Path) -> None:
    """A plugin dir with no `.mcp.json` contributes no path."""
    (unit_home / "plugins" / "noplugin").mkdir(parents=True)
    assert all(p.parent.name != "noplugin" for p in cfg_mod._plugin_config_paths())


# ─── load_mcp_config (merge + precedence) ────────────────────────────────


def test_load_empty_when_nothing_configured(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cfg_mod, "_builtin_mcp_paths", list)
    monkeypatch.setattr(cfg_mod, "_plugin_config_paths", list)
    assert cfg_mod.load_mcp_config() == {}


def test_load_returns_machine_servers(unit_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg_mod, "_builtin_mcp_paths", list)
    monkeypatch.setattr(cfg_mod, "_plugin_config_paths", list)
    _write(unit_home / "mcp.json", {"fs": {"command": "machine"}})
    assert cfg_mod.load_mcp_config() == {"fs": {"command": "machine"}}


def test_load_returns_plugin_servers_when_no_machine_config(
    unit_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_json = tmp_path / "plugin" / ".mcp.json"
    _write(plugin_json, {"fs": {"command": "plugin"}})
    monkeypatch.setattr(cfg_mod, "_builtin_mcp_paths", list)
    monkeypatch.setattr(cfg_mod, "_plugin_config_paths", lambda: [plugin_json])
    assert cfg_mod.load_mcp_config() == {"fs": {"command": "plugin"}}


def test_machine_overrides_plugin_on_name_collision(
    unit_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A machine entry wins over a plugin's same-named default; non-colliding
    plugin servers still surface."""
    plugin_json = tmp_path / "plugin" / ".mcp.json"
    _write(plugin_json, {"fs": {"command": "plugin"}, "extra": {"command": "p"}})
    monkeypatch.setattr(cfg_mod, "_builtin_mcp_paths", list)
    monkeypatch.setattr(cfg_mod, "_plugin_config_paths", lambda: [plugin_json])
    _write(unit_home / "mcp.json", {"fs": {"command": "machine"}})

    merged = cfg_mod.load_mcp_config()
    assert merged == {"fs": {"command": "machine"}, "extra": {"command": "p"}}


def test_later_plugin_overrides_earlier(
    unit_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Within plugin paths, later entries override earlier ones (apply order)."""
    first = tmp_path / "a" / ".mcp.json"
    second = tmp_path / "b" / ".mcp.json"
    _write(first, {"fs": {"command": "first"}})
    _write(second, {"fs": {"command": "second"}})
    monkeypatch.setattr(cfg_mod, "_builtin_mcp_paths", list)
    monkeypatch.setattr(cfg_mod, "_plugin_config_paths", lambda: [first, second])
    assert cfg_mod.load_mcp_config() == {"fs": {"command": "second"}}


def test_builtin_mcps_folder_surfaces_chrome(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The repo's mcps/chrome/.mcp.json is scanned as a built-in source."""
    monkeypatch.setattr(cfg_mod, "_plugin_config_paths", list)
    merged = cfg_mod.load_mcp_config()
    assert "chrome" in merged
    # chrome is served through our per-agent bridge (services.browser.mcp_wrapper),
    # which dials the shared chrome MCP daemon's socket (no args: the socket + CDP
    # port are derived from settings). The interpreter is launched directly by a
    # relative path — never `uv run`, which would hang a resident wrapper process
    # on every agent; it resolves because built-ins are spawned with cwd=repo root.
    assert merged["chrome"]["command"] == ".venv/bin/python"
    assert "services.browser.mcp_wrapper" in merged["chrome"]["args"]
    # unix_socket alongside display: the wrapper reaches the browser-mcp daemon
    # over an AF_UNIX socket, so a Windows agent must not be offered this entry —
    # it is gated on the same fact that keeps the daemon out of that host's roster.
    assert merged["chrome"]["requires"] == {"display": True, "unix_socket": True}


def test_builtin_command_is_not_uv(unit_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No built-in server may launch through `uv run` — one resident uv wrapper
    process per agent per server is pure overhead at high agent density."""
    monkeypatch.setattr(cfg_mod, "_plugin_config_paths", list)
    for name, spec in cfg_mod.load_mcp_config().items():
        assert spec["command"] != "uv", f"built-in {name} still launches via uv"


def test_machine_config_overrides_builtin(unit_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A machine mcp.json entry wins over a same-named built-in."""
    monkeypatch.setattr(cfg_mod, "_plugin_config_paths", list)
    _write(unit_home / "mcp.json", {"chrome": {"command": "OVERRIDE"}})
    assert cfg_mod.load_mcp_config()["chrome"] == {"command": "OVERRIDE"}


# ─── installed layer ($AVA_HOME/mcps/, registry-gated) ───────────────────


def _install_mcp(home: Path, name: str, spec: dict[str, Any]) -> Path:
    """Register a `type="mcp"` package and write its `$AVA_HOME/mcps/<name>/.mcp.json`."""
    from shared import install_registry as reg

    dest = home / "mcps" / name
    dest.mkdir(parents=True, exist_ok=True)
    (dest / ".mcp.json").write_text(json.dumps({"mcpServers": {name: spec}}), encoding="utf-8")
    reg.register(reg.InstalledPackage(name=name, type="mcp"))
    return dest


def test_installed_mcp_surfaces_when_registered(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cfg_mod, "_builtin_mcp_paths", list)
    monkeypatch.setattr(cfg_mod, "_plugin_config_paths", list)
    _install_mcp(unit_home, "acme", {"command": ".venv/bin/python", "args": ["-m", "acme"]})
    assert "acme" in cfg_mod.load_mcp_config()


def test_installed_mcp_ignored_without_registry_row(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stray dir under the load dir with no `type="mcp"` registry row is not loaded."""
    monkeypatch.setattr(cfg_mod, "_builtin_mcp_paths", list)
    monkeypatch.setattr(cfg_mod, "_plugin_config_paths", list)
    _write(unit_home / "mcps" / "stray" / ".mcp.json", {"stray": {"command": "x"}})
    assert "stray" not in cfg_mod.load_mcp_config()


def test_machine_overrides_installed(unit_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Machine config is the top layer: it wins over a same-named installed server."""
    monkeypatch.setattr(cfg_mod, "_builtin_mcp_paths", list)
    monkeypatch.setattr(cfg_mod, "_plugin_config_paths", list)
    _install_mcp(unit_home, "acme", {"command": "installed"})
    _write(unit_home / "mcp.json", {"acme": {"command": "machine"}})
    assert cfg_mod.load_mcp_config()["acme"] == {"command": "machine"}


def test_installed_overrides_plugin(
    unit_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installed layer sits above plugin: an installed server wins over a same-named plugin one."""
    plugin_json = tmp_path / "plugin" / ".mcp.json"
    _write(plugin_json, {"acme": {"command": "plugin"}})
    monkeypatch.setattr(cfg_mod, "_builtin_mcp_paths", list)
    monkeypatch.setattr(cfg_mod, "_plugin_config_paths", lambda: [plugin_json])
    _install_mcp(unit_home, "acme", {"command": "installed"})
    assert cfg_mod.load_mcp_config()["acme"] == {"command": "installed"}


# ─── installed_mcp_dir (subprocess cwd) ──────────────────────────────────


def test_installed_mcp_dir_returns_load_dir(unit_home: Path) -> None:
    dest = _install_mcp(unit_home, "acme", {"command": ".venv/bin/python"})
    assert cfg_mod.installed_mcp_dir("acme") == dest


def test_installed_mcp_dir_none_for_builtin(unit_home: Path) -> None:
    """A built-in / unknown server has no install dir — cwd stays the daemon's."""
    assert cfg_mod.installed_mcp_dir("chrome") is None


def test_installed_mcp_dir_none_when_machine_shadows(unit_home: Path) -> None:
    """When a machine entry shadows an installed server, the effective server is the
    machine one (spawned from the daemon cwd), so no install dir is returned."""
    _install_mcp(unit_home, "acme", {"command": ".venv/bin/python"})
    _write(unit_home / "mcp.json", {"acme": {"command": "machine"}})
    assert cfg_mod.installed_mcp_dir("acme") is None


# ─── server_cwd (spawn cwd by winning layer) ─────────────────────────────


def test_server_cwd_builtin_is_repo_root(unit_home: Path) -> None:
    """Built-ins are pinned to the repo root so their relative `.venv/bin/python`
    resolves to the repo venv and `-m <pkg>` finds repo top-level packages."""
    from shared.paths import repo_root

    assert cfg_mod.server_cwd("chrome") == repo_root()


def test_server_cwd_installed_is_package_dir(unit_home: Path) -> None:
    dest = _install_mcp(unit_home, "acme", {"command": ".venv/bin/python"})
    assert cfg_mod.server_cwd("acme") == dest


def test_server_cwd_machine_entry_is_none(unit_home: Path) -> None:
    """A machine entry carries the user's own command line — don't reinterpret it."""
    _write(unit_home / "mcp.json", {"solo": {"command": "npx"}})
    assert cfg_mod.server_cwd("solo") is None


def test_server_cwd_machine_overriding_builtin_is_none(unit_home: Path) -> None:
    _write(unit_home / "mcp.json", {"chrome": {"command": "/abs/python"}})
    assert cfg_mod.server_cwd("chrome") is None


def test_server_cwd_plugin_is_none(
    unit_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_json = tmp_path / "plugin" / ".mcp.json"
    _write(plugin_json, {"pserver": {"command": "npx"}})
    monkeypatch.setattr(cfg_mod, "_plugin_config_paths", lambda: [plugin_json])
    assert cfg_mod.server_cwd("pserver") is None


def test_server_cwd_unknown_is_none(unit_home: Path) -> None:
    assert cfg_mod.server_cwd("nosuchserver") is None


# ─── assert_requirements ─────────────────────────────────────────────────


def test_assert_requirements_noop_without_requires() -> None:
    cfg_mod.assert_requirements({"command": "x"})  # no raise


def test_assert_requirements_display_ok_with_display(monkeypatch: pytest.MonkeyPatch) -> None:
    # display_available is imported into cfg_mod from shared.platform_probes;
    # patch the bound name (where assert_requirements calls it).
    monkeypatch.setattr(cfg_mod, "display_available", lambda: True)
    cfg_mod.assert_requirements({"requires": {"display": True}})  # no raise


def test_assert_requirements_display_raises_without_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cfg_mod, "display_available", lambda: False)
    with pytest.raises(cfg_mod.MCPError, match="requires a display"):
        cfg_mod.assert_requirements({"requires": {"display": True}})


def test_assert_requirements_unknown_key_fails_fast() -> None:
    with pytest.raises(cfg_mod.MCPError, match="unknown requires key"):
        cfg_mod.assert_requirements({"requires": {"gpu": True}})


def test_assert_requirements_unix_socket_ok_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg_mod, "unix_sockets_available", lambda: True)
    cfg_mod.assert_requirements({"requires": {"unix_socket": True}})  # no raise


def test_assert_requirements_unix_socket_raises_without_af_unix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Windows agent connecting to `chrome` gets a capability error naming the
    real cause, instead of the wrapper's opaque AttributeError from
    `asyncio.open_unix_connection`."""
    monkeypatch.setattr(cfg_mod, "unix_sockets_available", lambda: False)
    with pytest.raises(cfg_mod.MCPError, match="requires AF_UNIX sockets"):
        cfg_mod.assert_requirements({"requires": {"unix_socket": True}})


def test_assert_requirements_gates_chrome_on_a_windows_shaped_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shipped chrome spec, on a host with a display but no AF_UNIX — i.e.
    Windows, where `display_available()` deliberately returns True. The display
    prong alone would have let it through."""
    monkeypatch.setattr(cfg_mod, "display_available", lambda: True)
    monkeypatch.setattr(cfg_mod, "unix_sockets_available", lambda: False)
    with pytest.raises(cfg_mod.MCPError, match="requires AF_UNIX sockets"):
        cfg_mod.assert_requirements({"requires": {"display": True, "unix_socket": True}})


# ─── server_capability ───────────────────────────────────────────────────


def test_server_capability_ok_when_host_has_both(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg_mod, "display_available", lambda: True)
    monkeypatch.setattr(cfg_mod, "unix_sockets_available", lambda: True)
    assert cfg_mod.server_capability({"requires": {"display": True, "unix_socket": True}}) == (
        True,
        None,
    )


def test_server_capability_reports_missing_unix_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """The UI gate reports the same fact the connect-time check raises on, so a
    Windows host shows chrome as unavailable rather than offering it."""
    monkeypatch.setattr(cfg_mod, "display_available", lambda: True)
    monkeypatch.setattr(cfg_mod, "unix_sockets_available", lambda: False)
    ok, reason = cfg_mod.server_capability({"requires": {"display": True, "unix_socket": True}})
    assert ok is False
    assert reason is not None
    assert "AF_UNIX" in reason


# ─── resolve_command ─────────────────────────────────────────────────────


def test_resolve_command_passthrough_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg_mod, "IS_WINDOWS", False)
    assert cfg_mod.resolve_command(".venv/bin/python") == ".venv/bin/python"


def test_resolve_command_maps_venv_python_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """`.mcp.json` is committed once and read on every platform, so the reader
    maps the repo's POSIX interpreter convention onto the local venv layout —
    the same substitution `shared.session_backend` does for session commands."""
    monkeypatch.setattr(cfg_mod, "IS_WINDOWS", True)
    assert cfg_mod.resolve_command(".venv/bin/python") == ".venv\\Scripts\\python.exe"


def test_resolve_command_leaves_third_party_commands_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plugin / machine entry carries its own argv — reinterpreting it would be
    guessing at someone else's command line."""
    monkeypatch.setattr(cfg_mod, "IS_WINDOWS", True)
    assert cfg_mod.resolve_command("npx") == "npx"
    assert cfg_mod.resolve_command("/usr/local/bin/python3") == "/usr/local/bin/python3"
    assert cfg_mod.resolve_command(".venv/bin/python3.12") == ".venv/bin/python3.12"


# ─── server_url ──────────────────────────────────────────────────────────


def test_server_url_none_for_stdio_server() -> None:
    """A command-based server has no url — transport stays stdio."""
    assert cfg_mod.server_url({"command": "npx", "args": ["-y", "x"]}) is None
    assert cfg_mod.server_url({}) is None


def test_server_url_accepts_https_endpoint() -> None:
    assert (
        cfg_mod.server_url({"url": "https://mcp.example.com/mcp"}) == "https://mcp.example.com/mcp"
    )
    assert cfg_mod.server_url({"url": "http://localhost:8080/mcp"}) == "http://localhost:8080/mcp"


def test_server_url_rejects_non_http_url() -> None:
    """Fail fast on a url that is not http(s) — never hand it to a transport."""
    with pytest.raises(cfg_mod.MCPError, match=r"http\(s\) endpoint"):
        cfg_mod.server_url({"url": "file:///tmp/x"})
    with pytest.raises(cfg_mod.MCPError, match=r"http\(s\) endpoint"):
        cfg_mod.server_url({"url": 42})


def test_server_url_rejects_url_plus_command() -> None:
    """A server is either local stdio or remote HTTP — never both."""
    with pytest.raises(cfg_mod.MCPError, match="both 'url' and 'command'"):
        cfg_mod.server_url({"url": "https://mcp.example.com/mcp", "command": "npx"})


def test_server_url_rejects_non_dict_headers() -> None:
    with pytest.raises(cfg_mod.MCPError, match="'headers' must be a dict"):
        cfg_mod.server_url({"url": "https://mcp.example.com/mcp", "headers": ["Bearer x"]})
    with pytest.raises(cfg_mod.MCPError, match="'headers' must be a dict"):
        cfg_mod.server_url({"url": "https://mcp.example.com/mcp", "headers": {"k": 1}})


def test_server_url_accepts_string_headers() -> None:
    assert (
        cfg_mod.server_url(
            {"url": "https://mcp.example.com/mcp", "headers": {"Authorization": "Bearer x"}}
        )
        == "https://mcp.example.com/mcp"
    )


def test_server_url_accepts_oauth_flag() -> None:
    """`"oauth": true` is a legal remote-server auth mode."""
    assert (
        cfg_mod.server_url({"url": "https://mcp.example.com/mcp", "oauth": True})
        == "https://mcp.example.com/mcp"
    )


def test_server_url_rejects_non_bool_oauth() -> None:
    with pytest.raises(cfg_mod.MCPError, match="'oauth' must be a boolean"):
        cfg_mod.server_url({"url": "https://mcp.example.com/mcp", "oauth": "yes"})


def test_server_url_rejects_oauth_plus_headers() -> None:
    """Static headers and the OAuth flow are two auth modes — pick one."""
    with pytest.raises(cfg_mod.MCPError, match="pick one auth mode"):
        cfg_mod.server_url(
            {"url": "https://mcp.example.com/mcp", "oauth": True, "headers": {"k": "v"}}
        )


def test_builtin_mcps_folder_surfaces_computer(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The repo's mcps/computer/.mcp.json is scanned as a built-in source.

    The computer server is fronted the same way as chrome: shared="computer"
    tells the MCP daemon to dial the per-machine computer-mcp service directly,
    and the wrapper command is the local-fallback path."""
    monkeypatch.setattr(cfg_mod, "_plugin_config_paths", list)
    merged = cfg_mod.load_mcp_config()
    assert "computer" in merged
    assert merged["computer"]["shared"] == "computer"
    assert merged["computer"]["command"] == ".venv/bin/python"
    assert "services.computer.mcp_wrapper" in merged["computer"]["args"]
    assert merged["computer"]["requires"] == {"display": True, "unix_socket": True}
