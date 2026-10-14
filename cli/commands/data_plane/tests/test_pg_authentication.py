"""Explicit-machine authentication inputs preserve peer and SCRAM boundaries."""

import dataclasses

import pytest

from cli.commands.data_plane import cluster_instance as _ci
from cli.commands.data_plane.config import DataPlaneConfig
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime


def _admin_peer_line() -> str:
    import getpass

    return f"local all {getpass.getuser()} peer"


_ALWAYS_AUTH_LOOPBACK = [
    # The collector's password-less monitoring role: peer, mapped from the OS user.
    "local all ava_monitor peer map=ava_monitor",
    "local all all scram-sha-256",
    "host all all 127.0.0.1/32 scram-sha-256",
    "host all all ::1/128 scram-sha-256",
]


def test_pg_hba_body_of_a_single_box_is_loopback_only(
    monkeypatch: pytest.MonkeyPatch,
    *,
    command_runtime: CommandRuntime,
    data_plane_config: DataPlaneConfig,
) -> None:
    """A box that declares no reachable address (`AVA_MACHINE_HOST` unset, the
    `localhost` default) authenticates everything and opens no LAN line: the OS
    user only by peer on the owner-only socket, every other role SCRAM; no trust
    line anywhere. The cluster has a secret all the same — it does not widen reach."""
    data_plane_config = dataclasses.replace(
        data_plane_config,
        cluster_secret="s3cret",  # noqa: S106
        trusted_cidrs="",
    )
    command_runtime.machine.set(host="localhost")
    body = _ci.pg_hba_body(data_plane_config, machine=command_runtime.machine)
    assert "trust" not in body
    assert body.splitlines() == [_admin_peer_line(), *_ALWAYS_AUTH_LOOPBACK]


def test_pg_ident_maps_only_the_os_user_to_the_monitoring_role() -> None:
    """The ident map the monitor's peer line names admits exactly this OS user
    as `ava_monitor`; it is the file's only mapping."""
    import getpass

    assert _ci.pg_ident_body() == f"ava_monitor {getpass.getuser()} ava_monitor\n"


def test_pg_hba_body_adds_the_declared_reach_as_scram_lines(
    monkeypatch: pytest.MonkeyPatch,
    *,
    command_runtime: CommandRuntime,
    data_plane_config: DataPlaneConfig,
) -> None:
    """A declared reachable host and the trusted CIDRs join as SCRAM lines."""
    data_plane_config = dataclasses.replace(data_plane_config, trusted_cidrs="10.0.0.0/8")
    command_runtime.machine.set(host="10.0.0.5")
    body = _ci.pg_hba_body(data_plane_config, machine=command_runtime.machine)
    assert body.splitlines() == [
        _admin_peer_line(),
        *_ALWAYS_AUTH_LOOPBACK,
        "host all all 10.0.0.5/32 scram-sha-256",
        "host all all 10.0.0.0/8 scram-sha-256",
    ]
