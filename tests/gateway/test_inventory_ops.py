"""`inventory_read_op` and `inventory_write_op` — host-side plugin + MCP enable RPC.

Read returns this host's discovered plugins (each with an enabled flag +
description) and the full merged MCP server map (each with the per-host overlay
state + a read-time capability verdict). Write validates a set of toggles
all-or-nothing, then applies them to the per-machine local files.

Tests run entirely in-process; no DB required. `inventory_read_op` /
`inventory_write_op` only touch local files (plugins_config.json,
mcp_enabled.json) + the merged MCP config. `unit_home` redirects `ava_home()`
to a per-test tmp dir so the overlay + plugin config files are isolated.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import ava._mcp_config as mcp_cfg_mod
from ops import ops_inventory as ops
from ops.ops_inventory import inventory_read_op, inventory_write_op
from ops.rpc_schemas import FieldWriteResult
from shared import mcp_enabled, plugins_config

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_machine_mcp(unit_home: Path, servers: dict[str, dict]) -> None:
    (unit_home / "mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


@pytest.fixture
def _machine_only_mcp(unit_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the MCP merged map to just the machine `mcp.json` under test —
    builtin `mcps/` and plugin `.mcp.json` sources are stubbed empty. Also pins
    the host role to agent-runner (the ops' precondition): inventory ops run only
    there, and the test env has no AVA_MACHINE_SERVE_* set."""
    monkeypatch.setattr(mcp_cfg_mod, "_builtin_mcp_paths", list)
    monkeypatch.setattr(mcp_cfg_mod, "_plugin_config_paths", list)
    # is_agent_runner() (the ops precondition) reads shared.machine.machine_role;
    # ops.machine_role is only used to format the rejection message. Pin both.
    monkeypatch.setattr("shared.machine.machine_role", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(ops, "machine_role", lambda: frozenset({"agent-runner"}))
    return unit_home


# ---------------------------------------------------------------------------
# inventory_read_op
# ---------------------------------------------------------------------------


def test_inventory_read_op_lists_real_plugins(
    _machine_only_mcp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The repo's real built-in plugins (ava_code, etc.) appear, each with
    an enabled bool + a description. (ava_compact moved to core, Issue #1284.)"""
    monkeypatch.setattr(ops, "machine_name", lambda: "test-machine")
    result = inventory_read_op()
    assert result.machine == "test-machine"
    plugins = result.plugins
    # Real repo plugins discovered under <repo>/ava_builtins/plugins/.
    assert "ava_code" in plugins
    for entry in plugins.values():
        assert isinstance(entry.enabled, bool)
        assert isinstance(entry.description, str)
        # Plugins have no static host gate.
        assert entry.can_enable is None
        assert entry.reason is None


def test_inventory_read_op_mcp_server_default_on(
    _machine_only_mcp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A machine-config server with no overlay entry shows enabled=True and a
    one-line description."""
    monkeypatch.setattr(ops, "machine_name", lambda: "test-machine")
    _write_machine_mcp(_machine_only_mcp, {"fs": {"command": "run-fs", "args": ["--root", "/"]}})
    result = inventory_read_op()
    servers = result.mcp_servers
    assert "fs" in servers
    assert servers["fs"].enabled is True
    assert servers["fs"].can_enable is True
    assert servers["fs"].reason is None
    assert servers["fs"].description == "run-fs --root /"


def test_inventory_read_op_overlay_reflected(
    _machine_only_mcp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server the per-host overlay marks disabled reads back enabled=False."""
    monkeypatch.setattr(ops, "machine_name", lambda: "test-machine")
    _write_machine_mcp(_machine_only_mcp, {"X": {"command": "x"}})
    mcp_enabled.set_mcp_enabled("X", enabled=False)
    result = inventory_read_op()
    assert result.mcp_servers["X"].enabled is False


# ---------------------------------------------------------------------------
# inventory_write_op
# ---------------------------------------------------------------------------


def test_inventory_write_op_plugin_happy_path(
    _machine_only_mcp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disabling a present plugin -> applied=True, and the local config reflects it."""
    monkeypatch.setattr(ops, "machine_name", lambda: "test-machine")
    result = inventory_write_op(plugins={"ava_code": False}, mcp_servers={})
    assert result.applied is True
    assert result.plugin_results["ava_code"] == FieldWriteResult(ok=True, reason=None)
    # The per-machine plugins_config.json now records ava_code as disabled.
    cfg = plugins_config.load(set(plugins_config._discover_plugins()))
    assert cfg.plugins["ava_code"].enabled is False


def test_inventory_write_op_atomic_rejects_unknown_plugin(
    _machine_only_mcp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A payload mixing a present plugin toggle + an unknown plugin is rejected
    atomically: applied=False, the present plugin's file is UNCHANGED."""
    monkeypatch.setattr(ops, "machine_name", lambda: "test-machine")
    result = inventory_write_op(plugins={"ava_code": False, "does-not-exist": True}, mcp_servers={})
    assert result.applied is False
    assert result.plugin_results["ava_code"].ok is True
    assert result.plugin_results["does-not-exist"].ok is False
    assert "not installed" in (result.plugin_results["does-not-exist"].reason or "")
    # Atomicity: nothing was written, so the local file does not exist yet.
    assert not plugins_config.local_config_path().exists()


def test_inventory_write_op_rejects_unknown_mcp_server(
    _machine_only_mcp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An MCP name not in the merged map is rejected with a reason; applied=False."""
    monkeypatch.setattr(ops, "machine_name", lambda: "test-machine")
    result = inventory_write_op(plugins={}, mcp_servers={"ghost": True})
    assert result.applied is False
    assert result.mcp_results["ghost"].ok is False
    assert "known MCP server" in (result.mcp_results["ghost"].reason or "")


def test_inventory_write_op_mcp_capability_rejected(
    _machine_only_mcp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server requiring a display, on a host with none, can't be enabled
    (applied=False) but can always be disabled."""
    monkeypatch.setattr(ops, "machine_name", lambda: "test-machine")
    _write_machine_mcp(_machine_only_mcp, {"that": {"command": "x", "requires": {"display": True}}})
    # Patch the symbol where server_capability looks it up.
    monkeypatch.setattr(mcp_cfg_mod, "display_available", lambda: False)

    enable = inventory_write_op(plugins={}, mcp_servers={"that": True})
    assert enable.applied is False
    assert enable.mcp_results["that"].ok is False
    assert enable.mcp_results["that"].reason is not None
    # Nothing written on rejection.
    assert mcp_enabled.read_enabled() == {}

    disable = inventory_write_op(plugins={}, mcp_servers={"that": False})
    assert disable.applied is True
    assert disable.mcp_results["that"] == FieldWriteResult(ok=True, reason=None)
    assert mcp_enabled.read_enabled() == {"that": False}


def test_inventory_write_op_empty_payload_trivially_applied(
    _machine_only_mcp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty payload -> applied=True, nothing written."""
    monkeypatch.setattr(ops, "machine_name", lambda: "test-machine")
    result = inventory_write_op(plugins={}, mcp_servers={})
    assert result.applied is True
    assert result.plugin_results == {}
    assert result.mcp_results == {}
    assert not plugins_config.local_config_path().exists()
    assert mcp_enabled.read_enabled() == {}


def test_inventory_write_op_applies_both_plugin_and_mcp(
    _machine_only_mcp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mixed payload (present plugin + known MCP, both passing) writes both."""
    monkeypatch.setattr(ops, "machine_name", lambda: "test-machine")
    _write_machine_mcp(_machine_only_mcp, {"fs": {"command": "x"}})
    result = inventory_write_op(plugins={"ava_code": False}, mcp_servers={"fs": False})
    assert result.applied is True
    cfg = plugins_config.load(set(plugins_config._discover_plugins()))
    assert cfg.plugins["ava_code"].enabled is False
    assert mcp_enabled.read_enabled() == {"fs": False}


# ---------------------------------------------------------------------------
# agent-runner-only precondition
# ---------------------------------------------------------------------------


def test_inventory_ops_reject_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a gateway both ops raise before touching any local file — the
    invariant that inventory is agent-runner-only, made loud rather than silently
    surfacing the gateway checkout's built-in plugins/MCP."""
    monkeypatch.setattr("shared.machine.machine_role", lambda: frozenset({"gateway"}))
    monkeypatch.setattr(ops, "machine_role", lambda: frozenset({"gateway"}))
    with pytest.raises(RuntimeError, match="only on an agent-runner"):
        inventory_read_op()
    with pytest.raises(RuntimeError, match="only on an agent-runner"):
        inventory_write_op(plugins={}, mcp_servers={})
