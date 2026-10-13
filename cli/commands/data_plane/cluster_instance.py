"""Per-cluster native Postgres+Redis bring-up.

Every cluster (including `main`) runs its OWN Postgres and Redis under its
`$AVA_HOME` on the fixed port table (`base.host.env.port_table`) — this is the only
data-plane path; there is no shared host instance.

Model (mirrors `base.cluster.dataplane.pg_tools.throwaway_postgres`, but persistent + authed):

- Postgres `initdb`s into `$AVA_HOME/pg` (cold) — cached through a host-level
  template dir so a new cluster / a test spins up by directory copy, not a fresh
  multi-second init — and is launched directly with durable native custody on its pg port.
  pg_hba ALWAYS authenticates, whatever the bearer: the OS-user bootstrap
  superuser is the sole passwordless identity (`peer` on the 0700 owner-only
  socket — the administrator authority), and every other role is SCRAM over
  the socket and TCP. The schema owner is NOLOGIN; application processes log in
  only as the home's write generation (`base.cluster.authority`).
- Redis runs `redis-server` on the cluster's redis port and ALWAYS authenticates,
  whatever the bearer: `requirepass` = the gateway-only Redis admin password, and
  the cluster's ACL user has its own runtime password. Both are minted at first
  start; a home without them is refused, never converted. Data dir under `$AVA_HOME/redis`. The
  Redis admin password reaches redis through a 0600 `redis.conf` and reaches `redis-cli`
  through `$REDISCLI_AUTH` — never argv, which `ps` shows to any local user
  (issue #974).

The Postgres db/role and Redis ACL identifiers are independent URL data: callers
read each from its own `.env` URL (`db_identity()` / `redis_identity()`) and pass
`identity` / `redis_user` explicitly. First-start identity supplies the fixed
`DATA_PLANE_IDENTITY` for both; later starts preserve different existing names.

Bind posture — authenticated Postgres, PgBouncer and Linux Redis bind loopback
and this host's declared reachable address (`AVA_MACHINE_HOST`), never all
interfaces; a host that declares none (`localhost`) binds loopback only. macOS Redis
retains its loopback-only workaround and the root-supervised `redis-bridge` relay
(task #1469).

POSIX only (macOS brew / Linux postgres + redis-server).
"""

from __future__ import annotations

import getpass
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from base.cluster import ensure_cluster_redis_acl, ownership, port_preflight
from base.cluster import postgres as owned_postgres
from base.cluster.authority.monitor import MONITOR_MAP, MONITOR_ROLE
from base.cluster.dataplane.pg_admin import pg_admin_url as _base_pg_admin_url
from base.cluster.dataplane.pg_admin import pg_socket_dir as _base_pg_socket_dir
from base.cluster.dataplane.pg_tools import pg_shm_args, pg_start_env, pg_tool, pg_tz_args
from base.cluster.machine import MachineIdentity
from base.config import ConfigBoot, settings
from base.host.brew_pin import brew_prefix
from base.host.net.url_secret import url_host
from base.host.private_storage import write_private_bytes
from base.host.system.backend import PG_BIN_LINUX
from base.native_process.child_env import daemon_process_env, inherited_process_env
from base.native_process.os_platform import is_macos
from base.paths import ava_home
from cli.commands.data_plane.walg import warn_archive_inactive
from services.backup.walg.archive import archive_pg_args


def pg_dial_host() -> str:
    """The host this cluster's own Postgres is dialed at — read from this
    cluster's AVA_DB_URL (the URL is the one dial source every consumer uses;
    DataPlaneSettings rewrites a self-host URL to loopback before anything
    dials). The home identity and its URLs exist before native bring-up.
    External data-plane reachability is handled separately by `bringup`.
    """
    return url_host(settings.data_plane.db_url)


def redis_dial_host() -> str:
    """The host this cluster's own Redis is dialed at — read from this
    cluster's AVA_REDIS_URL (see `pg_dial_host`)."""
    return url_host(settings.data_plane.redis_url)


# A single cluster's own pg needs far less than a per-host shared budget. `main`
# on its own instance carries the whole prod fleet, so keep enough headroom (the
# gateway + long-running services alone hold ~25, then ~3-4 per agent).
_PG_MAX_CONNECTIONS = 500
_PG_START_TIMEOUT_S = 60.0
_PG_PROBE_TIMEOUT_S = 3.0

# Bounded wait for this host's non-loopback bind address (AVA_MACHINE_HOST) to
# appear on a local interface before the Postgres data plane binds to it. On
# reboot brew / launchd can start `ava` before the private network has assigned
# the address, so binding it fails and the whole autostart dies.
BIND_WAIT_TIMEOUT_S = 60.0
_BIND_WAIT_INTERVAL_S = 2.0


def pg_data_dir() -> Path:
    return ava_home() / "pg"


def _pg_template_dir() -> Path:
    """Cached `initdb` output, copied so a new instance is a directory copy rather
    than a multi-second init. Kept in the home, beside the data directory it seeds."""
    return ava_home() / "pg-template-17"


def _redis_bin(name: str) -> str:
    """One home-owned pair; an incomplete override never falls back to PATH."""
    directory = settings.data_plane.redis_bin_dir
    if directory:
        root = Path(directory)
        for tool in ("redis-server", "redis-cli"):
            path = root / tool
            if not path.is_file() or not os.access(path, os.X_OK):
                raise RuntimeError(f"AVA_REDIS_BIN_DIR requires an executable file: {path}")
        return str(root / name)
    return str(brew_prefix("redis@8.2") / "bin" / name) if is_macos() else name


def _redis_server_bin() -> str:
    return _redis_bin("redis-server")


def redis_cli_bin() -> str:
    return _redis_bin("redis-cli")


def _pg_bin(name: str) -> str:
    return str(pg_tool(name)) if is_macos() else str(PG_BIN_LINUX / name)


def _addr_assigned(addr: str) -> bool:
    """True if `addr` is currently assigned to a local interface (bindable). A
    Postgres or PgBouncer listener can only bind an address the kernel has on an
    interface; before the private-network interface comes up, binding its address
    fails with EADDRNOTAVAIL. Probing with a throwaway bind is the portable check."""
    family = socket.AF_INET6 if ":" in addr else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.bind((addr, 0))
        return True
    except OSError:
        return False


def wait_for_reachable_bind(*, machine: MachineIdentity) -> bool:
    """Block (bounded) until this host's non-loopback bind address is assigned to a
    local interface. Returns True immediately on a loopback-only (single-box) host —
    loopback is always present. Returns False on timeout so the caller fails fast
    with a clear message instead of pg_ctl dying on an un-bindable address."""
    host = machine.host()
    if host in port_preflight.LOOPBACK_ALIASES:
        return True
    deadline = time.monotonic() + BIND_WAIT_TIMEOUT_S
    while True:
        if _addr_assigned(host):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_BIND_WAIT_INTERVAL_S)


def _pg_hba_body(*, machine: MachineIdentity) -> str:
    """Always-authenticated pg_hba.

    The OS user (the initdb bootstrap superuser) reaches Postgres only through
    `peer` on the owner-only socket — the administrator authority provisioning,
    migrations and the authority invariant use. The same OS user reaches the
    password-less monitoring login (the collector's statistics reader) through
    `peer` with the `pg_ident` map `_pg_ident_body` writes. Every other role
    authenticates with SCRAM over the socket and loopback TCP; `NOLOGIN` roles
    (the schema owner and the capability groups) never log in
    under any method.

    The declared reach adds SCRAM host lines, matching the bind posture
    (`port_preflight.bind_addrs`): the reachable address when it is not a loopback
    alias, and each `trusted_cidrs` range. A single box (reachable address
    `localhost`, no CIDRs) has loopback lines only. Both are cluster-scope fields
    the env-authority pass drops when undeclared, so neither has an ambient-leak
    vector."""
    lines = [
        f"local all {getpass.getuser()} peer",
        f"local all {MONITOR_ROLE} peer map={MONITOR_MAP}",
        "local all all scram-sha-256",
        "host all all 127.0.0.1/32 scram-sha-256",
        "host all all ::1/128 scram-sha-256",
    ]
    host = machine.host()
    if host not in port_preflight.LOOPBACK_ALIASES:
        lines.append(f"host all all {host}/32 scram-sha-256")
    for cidr in (c.strip() for c in settings.data_plane.trusted_cidrs.split(",") if c.strip()):
        lines.append(f"host all all {cidr} scram-sha-256")
    return "\n".join(lines) + "\n"


def _pg_ident_body() -> str:
    """The only ident mapping: this OS user may log in as the monitoring role."""
    return f"{MONITOR_MAP} {getpass.getuser()} {MONITOR_ROLE}\n"


# The server asks a password-less client for one; libpq reports either wording.
_PASSWORD_DEMANDED = ("no password supplied", "password authentication failed")
_HBA_PROOF_TIMEOUT_S = 10.0


def require_authenticated_hba(pg_port: int, dial_host: str) -> None:
    """Prove the RUNNING postmaster enforces password authentication.

    `pg_hba_file_rules` shows the file, not what the postmaster loaded, and a
    retained postmaster reloads asynchronously after SIGHUP. So the proof is
    behavioral: a password-less dial as a role name that cannot exist must be
    asked for a password over loopback TCP and over the socket. A trust line
    would instead answer that the role does not exist. Bounded; raises
    RuntimeError naming each path that did not demand a password.
    """
    import secrets

    import psycopg

    probe = f"ava_hba_probe_{secrets.token_hex(4)}"
    # A path that never exists: no ~/.pgpass entry can answer the challenge.
    passfile = str(pg_socket_dir() / "no-passfile")
    deadline = time.monotonic() + _HBA_PROOF_TIMEOUT_S
    while True:
        unproven: list[str] = []
        for host in (dial_host, str(pg_socket_dir())):
            try:
                psycopg.connect(
                    host=host,
                    port=pg_port,
                    user=probe,
                    password="",
                    passfile=passfile,
                    dbname="postgres",
                    connect_timeout=3,
                ).close()
                unproven.append(f"{host}: admitted without a password")
            except psycopg.OperationalError as exc:
                if not any(marker in str(exc) for marker in _PASSWORD_DEMANDED):
                    unproven.append(f"{host}: {str(exc).strip().splitlines()[-1]}")
        if not unproven:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "postgres does not enforce password authentication after its pg_hba "
                "rewrite: " + "; ".join(unproven)
            )
        time.sleep(0.1)


def _initdb(target: Path) -> None:
    """Fresh initdb at `target` with the OS user as the bootstrap superuser
    (mirrors throwaway_postgres / the brew install convention). The `trust`
    default is never served: `_start_pg` writes the always-auth hba first."""
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            _pg_bin("initdb"),
            "-D",
            str(target),
            "-U",
            getpass.getuser(),
            "-A",
            "trust",
            "--encoding=UTF8",
            "--locale=C",
        ],
        check=True,
        capture_output=True,
    )


def _ensure_pg_data() -> Path:
    """Ensure this cluster's pg data dir exists and is initialized, via the cached
    template (initdb once host-wide, then copy). Returns the data dir."""
    data = pg_data_dir()
    if (data / "PG_VERSION").exists():
        return data
    template = _pg_template_dir()
    if not (template / "PG_VERSION").exists():
        _initdb(template)
    shutil.copytree(template, data)
    return data


# Thin shells over the shared admin-plane dial (moved to base.cluster.dataplane.pg_admin,
# 2026-08-31 — services layer reaches it without importing up into cli). The
# names stay for existing cli callers and their monkeypatches.
def pg_socket_dir(socket_root: Path | None = None) -> Path:
    """Thin shell over base.cluster.dataplane.pg_admin.pg_socket_dir — the home resolution stays
    in the cli namespace so tests steering `ava_home` keep steering this probe."""
    return _base_pg_socket_dir(socket_root, home=ava_home())


def pg_admin_url(pg_port: int) -> str:
    """Thin shell — the admin URL lives in base.cluster.dataplane.pg_admin (services import it
    from there); this keeps the cli-side monkeypatch surface stable."""
    return _base_pg_admin_url(pg_port)


def pg_running(pg_port: int, host: str = "127.0.0.1") -> bool:
    try:
        out = subprocess.run(
            [_pg_bin("pg_isready"), "-h", host, "-p", str(pg_port)],
            capture_output=True,
            check=False,
            timeout=_PG_PROBE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return False
    return out.returncode == 0


def _start_pg(
    pg_port: int,
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
    machine: MachineIdentity,
) -> int:
    if retained_children is None:
        raise ValueError("PostgreSQL launch requires its caller-owned child retention")
    config = ConfigBoot()

    def path_reader() -> Path | None:
        if not config.prepared:
            config.read_process_environment()
        return config.view.walg.walg_config_file

    owner = ownership.require_postgres(pg_data_dir(), pg_port, required=False)
    data = _ensure_pg_data()
    (data / "pg_ident.conf").write_text(_pg_ident_body())
    (data / "pg_hba.conf").write_text(_pg_hba_body(machine=machine))
    dial_host = pg_dial_host()
    # The wait applies only when this cluster binds a non-loopback reachable address
    # (`port_preflight.bind_addrs`); a loopback host returns immediately.
    if owner is None and not wait_for_reachable_bind(machine=machine):
        print(
            f"  ✗ reachable bind address {machine.host()!r} is not assigned to any "
            f"local interface after {int(BIND_WAIT_TIMEOUT_S)}s — postgres cannot bind "
            f"it. On reboot this means the private network has not come "
            f"up yet; retry `ava start` once it is.",
            file=sys.stderr,
        )
        return 1
    listen = ",".join(port_preflight.bind_addrs(machine=machine))
    owned_postgres.start(
        data,
        pg_port,
        [
            _pg_bin("postgres"),
            "-D",
            str(data),
            "-p",
            str(pg_port),
            "-c",
            f"listen_addresses={listen}",
            "-c",
            f"unix_socket_directories={pg_socket_dir()}",
            "-c",
            "unix_socket_permissions=0700",
            "-c",
            f"max_connections={_PG_MAX_CONNECTIONS}",
            *shlex.split(pg_tz_args()),
            *shlex.split(pg_shm_args()),
            *archive_pg_args(path_reader=path_reader),
        ],
        pg_start_env(),
        ready=lambda: pg_running(pg_port, dial_host),
        timeout=_PG_START_TIMEOUT_S,
        expected=owner,
        retained_children=retained_children,
    )
    ownership.require_postgres(data, pg_port)
    require_authenticated_hba(pg_port, dial_host)
    print(f"  ✓ postgres ready ({dial_host}:{pg_port}, password authentication enforced)")
    warn_archive_inactive(path_reader=path_reader)
    return 0


def redis_cli_env(redis_admin_password: str) -> dict[str, str]:
    """Child env that authenticates `redis-cli` without putting the secret on its
    command line — `-a <secret>` is argv, which `ps` shows to any local user
    (issue #974). `$REDISCLI_AUTH` is redis-cli's own answer to exactly this
    (it is also why the tool prints no auth warning for it). The owned Redis
    always requires its admin password, so an empty one is refused.

    Raises:
        ValueError: `redis_admin_password` is empty."""
    if not redis_admin_password:
        raise ValueError("the owned Redis always requires its admin password")
    return inherited_process_env({"REDISCLI_AUTH": redis_admin_password})


def _redis_running(redis_port: int, redis_admin_password: str, host: str = "127.0.0.1") -> bool:
    out = subprocess.run(
        [redis_cli_bin(), "-h", host, "-p", str(redis_port), "ping"],
        env=redis_cli_env(redis_admin_password),
        capture_output=True,
        text=True,
        check=False,
    )
    return "PONG" in (out.stdout or "")


# Redis default persistence schedule (900s/1 change, 300s/10, 60s/10000).
_REDIS_SAVE_SCHEDULE = ("900 1", "300 10", "60 10000")


def _write_redis_conf(data: Path, redis_admin_password: str) -> Path:
    """Write the cluster's RDB save schedule and `requirepass` into a 0600
    `redis.conf` and return it.

    `redis-server --requirepass <password>` would carry the Redis admin password on argv.
    redis overwrites its own process title moments later, so `ps` only sees it
    during startup — but a startup window is still a window, and the conf file is
    the mechanism redis itself documents. The save schedule lives here too: a
    restart without persistence wipes the frozen caches and re-causes the
    all-at-once rebuild stampede (2026-08-29 #2004 amplifier, task #2027), so it
    must survive every render, not ride a runtime CONFIG SET. Everything else
    stays a flag: redis applies flags after the config file, so the per-cluster
    port/bind/dir still win (and a stale conf from an older start cannot pin them).

    Every render carries `requirepass`: Redis always authenticates.

    Raises:
        ValueError: `redis_admin_password` is empty."""
    if not redis_admin_password:
        raise ValueError("redis.conf requires the Redis admin password (requirepass)")
    conf = data / "redis.conf"
    content = b"".join(f"save {spec}\n".encode() for spec in _REDIS_SAVE_SCHEDULE)
    escaped = redis_admin_password.replace("\\", "\\\\").replace('"', '\\"')
    content += f'requirepass "{escaped}"\n'.encode()
    write_private_bytes(conf, content)
    return conf


def start_redis(
    redis_port: int,
    redis_admin_password: str,
    runtime_password: str,
    identity: str,
    *,
    machine: MachineIdentity,
) -> int:
    if not (redis_admin_password and runtime_password):
        raise ValueError("Redis always authenticates: its admin and runtime passwords are required")
    dial_host = redis_dial_host()
    if _redis_running(redis_port, redis_admin_password, dial_host):
        # Re-affirm the ACL user on every start (survives a restart that drops
        # the in-memory ACL).
        result = _ensure_redis_acl(
            redis_port, redis_admin_password, runtime_password, identity, dial_host
        )
        if result == 0:
            _write_redis_conf(ownership.redis_data_dir(), redis_admin_password)
            print(f"  ✓ redis already running ({dial_host}:{redis_port})")
        return result
    port_preflight.require_listener(None, redis_port, required=False)
    bind_addrs = ["127.0.0.1"] if is_macos() else port_preflight.bind_addrs(machine=machine)
    if not is_macos() and not wait_for_reachable_bind(machine=machine):
        return 1
    data = ownership.redis_data_dir()
    data.mkdir(parents=True, exist_ok=True)
    args = [
        _redis_server_bin(),
        str(_write_redis_conf(data, redis_admin_password)),  # requirepass — see the helper
        "--daemonize",
        "yes",
        "--port",
        str(redis_port),
        "--bind",
        *bind_addrs,
        "--protected-mode",
        "no",
        "--dir",
        str(data),
        "--logfile",
        str(data / "redis.log"),
    ]
    result = subprocess.run(
        args, check=False, capture_output=True, text=True, env=daemon_process_env()
    )
    if result.returncode != 0:
        print(
            f"  ✗ redis-server failed (rc={result.returncode}): {result.stderr.strip()}",
            file=sys.stderr,
        )
        return 1
    # redis-server --daemonize returns immediately; wait for the port to answer.
    for _ in range(60):
        if _redis_running(redis_port, redis_admin_password, dial_host):
            break
        time.sleep(0.1)
    else:
        print(f"  ✗ redis did not become ready on :{redis_port}", file=sys.stderr)
        return 1
    # The runtime URLs carry the identity as username, and redis-py AUTHes as
    # that user, so a missing user would WRONGPASS forever and the redis wake
    # bus would never deliver to agents.
    result = _ensure_redis_acl(
        redis_port, redis_admin_password, runtime_password, identity, dial_host
    )
    if result == 0:
        print(f"  ✓ redis started ({dial_host}:{redis_port})")
    return result


def _ensure_redis_acl(
    redis_port: int,
    redis_admin_password: str,
    runtime_password: str,
    identity: str,
    redis_host: str,
) -> int:
    """Add the cluster's ACL user (the runtime identity, names-as-data — passed in
    by the caller, never derived from a name) on top of `requirepass`. requirepass
    authenticates the `default` admin user, which provisions the ACL user.
    Re-affirmed every start (survives a restart that drops the in-memory ACL).
    Both passwords are always present: Redis never runs without auth."""
    admin = f"redis://default:{redis_admin_password}@{redis_host}:{redis_port}"
    try:
        ensure_cluster_redis_acl(
            identity,
            redis_admin_url=admin,
            runtime_password=runtime_password,
            channel_prefix=settings.data_plane.events_channel.removesuffix(":events"),
            expected_data_dir=ownership.redis_data_dir(),
        )
    except Exception as exc:
        print(f"  ✗ ensuring cluster redis user failed: {exc}", file=sys.stderr)
        return 1
    return 0


def ensure_cluster_storage(
    *,
    pg_port: int,
    redis_port: int,
    redis_admin_password: str,
    redis_password: str,
    redis_user: str,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
    machine: MachineIdentity,
) -> int:
    """Ensure owned Postgres and Redis; schema, authority and pooler follow in
    start order (`cli.commands.data_plane.bringup`).

    Raises:
        ValueError: a required credential is missing, before any native effect.
            Redis always needs its admin and runtime passwords. Postgres needs
            none: its administrator is the OS user over the owner-only socket."""
    if not (redis_admin_password and redis_password):
        raise ValueError(
            "Redis always authenticates, but this home has no generated Redis credentials "
            "(AVA_REDIS_ADMIN_PASSWORD / AVA_REDIS_PASSWORD): it was born before Redis "
            "always authenticated and no conversion exists; re-birth it as a new home."
        )
    print(f"\n→ per-cluster data plane (pg :{pg_port}, redis :{redis_port})")
    if (rc := _start_pg(pg_port, retained_children=retained_children, machine=machine)) != 0:
        return rc
    if (
        rc := start_redis(
            redis_port,
            redis_admin_password,
            redis_password,
            redis_user,
            machine=machine,
        )
    ) != 0:
        return rc
    return 0


def redis_reachable(redis_port: int, redis_host: str = "127.0.0.1") -> bool:
    """True if this cluster's Redis answers PING as its `default` user (using
    the gateway-only Redis admin password). Used by `ava status`; degrades to
    False on any error, including a home without its admin password."""
    import redis as _redis
    from redis.exceptions import RedisError

    client = _redis.Redis(
        host=redis_host,
        port=redis_port,
        password=settings.data_plane.redis_admin_password,
        socket_connect_timeout=3,
    )
    try:
        return bool(client.ping())  # pyright: ignore[reportUnknownMemberType]
    except (RedisError, OSError):
        return False
    finally:
        client.close()
