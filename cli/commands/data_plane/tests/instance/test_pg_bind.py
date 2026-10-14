"""The cluster's own Postgres waits for its reachable bind address and starts with no inherited authority."""

import dataclasses
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from base.cluster import ownership, port_preflight
from base.cluster.machine import MachineIdentity
from base.config import settings
from cli.commands.data_plane import cluster_instance as _ci
from cli.commands.data_plane.config import DataPlaneConfig
from cli.commands.data_plane.tests.data_plane_records import with_redis_admin
from cli.commands.data_plane.tests.instance.bind_support import (
    config_boot_environment as config_boot_environment,
)
from cli.commands.data_plane.tests.instance.bind_support import native_ownership as native_ownership
from cli.commands.data_plane.tests.instance.bind_support import (
    no_native_effect as _no_native_effect,
)
from cli.commands.data_plane.tests.instance.bind_support import (
    retained_children as retained_children,
)
from cli.database.tests.command_inputs import command_machine as command_machine
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline

# ─── task #1303: postgres gets the same secret-gated reachable-bind wait ──────


def test_start_probes_receive_the_url_hosts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    retained_children: list[subprocess.Popen[bytes]],
    *,
    command_machine: MachineIdentity,
    data_plane_config: DataPlaneConfig,
) -> None:
    """The bring-up probes must be WIRED to the host their own URLs name —
    asserted against foreign URLs, so a re-hardcoded loopback literal fails
    (matching the test env's own loopback URL would be vacuous)."""
    data_plane_config = dataclasses.replace(
        data_plane_config,
        db_url="postgresql://ava:p@10.0.0.7:15433/ava",
        redis_url="redis://ava:p@10.0.0.7:16380/0",
    )
    seen: dict[str, tuple[int, str]] = {}

    def pg_running(port: int, host: str) -> bool:
        seen["pg"] = (port, host)
        return True

    def _redis_running(_config: DataPlaneConfig, port: int, host: str) -> bool:
        seen["redis"] = (port, host)
        return True

    def _ensure_redis_acl(*_args: object, **_kwargs: object) -> int:
        return 0

    monkeypatch.setattr(_ci, "pg_running", pg_running)
    monkeypatch.setattr(_ci, "redis_running", _redis_running)
    monkeypatch.setattr(_ci, "ensure_redis_acl", _ensure_redis_acl)
    monkeypatch.setattr(_ci, "ensure_pg_data", lambda: tmp_path)
    monkeypatch.setattr(ownership, "redis_data_dir", lambda: tmp_path)
    monkeypatch.setattr(_ci, "pg_socket_dir", lambda: tmp_path)
    calls: list[list[str]] = []

    def _run(cmd: list[str], **_: object) -> object:
        calls.append(cmd)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(_ci, "pg_bin", Mock(return_value="/test/postgres"))
    monkeypatch.setattr(_ci.owned_postgres, "start", _no_native_effect)
    monkeypatch.setattr(_ci.subprocess, "run", _run)

    assert (
        _ci.start_pg(
            data_plane_config, 15433, retained_children=retained_children, machine=command_machine
        )
        == 0
    )
    assert (
        _ci.start_redis(
            with_redis_admin(data_plane_config, "admin"),
            16380,
            "runtime",
            "ava",
            machine=command_machine,
        )
        == 0
    )
    assert seen == {"redis": (16380, "10.0.0.7")}


def _wire_pg_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
    *,
    running: bool = False,
) -> list[list[str]]:
    """Common mocks for start_pg: a not-running pg on a scratch data dir, with
    subprocess.run captured."""
    monkeypatch.setattr(_ci, "ensure_pg_data", lambda: tmp_path)

    def captured(*_args: object, **_kwargs: object) -> SimpleNamespace | None:
        return SimpleNamespace(pid=123, live=lambda: True) if running else None

    monkeypatch.setattr(_ci.ownership, "require_postgres", captured)
    monkeypatch.setattr(_ci, "pg_running", lambda _port, _host: running)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_ci, "pg_socket_dir", lambda: tmp_path)
    calls: list[list[str]] = []

    def _run(cmd: list[str], **_: object) -> object:
        calls.append(cmd)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    def start(
        _data: Path, _port: int, argv: list[str], _env: dict[str, str], **_kw: object
    ) -> None:
        calls.append(argv)

    monkeypatch.setattr(_ci, "pg_bin", Mock(return_value="/test/postgres"))
    monkeypatch.setattr(_ci.owned_postgres, "start", start)
    return calls


def test_start_pg_on_a_single_box_never_probes_an_address(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
    retained_children: list[subprocess.Popen[bytes]],
    *,
    command_machine: MachineIdentity,
    data_plane_config: DataPlaneConfig,
) -> None:
    """A box whose reachable address is loopback binds loopback only — no interface
    address is probed and the start proceeds."""
    command_machine.set(host="localhost")
    monkeypatch.setattr(
        _ci,
        "addr_assigned",
        lambda _a: pytest.fail("loopback-only bind must never probe an address"),  # pyright: ignore[reportUnknownArgumentType]
    )
    calls = _wire_pg_start(monkeypatch, tmp_path)

    assert (
        _ci.start_pg(
            data_plane_config, 5433, retained_children=retained_children, machine=command_machine
        )
        == 0
    )
    assert calls != [], "the start must proceed without waiting"


def test_start_pg_sets_owner_only_socket_permissions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    retained_children: list[subprocess.Popen[bytes]],
    *,
    command_machine: MachineIdentity,
    data_plane_config: DataPlaneConfig,
) -> None:
    """Postgres itself creates local trust sockets without group/world access."""
    calls = _wire_pg_start(monkeypatch, tmp_path)

    assert (
        _ci.start_pg(
            data_plane_config, 5433, retained_children=retained_children, machine=command_machine
        )
        == 0
    )
    assert "unix_socket_permissions=0700" in calls[0]
    assert Path(calls[0][0]).name == "postgres"
    assert "pg_ctl" not in calls[0]


def test_start_pg_waits_and_fails_fast_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
    capsys: pytest.CaptureFixture[str],
    retained_children: list[subprocess.Popen[bytes]],
    *,
    command_machine: MachineIdentity,
    data_plane_config: DataPlaneConfig,
) -> None:
    """Secret-set cluster, reachable address never assigned: postgres must not be
    launched into a guaranteed bind failure — fail fast with an explicit error."""

    def fake_bind_addrs(*, machine: MachineIdentity) -> list[str]:
        assert isinstance(machine, MachineIdentity)
        return ["127.0.0.1", "10.0.0.5"]

    monkeypatch.setattr(port_preflight, "bind_addrs", fake_bind_addrs)  # pyright: ignore[reportUnknownArgumentType]

    def fake_wait_for_reachable_bind(*, machine: MachineIdentity) -> bool:
        assert isinstance(machine, MachineIdentity)
        return False

    monkeypatch.setattr(_ci, "wait_for_reachable_bind", fake_wait_for_reachable_bind)
    command_machine.set(host="10.0.0.5")
    calls = _wire_pg_start(monkeypatch, tmp_path)

    rc = _ci.start_pg(
        data_plane_config, 5433, retained_children=retained_children, machine=command_machine
    )

    assert rc == 1
    assert calls == [], "postgres must not be launched when the bind address is absent"
    err = capsys.readouterr().err
    assert "not assigned to any local interface" in err
    assert "private network" in err


def test_start_pg_waits_for_reachable_bind_before_starting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
    retained_children: list[subprocess.Popen[bytes]],
    *,
    command_machine: MachineIdentity,
    data_plane_config: DataPlaneConfig,
) -> None:
    """Secret-set cluster, address appears late: the wait resolves and the start
    proceeds — the boot-race case the wait exists for."""
    waited: list[bool] = []

    def fake_bind_addrs(*, machine: MachineIdentity) -> list[str]:
        assert isinstance(machine, MachineIdentity)
        return ["127.0.0.1", "10.0.0.5"]

    monkeypatch.setattr(port_preflight, "bind_addrs", fake_bind_addrs)  # pyright: ignore[reportUnknownArgumentType]

    def fake_wait_for_reachable_bind(*, machine: MachineIdentity) -> bool:
        assert isinstance(machine, MachineIdentity)
        return waited.append(True) or True

    monkeypatch.setattr(
        _ci,
        "wait_for_reachable_bind",
        fake_wait_for_reachable_bind,
    )
    calls = _wire_pg_start(monkeypatch, tmp_path)

    assert (
        _ci.start_pg(
            data_plane_config, 5433, retained_children=retained_children, machine=command_machine
        )
        == 0
    )
    assert waited == [True]
    assert calls != []


def test_start_pg_adds_no_archive_arguments_while_wal_g_is_off(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    retained_children: list[subprocess.Popen[bytes]],
    *,
    command_machine: MachineIdentity,
    data_plane_config: DataPlaneConfig,
) -> None:
    monkeypatch.setattr(settings.walg, "walg_config_file", None)
    calls = _wire_pg_start(monkeypatch, tmp_path)

    assert (
        _ci.start_pg(
            data_plane_config, 5433, retained_children=retained_children, machine=command_machine
        )
        == 0
    )

    assert not [arg for arg in calls[0] if arg.startswith("archive_")]


def test_start_pg_launches_with_the_archive_arguments_when_wal_g_is_on(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    retained_children: list[subprocess.Popen[bytes]],
    *,
    command_machine: MachineIdentity,
    data_plane_config: DataPlaneConfig,
) -> None:
    from services.backup.walg.archive import archive_pg_args
    from services.backup.walg.tests.support import make_sandbox

    make_sandbox(tmp_path, monkeypatch)
    warned: list[bool] = []

    def record_archive_warning(**_inputs: object) -> None:
        warned.append(True)

    monkeypatch.setattr(_ci, "warn_archive_inactive", record_archive_warning)
    calls = _wire_pg_start(monkeypatch, tmp_path)

    assert (
        _ci.start_pg(
            data_plane_config, 5433, retained_children=retained_children, machine=command_machine
        )
        == 0
    )

    args = archive_pg_args(path_reader=lambda: settings.walg.walg_config_file)
    assert args and args[1] == "archive_mode=on"
    assert calls[0][-len(args) :] == args, (
        "the archive settings are launch arguments of the postmaster"
    )
    assert warned == [True], "a retained postmaster is checked after the start"


def test_the_postmaster_inherits_no_ava_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    retained_children: list[subprocess.Popen[bytes]],
    *,
    command_machine: MachineIdentity,
    data_plane_config: DataPlaneConfig,
) -> None:
    """The postmaster is the longest-lived daemon (retained across releases), and
    the `ava start` that spawns it may hold the delivered gateway login, the
    generation, the API token, the human secret and the Redis admin password.
    It keeps only the operator's process mechanics."""
    delivered = {
        "AVA_DB_URL": "postgresql://ava_g0_gateway:gen-password@127.0.0.1:6433/ava",
        "AVA_DB_GENERATION": "0",
        "AVA_API_TOKEN": "gateway-api-token-" + "t" * 32,
        "AVA_CLUSTER_SECRET": "human-" + "h" * 40,
        "AVA_REDIS_ADMIN_PASSWORD": "redis-admin-" + "r" * 20,
        "AVA_HOME": str(tmp_path / ".ava-operator"),
    }
    for key, value in {**delivered, "PATH": "/usr/bin:/bin", "TZ": "Asia/Shanghai"}.items():
        monkeypatch.setenv(key, value)

    def fake_bind_addrs(*, machine: MachineIdentity) -> list[str]:
        assert isinstance(machine, MachineIdentity)
        return ["127.0.0.1"]

    monkeypatch.setattr(port_preflight, "bind_addrs", fake_bind_addrs)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_ci, "ensure_pg_data", lambda: tmp_path)
    monkeypatch.setattr(_ci, "pg_running", lambda _port, _host: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_ci, "pg_socket_dir", lambda: tmp_path)
    envs: list[dict[str, str]] = []

    def start(
        _data: Path, _port: int, _argv: list[str], env: dict[str, str], **_kw: object
    ) -> None:
        envs.append(env)

    monkeypatch.setattr(_ci, "pg_bin", Mock(return_value="/test/postgres"))
    monkeypatch.setattr(_ci.owned_postgres, "start", start)

    assert (
        _ci.start_pg(
            data_plane_config, 5433, retained_children=retained_children, machine=command_machine
        )
        == 0
    )
    [env] = envs
    assert env["PATH"] == "/usr/bin:/bin" and env["TZ"] == "Asia/Shanghai"
    assert not [key for key in env if key.startswith("AVA_")]
    assert not set(delivered.values()) & set(env.values())


def test_start_pg_hands_the_built_start_env_to_owned_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
    retained_children: list[subprocess.Popen[bytes]],
    *,
    command_machine: MachineIdentity,
    data_plane_config: DataPlaneConfig,
) -> None:
    """Task #3754: direct postgres gets pg_start_env() — the postmaster env is
    built explicitly (the macOS locale fallback for launchd / non-interactive
    ssh starts) instead of inheriting whatever process brought the cluster up."""

    def fake_bind_addrs(*, machine: MachineIdentity) -> list[str]:
        assert isinstance(machine, MachineIdentity)
        return ["127.0.0.1"]

    monkeypatch.setattr(port_preflight, "bind_addrs", fake_bind_addrs)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_ci, "ensure_pg_data", lambda: tmp_path)
    monkeypatch.setattr(_ci, "pg_running", lambda _port, _host: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_ci, "pg_socket_dir", lambda: tmp_path)
    sentinel = {"LC_ALL": "en_US.UTF-8", "PATH": "/usr/bin:/bin"}
    monkeypatch.setattr(_ci, "pg_start_env", lambda: sentinel)
    envs: list[object] = []

    def start(
        _data: Path, _port: int, _argv: list[str], env: dict[str, str], **_kw: object
    ) -> None:
        envs.append(env)

    monkeypatch.setattr(_ci, "pg_bin", Mock(return_value="/test/postgres"))
    monkeypatch.setattr(_ci.owned_postgres, "start", start)

    assert (
        _ci.start_pg(
            data_plane_config, 5433, retained_children=retained_children, machine=command_machine
        )
        == 0
    )
    assert envs == [sentinel]
