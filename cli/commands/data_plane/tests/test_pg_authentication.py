"""Explicit-machine authentication inputs preserve peer and SCRAM boundaries."""

import pytest

from base.config import settings
from cli.commands.data_plane import cluster_instance as _ci
from cli.database.runtime import CommandRuntime
from cli.tests.bootstrap.command_inputs import command_runtime as command_runtime


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


def test_pg_hba_body_authenticates_without_secret(
    monkeypatch: pytest.MonkeyPatch, *, command_runtime: CommandRuntime
) -> None:
    """No secret -> the data plane still authenticates: the OS user only by peer
    on the owner-only socket, every other role SCRAM; no trust line anywhere and
    no reachable/cidr lines (the bind stays loopback-only)."""
    monkeypatch.setattr(settings.data_plane, "trusted_cidrs", "10.0.0.0/8")
    command_runtime.machine.set(host="10.0.0.5")
    body = _ci._pg_hba_body("", machine=command_runtime.machine)
    assert "trust" not in body
    assert body.splitlines() == [_admin_peer_line(), *_ALWAYS_AUTH_LOOPBACK]


def test_pg_ident_maps_only_the_os_user_to_the_monitoring_role() -> None:
    """The ident map the monitor's peer line names admits exactly this OS user
    as `ava_monitor`; it is the file's only mapping."""
    import getpass

    assert _ci._pg_ident_body() == f"ava_monitor {getpass.getuser()} ava_monitor\n"


def test_pg_hba_body_scram_with_secret(
    monkeypatch: pytest.MonkeyPatch, *, command_runtime: CommandRuntime
) -> None:
    """With a secret, the reachable host and trusted CIDRs join as SCRAM lines."""
    monkeypatch.setattr(settings.data_plane, "trusted_cidrs", "10.0.0.0/8")
    command_runtime.machine.set(host="10.0.0.5")
    body = _ci._pg_hba_body("s3cret", machine=command_runtime.machine)
    assert body.splitlines() == [
        _admin_peer_line(),
        *_ALWAYS_AUTH_LOOPBACK,
        "host all all 10.0.0.5/32 scram-sha-256",
        "host all all 10.0.0.0/8 scram-sha-256",
    ]


def test_pg_hba_body_follows_passed_secret_not_ambient_settings(
    monkeypatch: pytest.MonkeyPatch,
    *,
    command_runtime: CommandRuntime,
) -> None:
    """The hba is written from the passed cluster secret, never from ambient
    settings — otherwise a no-secret cluster born from a prod-sourced shell
    gains LAN-reachable host lines keyed to a FOREIGN cluster's posture."""
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "foreign-sibling-secret")
    monkeypatch.setattr(settings.data_plane, "trusted_cidrs", "10.0.0.0/8")
    command_runtime.machine.set(host="10.0.0.5")
    body = _ci._pg_hba_body("", machine=command_runtime.machine)
    assert "10.0.0" not in body
    assert body.splitlines() == [_admin_peer_line(), *_ALWAYS_AUTH_LOOPBACK]
