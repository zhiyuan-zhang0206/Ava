"""`inventory_read_op` and `inventory_write_op` — host-side plugin + MCP enable RPC.

Read returns this host's discovered plugins (each with an enabled flag +
description) and the full merged MCP server map (each with the per-host overlay
state + a read-time capability verdict). Write validates a set of toggles
all-or-nothing, then applies them to the per-machine local files.

Tests run entirely in-process; no DB required. `inventory_read_op` /
`inventory_write_op` only touch local files (plugins_config.json,
mcp_enabled.json) + the merged MCP config. `unit_home` redirects `meiva_home()`
to a per-test tmp dir so the overlay + plugin config files are isolated.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import meiva.mcps.config as mcp_cfg_mod
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import fake_host
from base.packages.plugins import enable_config, mcp_enabled
from ops.inventory import inventory_read_op, inventory_write_op
from ops.rpc_schemas import FieldWriteResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_machine_mcp(unit_home: Path, servers: dict[str, dict]) -> None:
    (unit_home / "mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


@pytest.fixture
def _machine_only_mcp(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch, machine_identity: MachineIdentity
) -> Path:
    """Isolate the MCP merged map to just the machine `mcp.json` under test —
    the public MCP loader supplies that real file's server map. Also pins
    the host role to agent-runner (the ops' precondition): inventory ops run only
    there, and the test env has no MEIVA_MACHINE_SERVE_* set."""

    def machine_config(*, include_disabled: bool = False) -> dict[str, dict[str, object]]:
        assert include_disabled is True
        return mcp_cfg_mod.read_servers(unit_home / "mcp.json")

    monkeypatch.setattr(mcp_cfg_mod, "load_mcp_config", machine_config)
    machine_identity.set(role=frozenset({"agent-runner"}))
    return unit_home


# ---------------------------------------------------------------------------
# inventory_read_op
# ---------------------------------------------------------------------------


def test_inventory_read_op_lists_real_plugins(
    _machine_only_mcp: Path, monkeypatch: pytest.MonkeyPatch, *, machine_identity: MachineIdentity
) -> None:
    """The repo's real built-in plugins (meiva_code, etc.) appear, each with
    an enabled bool + a description. (meiva_compact moved to core, Issue #1284.)"""
    machine_identity.set(name="test-machine")
    result = inventory_read_op(local_machine=machine_identity, host=fake_host())
    assert result.machine == "test-machine"
    plugins = result.plugins
    # Real repo plugins discovered under <repo>/meiva_builtins/plugins/.
    assert "meiva_code" in plugins
    for entry in plugins.values():
        assert isinstance(entry.enabled, bool)
        assert isinstance(entry.description, str)
        # Plugins have no static host gate.
        assert entry.can_enable is None
        assert entry.reason is None


def test_inventory_read_op_mcp_server_default_on(
    _machine_only_mcp: Path, monkeypatch: pytest.MonkeyPatch, *, machine_identity: MachineIdentity
) -> None:
    """A machine-config server with no overlay entry shows enabled=True and a
    one-line description."""
    machine_identity.set(name="test-machine")
    _write_machine_mcp(_machine_only_mcp, {"fs": {"command": "run-fs", "args": ["--root", "/"]}})
    result = inventory_read_op(local_machine=machine_identity, host=fake_host())
    servers = result.mcp_servers
    assert "fs" in servers
    assert servers["fs"].enabled is True
    assert servers["fs"].can_enable is True
    assert servers["fs"].reason is None
    assert servers["fs"].description == "run-fs --root /"


def test_inventory_read_op_overlay_reflected(
    _machine_only_mcp: Path, monkeypatch: pytest.MonkeyPatch, *, machine_identity: MachineIdentity
) -> None:
    """A server the per-host overlay marks disabled reads back enabled=False."""
    machine_identity.set(name="test-machine")
    _write_machine_mcp(_machine_only_mcp, {"X": {"command": "x"}})
    mcp_enabled.set_mcp_enabled("X", enabled=False)
    result = inventory_read_op(local_machine=machine_identity, host=fake_host())
    assert result.mcp_servers["X"].enabled is False


@pytest.mark.parametrize("requires", [[], {"gpu": False}, {"display": "false"}])
def test_inventory_rejects_invalid_requirements_before_read_or_write(
    _machine_only_mcp: Path,
    requires: object,
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
) -> None:
    machine_identity.set(name="test-machine")
    _write_machine_mcp(_machine_only_mcp, {"broken": {"command": "x", "requires": requires}})
    mcp_enabled.set_mcp_enabled("broken", enabled=False)
    before = mcp_enabled.local_config_path().read_bytes()

    with pytest.raises(mcp_cfg_mod.MCPError, match="requires"):
        inventory_read_op(local_machine=machine_identity, host=fake_host())
    with pytest.raises(mcp_cfg_mod.MCPError, match="requires"):
        inventory_write_op(
            plugins={"meiva_code": False},
            mcp_servers={"broken": True},
            local_machine=machine_identity,
            host=fake_host(),
        )
    assert mcp_enabled.local_config_path().read_bytes() == before
    assert not enable_config.local_config_path().exists()


def test_inventory_can_disable_server_with_invalid_requirements(
    _machine_only_mcp: Path, monkeypatch: pytest.MonkeyPatch, *, machine_identity: MachineIdentity
) -> None:
    machine_identity.set(name="test-machine")
    spec = {"command": "x", "requires": {"display": "false"}}
    _write_machine_mcp(_machine_only_mcp, {"broken": spec})

    result = inventory_write_op(
        plugins={}, mcp_servers={"broken": False}, local_machine=machine_identity, host=fake_host()
    )
    assert result.applied is True
    assert result.mcp_results["broken"] == FieldWriteResult(ok=True, reason=None)
    assert mcp_enabled.read_enabled() == {"broken": False}
    assert mcp_cfg_mod.read_servers(_machine_only_mcp / "mcp.json") == {"broken": spec}


# ---------------------------------------------------------------------------
# inventory_write_op
# ---------------------------------------------------------------------------


def test_inventory_write_op_plugin_happy_path(
    _machine_only_mcp: Path, monkeypatch: pytest.MonkeyPatch, *, machine_identity: MachineIdentity
) -> None:
    """Disabling a present plugin -> applied=True, and the local config reflects it."""
    machine_identity.set(name="test-machine")
    result = inventory_write_op(
        plugins={"meiva_code": False},
        mcp_servers={},
        local_machine=machine_identity,
        host=fake_host(),
    )
    assert result.applied is True
    assert result.plugin_results["meiva_code"] == FieldWriteResult(ok=True, reason=None)
    # The per-machine plugins_config.json now records meiva_code as disabled.
    cfg = enable_config.load(set(enable_config.discover_plugins()))
    assert cfg.plugins["meiva_code"].enabled is False


def test_inventory_write_op_atomic_rejects_unknown_plugin(
    _machine_only_mcp: Path, monkeypatch: pytest.MonkeyPatch, *, machine_identity: MachineIdentity
) -> None:
    """A payload mixing a present plugin toggle + an unknown plugin is rejected
    atomically: applied=False, the present plugin's file is UNCHANGED."""
    machine_identity.set(name="test-machine")
    result = inventory_write_op(
        plugins={"meiva_code": False, "does-not-exist": True},
        mcp_servers={},
        local_machine=machine_identity,
        host=fake_host(),
    )
    assert result.applied is False
    assert result.plugin_results["meiva_code"].ok is True
    assert result.plugin_results["does-not-exist"].ok is False
    assert "not installed" in (result.plugin_results["does-not-exist"].reason or "")
    # Atomicity: nothing was written, so the local file does not exist yet.
    assert not enable_config.local_config_path().exists()


def test_inventory_write_op_rejects_unknown_mcp_server(
    _machine_only_mcp: Path, monkeypatch: pytest.MonkeyPatch, *, machine_identity: MachineIdentity
) -> None:
    """An MCP name not in the merged map is rejected with a reason; applied=False."""
    machine_identity.set(name="test-machine")
    result = inventory_write_op(
        plugins={}, mcp_servers={"ghost": True}, local_machine=machine_identity, host=fake_host()
    )
    assert result.applied is False
    assert result.mcp_results["ghost"].ok is False
    assert "known MCP server" in (result.mcp_results["ghost"].reason or "")


def test_inventory_write_op_mcp_capability_rejected(
    _machine_only_mcp: Path, monkeypatch: pytest.MonkeyPatch, *, machine_identity: MachineIdentity
) -> None:
    """A server requiring a display, on a host with none, can't be enabled
    (applied=False) but can always be disabled."""
    machine_identity.set(name="test-machine")
    _write_machine_mcp(_machine_only_mcp, {"that": {"command": "x", "requires": {"display": True}}})

    # Patch the symbol where server_capability looks it up.
    def fake_display_available(*, host: Host) -> bool:
        return False

    monkeypatch.setattr(mcp_cfg_mod, "display_available", fake_display_available)

    enable = inventory_write_op(
        plugins={}, mcp_servers={"that": True}, local_machine=machine_identity, host=fake_host()
    )
    assert enable.applied is False
    assert enable.mcp_results["that"].ok is False
    assert enable.mcp_results["that"].reason is not None
    # Nothing written on rejection.
    assert mcp_enabled.read_enabled() == {}

    disable = inventory_write_op(
        plugins={}, mcp_servers={"that": False}, local_machine=machine_identity, host=fake_host()
    )
    assert disable.applied is True
    assert disable.mcp_results["that"] == FieldWriteResult(ok=True, reason=None)
    assert mcp_enabled.read_enabled() == {"that": False}


def test_inventory_write_op_empty_payload_trivially_applied(
    _machine_only_mcp: Path, monkeypatch: pytest.MonkeyPatch, *, machine_identity: MachineIdentity
) -> None:
    """Empty payload -> applied=True, nothing written."""
    machine_identity.set(name="test-machine")
    result = inventory_write_op(
        plugins={}, mcp_servers={}, local_machine=machine_identity, host=fake_host()
    )
    assert result.applied is True
    assert result.plugin_results == {}
    assert result.mcp_results == {}
    assert not enable_config.local_config_path().exists()
    assert mcp_enabled.read_enabled() == {}


def test_inventory_write_op_applies_both_plugin_and_mcp(
    _machine_only_mcp: Path, monkeypatch: pytest.MonkeyPatch, *, machine_identity: MachineIdentity
) -> None:
    """A mixed payload (present plugin + known MCP, both passing) writes both."""
    machine_identity.set(name="test-machine")
    _write_machine_mcp(_machine_only_mcp, {"fs": {"command": "x"}})
    result = inventory_write_op(
        plugins={"meiva_code": False},
        mcp_servers={"fs": False},
        local_machine=machine_identity,
        host=fake_host(),
    )
    assert result.applied is True
    cfg = enable_config.load(set(enable_config.discover_plugins()))
    assert cfg.plugins["meiva_code"].enabled is False
    assert mcp_enabled.read_enabled() == {"fs": False}


# ---------------------------------------------------------------------------
# agent-runner-only precondition
# ---------------------------------------------------------------------------


def test_inventory_ops_reject_gateway(
    monkeypatch: pytest.MonkeyPatch, *, machine_identity: MachineIdentity
) -> None:
    """On a gateway both ops raise before touching any local file — the
    invariant that inventory is agent-runner-only, made loud rather than silently
    surfacing the gateway checkout's built-in plugins/MCP."""
    machine_identity.set(role=frozenset({"gateway"}))
    with pytest.raises(RuntimeError, match="only on an agent-runner"):
        inventory_read_op(local_machine=machine_identity, host=fake_host())
    with pytest.raises(RuntimeError, match="only on an agent-runner"):
        inventory_write_op(
            plugins={}, mcp_servers={}, local_machine=machine_identity, host=fake_host()
        )
