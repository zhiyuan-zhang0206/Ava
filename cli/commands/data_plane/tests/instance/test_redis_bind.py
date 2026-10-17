"""The cluster's own Redis binds loopback on macOS and the declared reach elsewhere, always authenticated."""

import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from base.cluster import ownership, port_preflight
from base.cluster.machine import MachineIdentity
from base.telemetry import EventPipeline
from cli.commands.data_plane import bringup as _dp
from cli.commands.data_plane import cluster_instance as _ci
from cli.commands.data_plane import pgbouncer
from cli.commands.data_plane.config import DataPlaneConfig
from cli.commands.data_plane.tests.data_plane_records import with_redis_admin
from cli.commands.data_plane.tests.instance.bind_support import (
    config_boot_environment as config_boot_environment,
)
from cli.commands.data_plane.tests.instance.bind_support import native_ownership as native_ownership
from cli.database.tests.command_inputs import command_machine as command_machine
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline

# ─── task #1469: macOS retains its loopback Redis workaround ──────────────────


def _wire_redis_start(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[list[str]]:
    """Capture a successful fresh Redis start without touching the cluster home."""
    monkeypatch.setattr(_ci, "is_macos", lambda: True)

    def fake_wait_for_reachable_bind(*, machine: MachineIdentity) -> bool:
        assert isinstance(machine, MachineIdentity)
        return pytest.fail("redis must never wait for the reachable bind")

    monkeypatch.setattr(
        _ci,
        "wait_for_reachable_bind",
        fake_wait_for_reachable_bind,
    )
    monkeypatch.setattr(_ci, "redis_server_bin", lambda _config: "redis-server")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(ownership, "redis_data_dir", lambda: tmp_path)
    redis_answers = iter([False, True])  # not running before start, up after
    monkeypatch.setattr(
        _ci,
        "redis_running",
        lambda *_a, **_kw: next(redis_answers),  # pyright: ignore[reportUnknownArgumentType]
    )
    started: list[list[str]] = []

    def _run(cmd: list[str], **_: object) -> object:
        started.append(cmd)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(_ci.subprocess, "run", _run)
    monkeypatch.setattr(_ci, "ensure_redis_acl", lambda *_a, **_kw: 0)  # pyright: ignore[reportUnknownArgumentType]
    return started


def _redis_bind_arg(command: list[str]) -> list[str]:
    """Return the complete `--bind` option through the next Redis option."""
    start = command.index("--bind")
    end = command.index("--protected-mode")
    return command[start:end]


def test_start_redis_on_macos_binds_loopback_only_and_authenticates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    command_machine: MachineIdentity,
    data_plane_config: DataPlaneConfig,
) -> None:
    """A macOS Redis start uses exactly the loopback bind, and still
    authenticates: reach never decides whether Redis has a password."""
    command_machine.set(host="10.0.0.5")
    started = _wire_redis_start(monkeypatch, tmp_path)

    assert (
        _ci.start_redis(
            with_redis_admin(data_plane_config, "redis-admin"),
            6380,
            "redis-runtime",
            "meiva",
            machine=command_machine,
        )
        == 0
    )
    assert _redis_bind_arg(started[0]) == ["--bind", "127.0.0.1"]
    assert "--save" not in started[0]  # persistence rides the rendered conf, not argv
    conf = (tmp_path / "redis.conf").read_text()
    assert conf.startswith("save 900 1")
    assert conf.endswith('requirepass "redis-admin"\n')


def test_the_redis_daemon_inherits_no_meiva_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    command_machine: MachineIdentity,
    data_plane_config: DataPlaneConfig,
) -> None:
    """The boot pass may have delivered the gateway login, the generation and
    the API token to this process; the daemon it spawns keeps none of them."""
    delivered = {
        "MEIVA_DB_URL": "postgresql://meiva_g0_gateway:gen-password@127.0.0.1:6433/meiva",
        "MEIVA_DB_GENERATION": "0",
        "MEIVA_API_TOKEN": "gateway-api-token-" + "t" * 32,
        "MEIVA_CLUSTER_SECRET": "human-" + "h" * 40,
        "MEIVA_REDIS_ADMIN_PASSWORD": "redis-admin",
    }
    for key, value in {**delivered, "PATH": "/usr/bin:/bin", "TZ": "Asia/Shanghai"}.items():
        monkeypatch.setenv(key, value)
    _wire_redis_start(monkeypatch, tmp_path)
    envs: list[object] = []

    def _run(cmd: list[str], **kwargs: object) -> object:
        envs.append(kwargs.get("env"))
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(_ci.subprocess, "run", _run)
    assert (
        _ci.start_redis(
            with_redis_admin(data_plane_config, "redis-admin"),
            6380,
            "redis-runtime",
            "meiva",
            machine=command_machine,
        )
        == 0
    )
    [captured] = envs
    assert isinstance(captured, dict)
    env = cast("dict[str, str]", captured)
    assert env["PATH"] == "/usr/bin:/bin" and env["TZ"] == "Asia/Shanghai"
    assert not [key for key in env if key.startswith("MEIVA_")]
    assert not set(delivered.values()) & set(env.values())


@pytest.mark.parametrize(
    ("admin", "runtime"), [("", ""), ("", "redis-runtime"), ("redis-admin", "")]
)
def test_start_redis_refuses_missing_credentials_before_effects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    admin: str,
    runtime: str,
    *,
    command_machine: MachineIdentity,
    data_plane_config: DataPlaneConfig,
) -> None:
    """Redis never starts, or is re-affirmed, without both generated passwords;
    there is no password-less posture to fall back to."""
    started = _wire_redis_start(monkeypatch, tmp_path)
    monkeypatch.setattr(
        _ci,
        "ensure_redis_acl",
        lambda *_a, **_kw: pytest.fail("ACL effect without credentials"),  # pyright: ignore[reportUnknownArgumentType]
    )
    with pytest.raises(ValueError, match="password"):
        _ci.start_redis(
            with_redis_admin(data_plane_config, admin),
            6380,
            runtime,
            "meiva",
            machine=command_machine,
        )
    assert started == []
    assert not (tmp_path / "redis.conf").exists()


def test_redis_admin_helpers_refuse_an_empty_password(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="admin password"):
        _ci.write_redis_conf(tmp_path, "")
    with pytest.raises(ValueError, match="admin password"):
        _ci.redis_cli_env("")
    assert not (tmp_path / "redis.conf").exists()


def test_macos_start_redis_does_not_use_shared_pg_bind_addrs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    command_machine: MachineIdentity,
    data_plane_config: DataPlaneConfig,
) -> None:
    """The shared helper may remain dual-bind for pg without widening Redis."""

    def fake_bind_addrs(*, machine: MachineIdentity) -> list[str]:
        assert isinstance(machine, MachineIdentity)
        return pytest.fail("redis must not use the shared pg bind helper")

    monkeypatch.setattr(
        port_preflight,
        "bind_addrs",
        fake_bind_addrs,  # pyright: ignore[reportUnknownArgumentType]
    )
    started = _wire_redis_start(monkeypatch, tmp_path)

    assert (
        _ci.start_redis(
            with_redis_admin(data_plane_config, "redis-admin"),
            6380,
            "redis-runtime",
            "meiva",
            machine=command_machine,
        )
        == 0
    )
    assert _redis_bind_arg(started[0]) == ["--bind", "127.0.0.1"]


@pytest.mark.parametrize(
    ("host", "expected", "waits"),
    [
        ("localhost", ["--bind", "127.0.0.1"], []),
        ("10.0.0.5", ["--bind", "127.0.0.1", "10.0.0.5"], [True]),
    ],
)
def test_linux_redis_bind_follows_the_declared_reachable_host_not_the_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    host: str,
    expected: list[str],
    waits: list[bool],
    *,
    command_machine: MachineIdentity,
    data_plane_config: DataPlaneConfig,
) -> None:
    started = _wire_redis_start(monkeypatch, tmp_path)
    monkeypatch.setattr(_ci, "is_macos", lambda: False)
    command_machine.set(host=host)
    data_plane_config = dataclasses.replace(data_plane_config, cluster_secret="cluster-bearer")  # noqa: S106
    seen: list[bool] = []

    def address_ready(*, machine: MachineIdentity) -> bool:
        assert machine is command_machine
        if host == "localhost":
            return True
        seen.append(True)
        return True

    monkeypatch.setattr(_ci, "wait_for_reachable_bind", address_ready)
    assert (
        _ci.start_redis(
            with_redis_admin(data_plane_config, "admin"),
            6380,
            "runtime",
            "meiva",
            machine=command_machine,
        )
        == 0
    )
    assert _redis_bind_arg(started[0]) == expected
    assert seen == waits


def test_linux_redis_refuses_start_when_reachable_address_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    command_machine: MachineIdentity,
    data_plane_config: DataPlaneConfig,
) -> None:
    started = _wire_redis_start(monkeypatch, tmp_path)
    monkeypatch.setattr(_ci, "is_macos", lambda: False)

    def fake_wait_for_reachable_bind(*, machine: MachineIdentity) -> bool:
        assert isinstance(machine, MachineIdentity)
        return False

    monkeypatch.setattr(_ci, "wait_for_reachable_bind", fake_wait_for_reachable_bind)
    assert (
        _ci.start_redis(
            with_redis_admin(data_plane_config, "admin"),
            6380,
            "runtime",
            "meiva",
            machine=command_machine,
        )
        == 1
    )
    assert started == []
    assert not (tmp_path / "redis.conf").exists()


def test_running_redis_persists_the_authenticated_password_to_its_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    command_machine: MachineIdentity,
    data_plane_config: DataPlaneConfig,
) -> None:
    """A journal retry repairs config after an old-password false-down probe."""
    monkeypatch.setattr(ownership, "redis_data_dir", lambda: tmp_path)
    monkeypatch.setattr(_ci, "redis_running", lambda *_args: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_ci, "ensure_redis_acl", lambda *_args: 0)  # pyright: ignore[reportUnknownArgumentType]
    (tmp_path / "redis.conf").write_text('requirepass "stale-old-password"\n')

    assert (
        _ci.start_redis(
            with_redis_admin(data_plane_config, "journal-password"),
            6380,
            "runtime",
            "meiva",
            machine=command_machine,
        )
        == 0
    )
    assert (tmp_path / "redis.conf").read_text() == (
        'save 900 1\nsave 300 10\nsave 60 10000\nrequirepass "journal-password"\n'
    )


def test_redis_config_keeps_previous_complete_value_when_replace_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from base.host import private_storage

    conf = tmp_path / "redis.conf"
    conf.write_text('requirepass "old-complete"\n')

    def _fail_replace(_source: object, _destination: object) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(private_storage.os, "replace", _fail_replace)

    with pytest.raises(OSError, match="injected replace failure"):
        _ci.write_redis_conf(tmp_path, "new-complete")

    assert conf.read_text() == 'requirepass "old-complete"\n'


def test_redis_conf_always_renders_rdb_save_schedule(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    command_machine: MachineIdentity,
    data_plane_config: DataPlaneConfig,
) -> None:
    """Redis persists and authenticates: the RDB save
    schedule and requirepass survive every conf render, or a restart silently
    loses persistence (task #2027) or comes back without a password."""
    monkeypatch.setattr(ownership, "redis_data_dir", lambda: tmp_path)
    monkeypatch.setattr(_ci, "redis_running", lambda *_args: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_ci, "ensure_redis_acl", lambda *_args: 0)  # pyright: ignore[reportUnknownArgumentType]

    assert (
        _ci.start_redis(
            with_redis_admin(data_plane_config, "admin"),
            6380,
            "runtime",
            "meiva",
            machine=command_machine,
        )
        == 0
    )
    assert (tmp_path / "redis.conf").read_text() == (
        'save 900 1\nsave 300 10\nsave 60 10000\nrequirepass "admin"\n'
    )


def test_force_stop_shuts_redis_down_as_admin_not_runtime_user(
    monkeypatch: pytest.MonkeyPatch,
    operator_pipeline: Callable[[], EventPipeline],
    data_plane_config: DataPlaneConfig,
) -> None:
    """The runtime URL's ACL user can neither AUTH as `default` nor SHUTDOWN, so
    the explicit force teardown must authenticate with the admin password."""
    data_plane_config = dataclasses.replace(
        data_plane_config,
        redis_url="redis://meiva:runtime-pw@127.0.0.1:16380/0",
        redis_admin_password="admin-pw",  # noqa: S106
    )
    monkeypatch.setattr(_ci.owned_postgres, "stop", lambda _data, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(pgbouncer, "stop_pgbouncer", lambda: None)
    monkeypatch.setattr(_ci, "redis_cli_bin", lambda _config: "redis-cli")  # pyright: ignore[reportUnknownArgumentType]
    shutdowns: list[tuple[list[str], str]] = []

    def _run(cmd: list[str], **kwargs: object) -> object:
        env = cast("dict[str, str]", kwargs["env"])
        shutdowns.append((cmd[1:], env["REDISCLI_AUTH"]))
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(_ci.subprocess, "run", _run)
    assert _dp.stop_cluster_instance(data_plane_config, producer=operator_pipeline) == 0
    assert shutdowns == [(["-p", "16380", "shutdown", "nosave"], "admin-pw")]
