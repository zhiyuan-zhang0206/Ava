"""Bounded wait for the reachable (private-network) bind address before starting pg.

On reboot brew/launchd can start `meiva` before the private network has assigned its address;
binding pg to a not-yet-present address fails and takes the whole autostart
down. `wait_for_reachable_bind` blocks (bounded) until the address is assigned, and
fails fast on timeout. A loopback-only single box never waits.
"""

import dataclasses
from pathlib import Path

import pytest

from base.cluster.machine import MachineIdentity
from cli.commands.data_plane import cluster_instance as _ci
from cli.commands.data_plane.config import DataPlaneConfig
from cli.commands.data_plane.tests.instance.bind_support import (
    config_boot_environment as config_boot_environment,
)
from cli.commands.data_plane.tests.instance.bind_support import native_ownership as native_ownership
from cli.database.tests.command_inputs import command_machine as command_machine
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline


def test_macos_redis_binaries_use_versioned_formula(
    monkeypatch: pytest.MonkeyPatch, data_plane_config: DataPlaneConfig
) -> None:
    formulae: list[str] = []

    def fake_brew_prefix(formula: str) -> Path:
        formulae.append(formula)
        return Path("/opt/homebrew/opt") / formula

    monkeypatch.setattr(_ci, "is_macos", lambda: True)
    monkeypatch.setattr(_ci, "brew_prefix", fake_brew_prefix)
    # Exercise formula discovery independently of this machine's explicit pin.
    data_plane_config = dataclasses.replace(data_plane_config, redis_bin_dir="")

    assert _ci.redis_server_bin(data_plane_config) == "/opt/homebrew/opt/redis@8.2/bin/redis-server"
    assert _ci.redis_cli_bin(data_plane_config) == "/opt/homebrew/opt/redis@8.2/bin/redis-cli"
    assert formulae == ["redis@8.2", "redis@8.2"]


def test_addr_assigned_loopback_is_true() -> None:
    assert _ci.addr_assigned("127.0.0.1") is True


def test_addr_assigned_unassigned_ip_is_false() -> None:
    """192.0.2.0/24 (RFC 5737 TEST-NET-1) is never assigned to a real interface, so
    binding it raises EADDRNOTAVAIL — the exact 'address not up yet' signal."""
    assert _ci.addr_assigned("192.0.2.1") is False


def test_wait_returns_immediately_for_loopback_only_host(
    monkeypatch: pytest.MonkeyPatch, *, command_machine: MachineIdentity
) -> None:
    """A single box reachable only at localhost never waits — loopback is always up."""
    command_machine.set(host="localhost")
    monkeypatch.setattr(
        _ci,
        "addr_assigned",
        lambda _a: pytest.fail("must not probe on a loopback-only host"),  # pyright: ignore[reportUnknownArgumentType]
    )
    assert _ci.wait_for_reachable_bind(machine=command_machine) is True


def test_wait_returns_true_once_address_appears(
    monkeypatch: pytest.MonkeyPatch, *, command_machine: MachineIdentity
) -> None:
    """The reachable address is absent on the first probe, present on the next — the
    private-network-coming-up-late case — so the wait resolves True after retrying."""
    command_machine.set(host="10.0.0.5")
    monkeypatch.setattr(_ci.time, "sleep", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]
    seen: list[int] = []

    def _appears(_addr: str) -> bool:
        seen.append(1)
        return len(seen) >= 2

    monkeypatch.setattr(_ci, "addr_assigned", _appears)
    assert _ci.wait_for_reachable_bind(machine=command_machine) is True
    assert len(seen) == 2


def test_wait_returns_false_on_timeout(
    monkeypatch: pytest.MonkeyPatch, *, command_machine: MachineIdentity
) -> None:
    """The address never appears within the bound → fail fast (caller aborts start)."""
    command_machine.set(host="10.0.0.5")
    monkeypatch.setattr(_ci, "BIND_WAIT_TIMEOUT_S", 0.0)
    monkeypatch.setattr(_ci.time, "sleep", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_ci, "addr_assigned", lambda _a: False)  # pyright: ignore[reportUnknownArgumentType]
    assert _ci.wait_for_reachable_bind(machine=command_machine) is False
