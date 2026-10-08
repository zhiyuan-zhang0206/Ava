"""Bounded wait for the reachable (private-network) bind address before starting pg.

On reboot brew/launchd can start `ava` before the private network has assigned its address;
binding pg to a not-yet-present address fails and takes the whole autostart
down. `_wait_for_reachable_bind` blocks (bounded) until the address is assigned, and
fails fast on timeout. A loopback-only single box never waits.
"""

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest

from base.cluster import ownership, port_preflight
from base.config import settings
from cli.commands.data_plane import cluster_instance as _ci


def _no_native_effect(*_args: object, **_kwargs: object) -> None:
    pass


@pytest.fixture(autouse=True)
def _native_ownership(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_ci.ownership, "require_listener", _no_native_effect)
    monkeypatch.setattr(_ci.ownership, "require_postgres", _no_native_effect)
    # The loaded-hba proof dials the postmaster; real Postgres covers it
    # (cli/commands/tests/test_single_box.py).
    monkeypatch.setattr(_ci, "require_authenticated_hba", _no_native_effect)


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


def _pg_socket_path(root: Path, home: Path) -> Path:
    from base.cluster import home_slug

    return root / f"ava-pg-{home_slug(home)}"


def test_macos_redis_binaries_use_versioned_formula(monkeypatch: pytest.MonkeyPatch) -> None:
    formulae: list[str] = []

    def fake_brew_prefix(formula: str) -> Path:
        formulae.append(formula)
        return Path("/opt/homebrew/opt") / formula

    monkeypatch.setattr(_ci, "is_macos", lambda: True)
    monkeypatch.setattr(_ci, "brew_prefix", fake_brew_prefix)
    # Exercise formula discovery independently of this machine's explicit pin.
    monkeypatch.setattr(settings.data_plane, "redis_bin_dir", "")

    assert _ci._redis_server_bin() == "/opt/homebrew/opt/redis@8.2/bin/redis-server"
    assert _ci._redis_cli_bin() == "/opt/homebrew/opt/redis@8.2/bin/redis-cli"
    assert formulae == ["redis@8.2", "redis@8.2"]


def test_addr_assigned_loopback_is_true() -> None:
    assert _ci._addr_assigned("127.0.0.1") is True


def test_addr_assigned_unassigned_ip_is_false() -> None:
    """192.0.2.0/24 (RFC 5737 TEST-NET-1) is never assigned to a real interface, so
    binding it raises EADDRNOTAVAIL — the exact 'address not up yet' signal."""
    assert _ci._addr_assigned("192.0.2.1") is False


def test_wait_returns_immediately_for_loopback_only_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single box reachable only at localhost never waits — loopback is always up."""
    monkeypatch.setattr("base.cluster.machine.reachable_host", lambda: "localhost")
    monkeypatch.setattr(
        _ci,
        "_addr_assigned",
        lambda _a: pytest.fail("must not probe on a loopback-only host"),  # pyright: ignore[reportUnknownArgumentType]
    )
    assert _ci._wait_for_reachable_bind() is True


def test_wait_returns_true_once_address_appears(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reachable address is absent on the first probe, present on the next — the
    private-network-coming-up-late case — so the wait resolves True after retrying."""
    monkeypatch.setattr("base.cluster.machine.reachable_host", lambda: "10.0.0.5")
    monkeypatch.setattr(_ci.time, "sleep", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]
    seen: list[int] = []

    def _appears(_addr: str) -> bool:
        seen.append(1)
        return len(seen) >= 2

    monkeypatch.setattr(_ci, "_addr_assigned", _appears)
    assert _ci._wait_for_reachable_bind() is True
    assert len(seen) == 2


def test_wait_returns_false_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The address never appears within the bound → fail fast (caller aborts start)."""
    monkeypatch.setattr("base.cluster.machine.reachable_host", lambda: "10.0.0.5")
    monkeypatch.setattr(_ci, "_BIND_WAIT_TIMEOUT_S", 0.0)
    monkeypatch.setattr(_ci.time, "sleep", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_ci, "_addr_assigned", lambda _a: False)  # pyright: ignore[reportUnknownArgumentType]
    assert _ci._wait_for_reachable_bind() is False


# ─── no-secret posture: loopback-only binds + trust-only pg_hba ──────────────


def test_pg_hba_body_authenticates_without_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """No secret -> the data plane still authenticates: the OS user only by peer
    on the owner-only socket, every other role SCRAM; no trust line anywhere and
    no reachable/cidr lines (the bind stays loopback-only)."""
    monkeypatch.setattr(settings.data_plane, "trusted_cidrs", "10.0.0.0/8")
    monkeypatch.setattr("base.cluster.machine.reachable_host", lambda: "10.0.0.5")
    body = _ci._pg_hba_body("")
    assert "trust" not in body
    assert body.splitlines() == [_admin_peer_line(), *_ALWAYS_AUTH_LOOPBACK]


def test_pg_ident_maps_only_the_os_user_to_the_monitoring_role() -> None:
    """The ident map the monitor's peer line names admits exactly this OS user
    as `ava_monitor`; it is the file's only mapping."""
    import getpass

    assert _ci._pg_ident_body() == f"ava_monitor {getpass.getuser()} ava_monitor\n"


def test_pg_hba_body_scram_with_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a secret, the reachable host and trusted CIDRs join as SCRAM lines."""
    monkeypatch.setattr(settings.data_plane, "trusted_cidrs", "10.0.0.0/8")
    monkeypatch.setattr("base.cluster.machine.reachable_host", lambda: "10.0.0.5")
    body = _ci._pg_hba_body("s3cret")
    assert body.splitlines() == [
        _admin_peer_line(),
        *_ALWAYS_AUTH_LOOPBACK,
        "host all all 10.0.0.5/32 scram-sha-256",
        "host all all 10.0.0.0/8 scram-sha-256",
    ]


# ─── Task #1113: the passed secret wins over ambient settings ────────────────


def test_pg_hba_body_follows_passed_secret_not_ambient_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hba is written from the passed cluster secret, never from ambient
    settings — otherwise a no-secret cluster born from a prod-sourced shell
    gains LAN-reachable host lines keyed to a FOREIGN cluster's posture."""
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "foreign-sibling-secret")
    monkeypatch.setattr(settings.data_plane, "trusted_cidrs", "10.0.0.0/8")
    monkeypatch.setattr("base.cluster.machine.reachable_host", lambda: "10.0.0.5")
    body = _ci._pg_hba_body("")
    assert "10.0.0" not in body
    assert body.splitlines() == [_admin_peer_line(), *_ALWAYS_AUTH_LOOPBACK]


# ─── task #1469: macOS retains its loopback Redis workaround ──────────────────


def _wire_redis_start(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[list[str]]:
    """Capture a successful fresh Redis start without touching the cluster home."""
    monkeypatch.setattr(_ci, "is_macos", lambda: True)
    monkeypatch.setattr(
        _ci,
        "_wait_for_reachable_bind",
        lambda: pytest.fail("redis must never wait for the reachable bind"),
    )
    monkeypatch.setattr(_ci, "_redis_server_bin", lambda: "redis-server")
    monkeypatch.setattr(ownership, "redis_data_dir", lambda: tmp_path)
    redis_answers = iter([False, True])  # not running before start, up after
    monkeypatch.setattr(
        _ci,
        "_redis_running",
        lambda *_a, **_kw: next(redis_answers),  # pyright: ignore[reportUnknownArgumentType]
    )
    started: list[list[str]] = []

    def _run(cmd: list[str], **_: object) -> object:
        started.append(cmd)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(_ci.subprocess, "run", _run)
    monkeypatch.setattr(_ci, "_ensure_redis_acl", lambda *_a, **_kw: 0)  # pyright: ignore[reportUnknownArgumentType]
    return started


def _redis_bind_arg(command: list[str]) -> list[str]:
    """Return the complete `--bind` option through the next Redis option."""
    start = command.index("--bind")
    end = command.index("--protected-mode")
    return command[start:end]


def test_start_redis_binds_loopback_only_without_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A no-secret Redis start uses exactly the loopback bind, and still
    authenticates: the bearer decides reach, never whether Redis has a password."""
    monkeypatch.setattr("base.cluster.machine.reachable_host", lambda: "10.0.0.5")
    started = _wire_redis_start(monkeypatch, tmp_path)

    assert _ci.start_redis(6380, "redis-admin", "redis-runtime", "", "ava") == 0
    assert _redis_bind_arg(started[0]) == ["--bind", "127.0.0.1"]
    assert "--save" not in started[0]  # persistence rides the rendered conf, not argv
    conf = (tmp_path / "redis.conf").read_text()
    assert conf.startswith("save 900 1")
    assert conf.endswith('requirepass "redis-admin"\n')


def test_the_redis_daemon_inherits_no_ava_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The boot pass may have delivered the gateway login, the generation and
    the API token to this process; the daemon it spawns keeps none of them."""
    delivered = {
        "AVA_DB_URL": "postgresql://ava_g0_gateway:gen-password@127.0.0.1:6433/ava",
        "AVA_DB_GENERATION": "0",
        "AVA_API_TOKEN": "gateway-api-token-" + "t" * 32,
        "AVA_CLUSTER_SECRET": "human-" + "h" * 40,
        "AVA_REDIS_ADMIN_PASSWORD": "redis-admin",
    }
    for key, value in {**delivered, "PATH": "/usr/bin:/bin", "TZ": "Asia/Shanghai"}.items():
        monkeypatch.setenv(key, value)
    _wire_redis_start(monkeypatch, tmp_path)
    envs: list[object] = []

    def _run(cmd: list[str], **kwargs: object) -> object:
        envs.append(kwargs.get("env"))
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(_ci.subprocess, "run", _run)
    assert _ci.start_redis(6380, "redis-admin", "redis-runtime", "", "ava") == 0
    [captured] = envs
    assert isinstance(captured, dict)
    env = cast("dict[str, str]", captured)
    assert env["PATH"] == "/usr/bin:/bin" and env["TZ"] == "Asia/Shanghai"
    assert not [key for key in env if key.startswith("AVA_")]
    assert not set(delivered.values()) & set(env.values())


@pytest.mark.parametrize(
    ("admin", "runtime"), [("", ""), ("", "redis-runtime"), ("redis-admin", "")]
)
def test_start_redis_refuses_missing_credentials_before_effects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, admin: str, runtime: str
) -> None:
    """Redis never starts, or is re-affirmed, without both generated passwords;
    there is no password-less posture to fall back to."""
    started = _wire_redis_start(monkeypatch, tmp_path)
    monkeypatch.setattr(
        _ci,
        "_ensure_redis_acl",
        lambda *_a, **_kw: pytest.fail("ACL effect without credentials"),  # pyright: ignore[reportUnknownArgumentType]
    )
    with pytest.raises(ValueError, match="password"):
        _ci.start_redis(6380, admin, runtime, "", "ava")
    assert started == []
    assert not (tmp_path / "redis.conf").exists()


def test_redis_admin_helpers_refuse_an_empty_password(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="admin password"):
        _ci._write_redis_conf(tmp_path, "")
    with pytest.raises(ValueError, match="admin password"):
        _ci._redis_cli_env("")
    assert not (tmp_path / "redis.conf").exists()


def test_macos_start_redis_binds_loopback_only_with_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The macOS workaround retains its external relay even with auth."""
    monkeypatch.setattr("base.cluster.machine.reachable_host", lambda: "10.0.0.5")
    started = _wire_redis_start(monkeypatch, tmp_path)

    assert _ci.start_redis(6380, "redis-admin", "redis-runtime", "s3cr3t", "ava") == 0
    assert _redis_bind_arg(started[0]) == ["--bind", "127.0.0.1"]


def test_macos_start_redis_does_not_use_shared_pg_bind_addrs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The shared helper may remain dual-bind for pg without widening Redis."""
    monkeypatch.setattr(
        port_preflight,
        "bind_addrs",
        lambda _secret: pytest.fail("redis must not use the shared pg bind helper"),  # pyright: ignore[reportUnknownArgumentType]
    )
    started = _wire_redis_start(monkeypatch, tmp_path)

    assert _ci.start_redis(6380, "redis-admin", "redis-runtime", "s3cr3t", "ava") == 0
    assert _redis_bind_arg(started[0]) == ["--bind", "127.0.0.1"]


@pytest.mark.parametrize("secret", ["", "cluster-bearer"])
def test_linux_redis_bind_uses_caller_secret_not_inherited_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, secret: str
) -> None:
    started = _wire_redis_start(monkeypatch, tmp_path)
    monkeypatch.setattr(_ci, "is_macos", lambda: False)
    monkeypatch.setattr("base.cluster.machine.reachable_host", lambda: "10.0.0.5")
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "" if secret else "polluted")
    waits: list[bool] = []

    def address_ready() -> bool:
        waits.append(True)
        return True

    monkeypatch.setattr(_ci, "_wait_for_reachable_bind", address_ready)
    assert _ci.start_redis(6380, "admin", "runtime", secret, "ava") == 0
    expected = ["--bind", "127.0.0.1", "10.0.0.5"] if secret else ["--bind", "127.0.0.1"]
    assert _redis_bind_arg(started[0]) == expected
    assert waits == ([True] if secret else [])


def test_linux_redis_refuses_start_when_reachable_address_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = _wire_redis_start(monkeypatch, tmp_path)
    monkeypatch.setattr(_ci, "is_macos", lambda: False)
    monkeypatch.setattr(_ci, "_wait_for_reachable_bind", lambda: False)
    assert _ci.start_redis(6380, "admin", "runtime", "bearer", "ava") == 1
    assert started == []
    assert not (tmp_path / "redis.conf").exists()


def test_running_redis_persists_the_authenticated_password_to_its_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A journal retry repairs config after an old-password false-down probe."""
    monkeypatch.setattr(ownership, "redis_data_dir", lambda: tmp_path)
    monkeypatch.setattr(_ci, "_redis_running", lambda *_args: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_ci, "_ensure_redis_acl", lambda *_args: 0)  # pyright: ignore[reportUnknownArgumentType]
    (tmp_path / "redis.conf").write_text('requirepass "stale-old-password"\n')

    assert _ci.start_redis(6380, "journal-password", "runtime", "bearer", "ava") == 0
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
        _ci._write_redis_conf(tmp_path, "new-complete")

    assert conf.read_text() == 'requirepass "old-complete"\n'


def test_redis_conf_always_renders_rdb_save_schedule(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A no-secret cluster still persists and authenticates: the RDB save
    schedule and requirepass survive every conf render, or a restart silently
    loses persistence (task #2027) or comes back without a password."""
    monkeypatch.setattr(ownership, "redis_data_dir", lambda: tmp_path)
    monkeypatch.setattr(_ci, "_redis_running", lambda *_args: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_ci, "_ensure_redis_acl", lambda *_args: 0)  # pyright: ignore[reportUnknownArgumentType]

    assert _ci.start_redis(6380, "admin", "runtime", "", "ava") == 0
    assert (tmp_path / "redis.conf").read_text() == (
        'save 900 1\nsave 300 10\nsave 60 10000\nrequirepass "admin"\n'
    )


def test_force_stop_shuts_redis_down_as_admin_not_runtime_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runtime URL's ACL user can neither AUTH as `default` nor SHUTDOWN, so
    the explicit force teardown must authenticate with the admin password."""
    monkeypatch.setattr(
        settings.data_plane, "redis_url", "redis://ava:runtime-pw@127.0.0.1:16380/0"
    )
    monkeypatch.setattr(settings.data_plane, "redis_admin_password", "admin-pw")
    monkeypatch.setattr(_ci.owned_postgres, "stop", lambda _data, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands.data_plane.pgbouncer.stop_pgbouncer", lambda: None)
    monkeypatch.setattr(_ci, "_redis_cli_bin", lambda: "redis-cli")
    shutdowns: list[tuple[list[str], str]] = []

    def _run(cmd: list[str], **kwargs: object) -> object:
        env = cast("dict[str, str]", kwargs["env"])
        shutdowns.append((cmd[1:], env["REDISCLI_AUTH"]))
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(_ci.subprocess, "run", _run)
    assert _ci.stop_cluster_instance() == 0
    assert shutdowns == [(["-p", "16380", "shutdown", "nosave"], "admin-pw")]


# ─── task #1303: postgres gets the same secret-gated reachable-bind wait ──────


def test_start_probes_receive_the_url_hosts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    retained_children: list[subprocess.Popen[bytes]],
) -> None:
    """The bring-up probes must be WIRED to the host their own URLs name —
    asserted against foreign URLs, so a re-hardcoded loopback literal fails
    (matching the test env's own loopback URL would be vacuous)."""
    monkeypatch.setattr(settings.data_plane, "db_url", "postgresql://ava:p@10.0.0.7:15433/ava")
    monkeypatch.setattr(settings.data_plane, "redis_url", "redis://ava:p@10.0.0.7:16380/0")
    seen: dict[str, tuple[int, str]] = {}

    def _pg_running(port: int, host: str) -> bool:
        seen["pg"] = (port, host)
        return True

    def _redis_running(port: int, _password: str, host: str) -> bool:
        seen["redis"] = (port, host)
        return True

    def _ensure_redis_acl(*_args: object, **_kwargs: object) -> int:
        return 0

    monkeypatch.setattr(_ci, "_pg_running", _pg_running)
    monkeypatch.setattr(_ci, "_redis_running", _redis_running)
    monkeypatch.setattr(_ci, "_ensure_redis_acl", _ensure_redis_acl)
    monkeypatch.setattr(_ci, "_ensure_pg_data", lambda: tmp_path)
    monkeypatch.setattr(ownership, "redis_data_dir", lambda: tmp_path)
    monkeypatch.setattr(_ci, "_pg_socket_dir", lambda: tmp_path)
    calls: list[list[str]] = []

    def _run(cmd: list[str], **_: object) -> object:
        calls.append(cmd)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(_ci, "_pg_bin", Mock(return_value="/test/postgres"))
    monkeypatch.setattr(_ci.owned_postgres, "start", _no_native_effect)
    monkeypatch.setattr(_ci.subprocess, "run", _run)

    assert _ci._start_pg(15433, "", retained_children=retained_children) == 0
    assert _ci.start_redis(16380, "admin", "runtime", "", "ava") == 0
    assert seen == {"redis": (16380, "10.0.0.7")}


def test_pg_socket_dir_rejects_symlink(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The predictable /tmp socket location cannot follow a pre-placed symlink."""
    home = tmp_path / "home"
    monkeypatch.setattr(_ci, "ava_home", lambda: home)
    socket_dir = _pg_socket_path(tmp_path, home)
    socket_dir.symlink_to(tmp_path / "elsewhere", target_is_directory=True)

    with pytest.raises(RuntimeError, match="symlink"):
        _ci._pg_socket_dir(tmp_path)


def test_pg_socket_dir_rejects_foreign_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A socket directory owned by another local account is refused."""
    home = tmp_path / "home"
    monkeypatch.setattr(_ci, "ava_home", lambda: home)
    socket_dir = _pg_socket_path(tmp_path, home)
    socket_dir.mkdir()
    owner = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: owner + 1)

    with pytest.raises(RuntimeError, match="not owned by the current user"):
        _ci._pg_socket_dir(tmp_path)


def test_pg_socket_dir_repairs_mode_drift(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Existing socket directories are tightened before Postgres binds a trust socket."""
    home = tmp_path / "home"
    monkeypatch.setattr(_ci, "ava_home", lambda: home)
    socket_dir = _pg_socket_path(tmp_path, home)
    socket_dir.mkdir()
    socket_dir.chmod(0o755)

    assert _ci._pg_socket_dir(tmp_path) == socket_dir
    assert socket_dir.stat().st_mode & 0o777 == 0o700


def _wire_pg_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
    *,
    running: bool = False,
) -> list[list[str]]:
    """Common mocks for _start_pg: a not-running pg on a scratch data dir, with
    subprocess.run captured."""
    monkeypatch.setattr(_ci, "_ensure_pg_data", lambda: tmp_path)

    def captured(*_args: object, **_kwargs: object) -> SimpleNamespace | None:
        return SimpleNamespace(pid=123, live=lambda: True) if running else None

    monkeypatch.setattr(_ci.ownership, "require_postgres", captured)
    monkeypatch.setattr(_ci, "_pg_running", lambda _port, _host: running)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_ci, "_pg_socket_dir", lambda: tmp_path)
    calls: list[list[str]] = []

    def _run(cmd: list[str], **_: object) -> object:
        calls.append(cmd)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    def start(
        _data: Path, _port: int, argv: list[str], _env: dict[str, str], **_kw: object
    ) -> None:
        calls.append(argv)

    monkeypatch.setattr(_ci, "_pg_bin", Mock(return_value="/test/postgres"))
    monkeypatch.setattr(_ci.owned_postgres, "start", start)
    return calls


def test_start_pg_loopback_only_bind_never_waits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
    retained_children: list[subprocess.Popen[bytes]],
) -> None:
    """A no-secret cluster binds loopback only — the wait is never consulted and
    the start proceeds (a stray AVA_MACHINE_HOST must not hold a warm start)."""
    monkeypatch.setattr(port_preflight, "bind_addrs", lambda _secret: ["127.0.0.1"])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _ci,
        "_wait_for_reachable_bind",
        lambda: pytest.fail("loopback-only bind must never wait"),
    )
    calls = _wire_pg_start(monkeypatch, tmp_path)

    assert _ci._start_pg(5433, "", retained_children=retained_children) == 0
    assert calls != [], "the start must proceed without waiting"


def test_start_pg_sets_owner_only_socket_permissions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    retained_children: list[subprocess.Popen[bytes]],
) -> None:
    """Postgres itself creates local trust sockets without group/world access."""
    calls = _wire_pg_start(monkeypatch, tmp_path)

    assert _ci._start_pg(5433, "", retained_children=retained_children) == 0
    assert "unix_socket_permissions=0700" in calls[0]
    assert Path(calls[0][0]).name == "postgres"
    assert "pg_ctl" not in calls[0]


def test_start_pg_waits_and_fails_fast_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
    capsys: pytest.CaptureFixture[str],
    retained_children: list[subprocess.Popen[bytes]],
) -> None:
    """Secret-set cluster, reachable address never assigned: postgres must not be
    launched into a guaranteed bind failure — fail fast with an explicit error."""
    monkeypatch.setattr(port_preflight, "bind_addrs", lambda _secret: ["127.0.0.1", "10.0.0.5"])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_ci, "_wait_for_reachable_bind", lambda: False)
    monkeypatch.setattr("base.cluster.machine.reachable_host", lambda: "10.0.0.5")
    calls = _wire_pg_start(monkeypatch, tmp_path)

    rc = _ci._start_pg(5433, "s3cr3t", retained_children=retained_children)

    assert rc == 1
    assert calls == [], "postgres must not be launched when the bind address is absent"
    err = capsys.readouterr().err
    assert "not assigned to any local interface" in err
    assert "private network" in err


def test_start_pg_waits_for_reachable_bind_before_starting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
    retained_children: list[subprocess.Popen[bytes]],
) -> None:
    """Secret-set cluster, address appears late: the wait resolves and the start
    proceeds — the boot-race case the wait exists for."""
    waited: list[bool] = []
    monkeypatch.setattr(port_preflight, "bind_addrs", lambda _secret: ["127.0.0.1", "10.0.0.5"])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _ci,
        "_wait_for_reachable_bind",
        lambda: waited.append(True) or True,
    )
    calls = _wire_pg_start(monkeypatch, tmp_path)

    assert _ci._start_pg(5433, "s3cr3t", retained_children=retained_children) == 0
    assert waited == [True]
    assert calls != []


def test_start_pg_adds_no_archive_arguments_while_wal_g_is_off(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    retained_children: list[subprocess.Popen[bytes]],
) -> None:
    monkeypatch.setattr(settings.walg, "walg_config_file", None)
    calls = _wire_pg_start(monkeypatch, tmp_path)

    assert _ci._start_pg(5433, "", retained_children=retained_children) == 0

    assert not [arg for arg in calls[0] if arg.startswith("archive_")]


def test_start_pg_launches_with_the_archive_arguments_when_wal_g_is_on(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    retained_children: list[subprocess.Popen[bytes]],
) -> None:
    from services.backup.walg.archive import archive_pg_args
    from services.backup.walg.tests.support import make_sandbox

    make_sandbox(tmp_path, monkeypatch)
    warned: list[bool] = []
    monkeypatch.setattr(_ci, "warn_archive_inactive", lambda: warned.append(True))
    calls = _wire_pg_start(monkeypatch, tmp_path)

    assert _ci._start_pg(5433, "", retained_children=retained_children) == 0

    args = archive_pg_args()
    assert args and args[1] == "archive_mode=on"
    assert calls[0][-len(args) :] == args, (
        "the archive settings are launch arguments of the postmaster"
    )
    assert warned == [True], "a retained postmaster is checked after the start"


def test_the_postmaster_inherits_no_ava_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    retained_children: list[subprocess.Popen[bytes]],
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
    monkeypatch.setattr(port_preflight, "bind_addrs", lambda _secret: ["127.0.0.1"])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_ci, "_ensure_pg_data", lambda: tmp_path)
    monkeypatch.setattr(_ci, "_pg_running", lambda _port, _host: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_ci, "_pg_socket_dir", lambda: tmp_path)
    envs: list[dict[str, str]] = []

    def start(
        _data: Path, _port: int, _argv: list[str], env: dict[str, str], **_kw: object
    ) -> None:
        envs.append(env)

    monkeypatch.setattr(_ci, "_pg_bin", Mock(return_value="/test/postgres"))
    monkeypatch.setattr(_ci.owned_postgres, "start", start)

    assert _ci._start_pg(5433, "", retained_children=retained_children) == 0
    [env] = envs
    assert env["PATH"] == "/usr/bin:/bin" and env["TZ"] == "Asia/Shanghai"
    assert not [key for key in env if key.startswith("AVA_")]
    assert not set(delivered.values()) & set(env.values())


def test_start_pg_hands_the_built_start_env_to_owned_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
    retained_children: list[subprocess.Popen[bytes]],
) -> None:
    """Task #3754: direct postgres gets pg_start_env() — the postmaster env is
    built explicitly (the macOS locale fallback for launchd / non-interactive
    ssh starts) instead of inheriting whatever process brought the cluster up."""
    monkeypatch.setattr(port_preflight, "bind_addrs", lambda _secret: ["127.0.0.1"])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_ci, "_ensure_pg_data", lambda: tmp_path)
    monkeypatch.setattr(_ci, "_pg_running", lambda _port, _host: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_ci, "_pg_socket_dir", lambda: tmp_path)
    sentinel = {"LC_ALL": "en_US.UTF-8", "PATH": "/usr/bin:/bin"}
    monkeypatch.setattr(_ci, "pg_start_env", lambda: sentinel)
    envs: list[object] = []

    def start(
        _data: Path, _port: int, _argv: list[str], env: dict[str, str], **_kw: object
    ) -> None:
        envs.append(env)

    monkeypatch.setattr(_ci, "_pg_bin", Mock(return_value="/test/postgres"))
    monkeypatch.setattr(_ci.owned_postgres, "start", start)

    assert _ci._start_pg(5433, "", retained_children=retained_children) == 0
    assert envs == [sentinel]


@pytest.fixture
def retained_children() -> list[subprocess.Popen[bytes]]:
    return []
