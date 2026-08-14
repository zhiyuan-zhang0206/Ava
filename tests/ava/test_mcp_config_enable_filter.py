"""`ava._mcp_config.load_mcp_config` — per-host enable overlay filtering.

The loader drops servers the per-host overlay (`~/.ava/mcp_enabled.json`) marks
disabled by default; `include_disabled=True` returns the full merged map. A
server absent from the overlay stays (default-on).

`unit_home` points `ava_home()` (so both `mcp.json` and `mcp_enabled.json`) at a
tmp dir. The builtin/plugin sources are monkeypatched empty so only the machine
config under test contributes.
"""

import json
from pathlib import Path

import pytest

import ava._mcp_config as cfg_mod
from shared.mcp_enabled import McpEnabledConfig, McpServerEntry, write_local


def _write_machine(unit_home: Path, servers: dict[str, dict[str, str]]) -> None:
    (unit_home / "mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


@pytest.fixture
def _two_servers(unit_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A machine mcp.json defining two servers, isolated from builtin/plugin sources."""
    monkeypatch.setattr(cfg_mod, "_builtin_mcp_paths", list)
    monkeypatch.setattr(cfg_mod, "_plugin_config_paths", list)
    _write_machine(unit_home, {"fs": {"command": "a"}, "github": {"command": "b"}})
    return unit_home


def test_no_overlay_keeps_all(_two_servers: Path) -> None:
    """No overlay file -> every defined server stays (default-on)."""
    assert set(cfg_mod.load_mcp_config()) == {"fs", "github"}


def test_default_drops_disabled(_two_servers: Path) -> None:
    """Default load() drops a server the overlay marks disabled; the other stays."""
    write_local(McpEnabledConfig(mcp_servers={"fs": McpServerEntry(enabled=False)}))
    assert set(cfg_mod.load_mcp_config()) == {"github"}


def test_include_disabled_keeps_both(_two_servers: Path) -> None:
    """include_disabled=True returns the full merged map regardless of the overlay."""
    write_local(McpEnabledConfig(mcp_servers={"fs": McpServerEntry(enabled=False)}))
    assert set(cfg_mod.load_mcp_config(include_disabled=True)) == {"fs", "github"}


def test_overlay_enabled_true_keeps_server(_two_servers: Path) -> None:
    """An explicit enabled=True entry leaves the server in place."""
    write_local(McpEnabledConfig(mcp_servers={"fs": McpServerEntry(enabled=True)}))
    assert set(cfg_mod.load_mcp_config()) == {"fs", "github"}
