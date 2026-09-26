"""Per-cluster native Postgres+Redis bring-up.

Every cluster (including `main`) runs its OWN Postgres and Redis under its
`$AVA_HOME` on its own allocated ports, so two co-located clusters share no data
plane and cannot reach into each other's database/channels — this is the only
data-plane path; there is no shared host instance.

Model (mirrors `shared.pg_tools.throwaway_postgres`, but persistent + authed):

- Postgres `initdb`s into `$AVA_HOME/pg` (cold) — cached through a host-level
  template dir so a new cluster / a test spins up by directory copy, not a fresh
  multi-second init — and is launched directly with durable native custody on its pg port.
  pg_hba: the local unix socket is `trust` (provisioning by the initdb superuser
  is passwordless), every TCP connection is `scram-sha-256` when the cluster has
  a bearer (a co-located cluster hitting the port still needs its role password).
  A no-secret
  cluster writes local + loopback `trust` only, and binds loopback alone. The
  owner role carries its independent password; provisioning connects over the socket.
- Redis runs `redis-server` on the cluster's redis port and ALWAYS authenticates,
  whatever the bearer: `requirepass` = the gateway-only Redis admin password, and
  the cluster's ACL user has its own runtime password. Both are minted at first
  start; a home without them is refused and converted once by
  `scripts/cutover_db_authority.py`. A no-secret cluster keeps the loopback-only
  bind. Data dir under `$AVA_HOME/redis`. The
  Redis admin password reaches redis through a 0600 `redis.conf` and reaches `redis-cli`
  through `$REDISCLI_AUTH` — never argv, which `ps` shows to any local user
  (issue #974).

The Postgres db/role and Redis ACL identifiers are independent URL data: callers
read each from its own `.env` URL (`db_identity()` / `redis_identity()`) and pass
`identity` / `redis_user` explicitly. First-start identity supplies the fixed
`DATA_PLANE_IDENTITY` for both; later starts preserve different existing names.

Bind posture — authenticated Postgres, PgBouncer and Linux Redis bind loopback
and this host's reachable address, never all interfaces. macOS Redis retains its
loopback-only workaround and host-level `com.ava.redis-bridge` relay (task #1469).
A no-secret cluster binds loopback only; no caller environment widens that posture.

POSIX only (macOS brew / Linux postgres + redis-server). Windows has no native
postgres/redis-server on PATH — a Windows per-cluster data plane is a follow-up;
`ensure_cluster_storage` fails fast there rather than mis-starting.
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
from urllib.parse import urlsplit

from shared.cluster import ensure_cluster_redis_acl, ownership
from shared.cluster import postgres as owned_postgres
from shared.config import settings
from shared.config.physical_backup import pitr_replication_hba_lines
from shared.machine import reachable_host
from shared.paths import ava_home
from shared.pg_admin import pg_admin_url as _shared_pg_admin_url
from shared.pg_admin import pg_socket_dir
from shared.pg_tools import (
    PG_BIN_LINUX,
    brew_prefix,
    is_macos,
    pg_shm_args,
    pg_start_env,
    pg_tool,
    pg_tz_args,
)
from shared.platform_backend import get_backend
from shared.private_storage import write_private_bytes
from shared.process_env import inherited_process_env
from shared.url_secret import url_host

_LOOPBACK_ALIASES = frozenset({"127.0.0.1", "::1", "localhost", "ip6-localhost"})


def _pg_dial_host() -> str:
    """The host this cluster's own Postgres is dialed at — read from this
    cluster's AVA_DB_URL (the URL is the one dial source every consumer uses;
    DataPlaneSettings rewrites a self-host URL to loopback before anything
    dials). The home identity and its URLs exist before native bring-up.
    External data-plane reachability is handled separately by `_data_plane`.
    """
    return url_host(settings.data_plane.db_url)


def _redis_dial_host() -> str:
    """The host this cluster's own Redis is dialed at — read from this
    cluster's AVA_REDIS_URL (see `_pg_dial_host`)."""
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
_BIND_WAIT_TIMEOUT_S = 60.0
_BIND_WAIT_INTERVAL_S = 2.0


def _pg_data_dir() -> Path:
    return ava_home() / "pg"


def _redis_data_dir() -> Path:
    return ava_home() / "redis"


def _pg_template_dir() -> Path:
    """Host-level cached `initdb` output, copied per cluster so a new instance is
    a directory copy rather than a multi-second init. Beside the cluster registry
    (independent of any one `$AVA_HOME`), so every co-located cluster shares it."""
    return Path(settings.general.cluster_registry).expanduser().parent / "pg-template-17"


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


def _redis_cli_bin() -> str:
    return _redis_bin("redis-cli")


def _pg_bin(name: str) -> str:
    return str(pg_tool(name)) if is_macos() else str(PG_BIN_LINUX / name)


def _bind_addrs(cluster_secret: str) -> list[str]:
    """Loopback plus this host's reachable address, de-duplicated (loopback alone
    when reachable resolves to localhost — the single-box default).

    A no-secret cluster binds LOOPBACK ONLY, whatever the reachable address says:
    with the Postgres data plane unauthenticated (no scram), a non-loopback bind
    would expose Postgres and its pooler to the LAN. Auth and reachability move
    together — an operator who wants a LAN-reachable Postgres data plane sets
    the cluster secret.

    `cluster_secret` is the CALLER-PASSED cluster secret (the same value the hba
    is written from and the pooler is configured with), never read from
    `settings` — a process that inherited a sibling cluster's
    AVA_CLUSTER_SECRET (a shell carrying a different home's environment) must not widen
    a no-secret cluster's bind posture to the LAN. The caller resolves the
    cluster's own secret (install: the decided secret; `ava start`: the
    authority-passed .env value)."""
    if not cluster_secret:
        return ["127.0.0.1"]
    host = reachable_host()
    out = ["127.0.0.1"]
    if host not in _LOOPBACK_ALIASES:
        out.append(host)
    return out


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


def _wait_for_reachable_bind() -> bool:
    """Block (bounded) until this host's non-loopback bind address is assigned to a
    local interface. Returns True immediately on a loopback-only (single-box) host —
    loopback is always present. Returns False on timeout so the caller fails fast
    with a clear message instead of pg_ctl dying on an un-bindable address."""
    host = reachable_host()
    if host in _LOOPBACK_ALIASES:
        return True
    deadline = time.monotonic() + _BIND_WAIT_TIMEOUT_S
    while True:
        if _addr_assigned(host):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_BIND_WAIT_INTERVAL_S)


def _pg_hba_body(cluster_secret: str) -> str:
    """The local socket is trust (the initdb superuser provisions passwordless);
    every TCP connection is scram (a co-located cluster needs its role password).
    Reachable/trusted ranges get the same scram treatment.

    A no-secret cluster has NO scram host lines at all: local trust + loopback
    trust only. There is no credential to check, and the data plane binds
    loopback alone (`_bind_addrs`), so no remote host can reach it anyway — the
    auth-less posture never extends past this machine.

    PITR adds loopback `replication` rows for its role (see
    `pitr_replication_hba_lines`): pg_basebackup's PHYSICAL replication
    connection matches only the literal `replication` keyword, never `all`.
    `cluster_secret` is the CALLER-PASSED cluster secret, never read from
    `settings`: install-time birth has no `.env` yet, so `settings` would see an
    inherited sibling secret (a prod-sourced shell) and write scram lines into a
    no-secret cluster's hba — the hba must always mirror the cluster the caller
    is actually birthing/bringing up (Task #1113). `trusted_cidrs` stays a
    settings read: it is a cluster-scope field the env-authority pass drops when
    undeclared, so it has no ambient-leak vector."""
    if not cluster_secret:
        return "local all all trust\nhost all all 127.0.0.1/32 trust\nhost all all ::1/128 trust\n"
    lines = [
        "local all all trust",
        "host all all 127.0.0.1/32 scram-sha-256",
        "host all all ::1/128 scram-sha-256",
    ]
    host = reachable_host()
    if host not in _LOOPBACK_ALIASES:
        lines.append(f"host all all {host}/32 scram-sha-256")
    for cidr in (c.strip() for c in settings.data_plane.trusted_cidrs.split(",") if c.strip()):
        lines.append(f"host all all {cidr} scram-sha-256")
    lines += pitr_replication_hba_lines()
    return "\n".join(lines) + "\n"


def _initdb(target: Path) -> None:
    """Fresh initdb at `target` with the OS user as the trust bootstrap superuser
    (mirrors throwaway_postgres / the brew install convention)."""
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
    data = _pg_data_dir()
    if (data / "PG_VERSION").exists():
        return data
    template = _pg_template_dir()
    if not (template / "PG_VERSION").exists():
        _initdb(template)
    shutil.copytree(template, data)
    return data


# Thin shells over the shared admin-plane dial (moved to shared.pg_admin,
# 2026-08-31 — services layer reaches it without importing up into cli). The
# underscore names stay for existing cli callers and their monkeypatches.
def _pg_socket_dir(socket_root: Path | None = None) -> Path:
    """Thin shell over shared.pg_admin.pg_socket_dir — the home resolution stays
    in the cli namespace so tests steering `ava_home` keep steering this probe."""
    return pg_socket_dir(socket_root, home=ava_home())


def pg_admin_url(pg_port: int) -> str:
    """Thin shell — the admin URL lives in shared.pg_admin (services import it
    from there); this keeps the cli-side monkeypatch surface stable."""
    return _shared_pg_admin_url(pg_port)


def _pg_running(pg_port: int, host: str = "127.0.0.1") -> bool:
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


def _start_pg(pg_port: int, cluster_secret: str) -> int:
    owner = ownership.require_postgres(_pg_data_dir(), pg_port, required=False)
    data = _ensure_pg_data()
    (data / "pg_hba.conf").write_text(_pg_hba_body(cluster_secret))
    dial_host = _pg_dial_host()
    # Same gate as the pgbouncer path (task #1303, PR #47 P2): a no-secret
    # cluster binds loopback alone (`_bind_addrs`), so a stray ambient
    # AVA_MACHINE_HOST must not hold a warm start hostage for a bind that never
    # happens. Wait only when this cluster actually binds the reachable address.
    if (
        owner is None
        and _bind_addrs(cluster_secret) != ["127.0.0.1"]
        and not _wait_for_reachable_bind()
    ):
        print(
            f"  ✗ reachable bind address {reachable_host()!r} is not assigned to any "
            f"local interface after {int(_BIND_WAIT_TIMEOUT_S)}s — postgres cannot bind "
            f"it. On reboot this means the private network has not come "
            f"up yet; retry `ava start` once it is.",
            file=sys.stderr,
        )
        return 1
    listen = ",".join(_bind_addrs(cluster_secret))
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
            f"unix_socket_directories={_pg_socket_dir()}",
            "-c",
            "unix_socket_permissions=0700",
            "-c",
            f"max_connections={_PG_MAX_CONNECTIONS}",
            *shlex.split(pg_tz_args()),
            *shlex.split(pg_shm_args()),
        ],
        pg_start_env(),
        ready=lambda: _pg_running(pg_port, dial_host),
        timeout=_PG_START_TIMEOUT_S,
        expected=owner,
    )
    ownership.require_postgres(data, pg_port)
    print(f"  ✓ postgres ready ({dial_host}:{pg_port})")
    return 0


def _redis_cli_env(redis_admin_password: str) -> dict[str, str]:
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
        [_redis_cli_bin(), "-h", host, "-p", str(redis_port), "ping"],
        env=_redis_cli_env(redis_admin_password),
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

    Every render carries `requirepass`: Redis always authenticates, including a
    no-secret cluster (which differs only in its loopback-only bind).

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


def _start_redis(
    redis_port: int,
    redis_admin_password: str,
    runtime_password: str,
    cluster_secret: str,
    identity: str,
) -> int:
    if not (redis_admin_password and runtime_password):
        raise ValueError("Redis always authenticates: its admin and runtime passwords are required")
    dial_host = _redis_dial_host()
    if _redis_running(redis_port, redis_admin_password, dial_host):
        # Re-affirm the ACL user on every start (survives a restart that drops
        # the in-memory ACL).
        result = _ensure_redis_acl(
            redis_port, redis_admin_password, runtime_password, identity, dial_host
        )
        if result == 0:
            _write_redis_conf(_redis_data_dir(), redis_admin_password)
            print(f"  ✓ redis already running ({dial_host}:{redis_port})")
        return result
    ownership.require_listener(None, redis_port, required=False)
    bind_addrs = ["127.0.0.1"] if is_macos() else _bind_addrs(cluster_secret)
    if not is_macos() and cluster_secret and not _wait_for_reachable_bind():
        return 1
    data = _redis_data_dir()
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
    result = subprocess.run(args, check=False, capture_output=True, text=True)
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
            expected_data_dir=_redis_data_dir(),
        )
    except Exception as exc:
        print(f"  ✗ ensuring cluster redis user failed: {exc}", file=sys.stderr)
        return 1
    return 0


def _start_pgbouncer(
    *,
    pg_port: int,
    listen_port: int,
    cluster_secret: str,
    db_admin_password: str,
    identity: str,
    runner_password: str | None = None,
) -> int:
    """Bring up (or reload) this cluster's PgBouncer pooler in front of the local
    Postgres. Only reached when AVA_PGBOUNCER_ENABLED; the db name and role (the
    pooled front door's scram identity) are the caller-passed data-plane
    identity (names-as-data — db and role share the identifier).

    `runner_password` (the gateway .env AVA_RUNNER_DB_PASSWORD) is threaded at
    first-start identity and resolved from the
    home's .env file on every later bring-up (the userlist carries an
    `ava_runner` entry only once the cluster has a runner credential)."""
    from cli.commands._pgbouncer import ensure_pgbouncer, runner_password_from_env

    return ensure_pgbouncer(
        pg_port=pg_port,
        listen_port=listen_port,
        db_name=identity,
        role=identity,
        cluster_secret=cluster_secret,
        db_admin_password=db_admin_password,
        runner_password=runner_password
        if runner_password is not None
        else runner_password_from_env(),
    )


def ensure_cluster_storage(
    *,
    pg_port: int,
    redis_port: int,
    cluster_secret: str,
    db_admin_password: str = "",
    redis_admin_password: str,
    redis_password: str,
    redis_user: str,
) -> int:
    """Ensure owned Postgres and Redis; schema and pooler follow in start order.

    Raises:
        ValueError: a required credential is missing, before any native effect.
            Redis always needs its admin and runtime passwords; Postgres needs its
            owner password when the cluster has a bearer."""
    if not get_backend().supports_data_plane():
        # Naming the supported topology, not the missing feature: this used to
        # read as an unfinished TODO ("Follow-up: bundle or Docker-host ..."),
        # which tells an operator nothing about what to do with the host in
        # front of them. Windows is the only platform that lands here.
        print(
            "  ✗ this platform cannot host a per-cluster data plane (no native redis),\n"
            "    so it cannot carry the `gateway` capability. Run the gateway on\n"
            "    macOS/Linux — on Windows hardware, inside WSL2 — and join this host\n"
            "    to it as an agent-runner:\n"
            "      set AVA_CLUSTER_SECRET from a non-echoing prompt, then run:\n"
            "      ava start --serve-agent-runner --no-serve-gateway --gateway-url <url> --machine-name <name> \\\n"
            "                 --machine-host <this-host-private-ip>\n"
            "    What a gateway would additionally require: future/infra/windows-gateway.md",
            file=sys.stderr,
        )
        return 1
    if not (redis_admin_password and redis_password):
        raise ValueError(
            "Redis always authenticates, but this home has no generated Redis credentials "
            "(AVA_REDIS_ADMIN_PASSWORD / AVA_REDIS_PASSWORD). Convert the existing home "
            "once, with its application stopped (`ava stop --keep-infra`): "
            f"`.venv/bin/python scripts/cutover_db_authority.py --home {ava_home()} "
            "--execute` (see conventions/data-plane-secret-split.md)."
        )
    if cluster_secret and not db_admin_password:
        raise ValueError("authenticated storage requires an explicit owner credential")
    print(f"\n→ per-cluster data plane (pg :{pg_port}, redis :{redis_port})")
    if (rc := _start_pg(pg_port, cluster_secret)) != 0:
        return rc
    if (
        rc := _start_redis(
            redis_port, redis_admin_password, redis_password, cluster_secret, redis_user
        )
    ) != 0:
        return rc
    return 0


def _redis_reachable(redis_port: int, redis_host: str = "127.0.0.1") -> bool:
    """True if this cluster's Redis answers PING as its `default` user (using
    the gateway-only Redis admin password). Used by `ava status`; degrades to
    False on any error, including a home without its admin password."""
    import redis as _redis

    client = _redis.Redis(
        host=redis_host,
        port=redis_port,
        password=settings.data_plane.redis_admin_password,
        socket_connect_timeout=3,
    )
    try:
        return bool(client.ping())  # pyright: ignore[reportUnknownMemberType]
    except Exception:
        return False
    finally:
        client.close()


def print_data_plane_status() -> None:
    """Print this cluster's own pg/redis reachability for `ava status`.

    Postgres is probed in two stages: `pg_isready` (the server accepts connections)
    then an authenticated `SELECT 1` over the cluster's POOLED front door
    (`connect()` — PgBouncer when enabled, the direct URL when not; the same swap
    every consumer dials). A server that is up but whose client credential has
    drifted from the owner password reports `✗ ... connect failed` rather than a
    false `✓` — that drift silently fails every DB-backed endpoint while pg_isready
    alone (loopback `trust`) stays green. Redis pings with its admin password. Both
    ports come from this cluster's own db_url / redis_url (its per-cluster
    instance).

    The probe goes POOLED, never `direct=True` (user ruling 2026-08: every consumer
    sits behind PgBouncer): the pooled `SELECT 1` proves the path consumers
    actually use — client scram against the pooler's userlist (rewritten every
    start from the owner password) plus the pooler → Postgres trust-socket hop.
    PG-side scram verifier drift is unreachable through the pooler by design (the
    backend hop carries no credential), so a direct probe would test a path no
    consumer dials. With PgBouncer disabled `pooled_db_url == db_url` and the probe
    is direct anyway."""
    import shared.db

    if settings.data_plane.is_remote:
        # A remote-managed plane has no local instance to manage — probe the
        # URLs themselves (the switch) and skip the local pooler line.
        from cli.commands._data_plane import remote_pg_reachable, remote_redis_reachable

        print("  · data plane remote-managed — probing the URLs (no local instance)")
        pg_ok, pg_line = remote_pg_reachable()
        print(f"  {'✓' if pg_ok else '✗'} {pg_line}")
        redis_ok, redis_line = remote_redis_reachable()
        print(f"  {'✓' if redis_ok else '✗'} {redis_line}")
        return

    # Both probes dial the host their own URL names (self-host URLs are
    # loopback-rewritten by DataPlaneSettings). The remote branch above returns
    # before this local probe runs.
    pg_host = _pg_dial_host()
    redis_host = _redis_dial_host()
    pg_port = urlsplit(settings.data_plane.db_url).port or 5432
    redis_port = urlsplit(settings.data_plane.redis_url).port or 6379
    if not _pg_running(pg_port, pg_host):
        print(f"  ✗ postgres ({pg_host}:{pg_port}) unreachable")
    else:
        try:
            with shared.db.connect() as conn:  # pooled front door (PgBouncer when enabled)
                conn.execute("select 1")
            print(f"  ✓ postgres ({pg_host}:{pg_port})")
        except Exception as exc:
            lines = [ln.strip() for ln in str(exc).splitlines() if ln.strip()]
            detail = lines[-1] if lines else "connection failed"
            print(f"  ✗ postgres ({pg_host}:{pg_port}) reachable but connect failed: {detail}")
    print(
        f"  {'✓' if _redis_reachable(redis_port, redis_host) else '✗'} "
        f"redis ({redis_host}:{redis_port})"
    )
    if settings.data_plane.pgbouncer_enabled:
        from cli.commands._pgbouncer import pgbouncer_reachable
        from shared.cluster import db_identity, get_record, record_pgbouncer_port

        # The pooler LISTENS on the registry-derived port (`ensure_cluster_storage`
        # is called with `record_pgbouncer_port(rec)`), so probe/display that same
        # port — the pooler port is a registry fact only (AVA_PGBOUNCER_PORT is no
        # longer materialized in .env; AVA_DB_URL carries the pooler port when
        # enabled). No registry record (an unusual host) means the pooler port is
        # unknowable — say so instead of printing a false `:0`. The db/role
        # identity is read from this cluster's own db_url (names-as-data).
        rec = get_record(ava_home())
        if rec is None:
            print("  - pgbouncer: no registry record — cannot resolve its port")
        else:
            port = record_pgbouncer_port(rec)
            identity = db_identity()
            ok = pgbouncer_reachable(
                port,
                identity,
                identity,
                settings.data_plane.db_admin_password,
            )
            print(f"  {'✓' if ok else '✗'} pgbouncer (127.0.0.1:{port}, transaction pooling)")


def _redis_port() -> int | None:
    """This cluster's redis port from settings.data_plane.redis_url, or None if not
    resolvable (no instance to stop). Deliberately no credential: the URL carries
    the restricted runtime ACL password, which is never admin authority — admin
    effects use `settings.data_plane.redis_admin_password`."""
    return urlsplit(settings.data_plane.redis_url).port or None


def stop_cluster_instance() -> int:
    """Stop this cluster's own Postgres + Redis (data preserved on disk). The
    counterpart of ensure_cluster_storage for `ava stop` / `ava cluster down` of a
    cluster running its own instance. Best-effort: a not-running instance is a
    no-op success only after native custody closes."""
    if settings.data_plane.is_remote:
        # A remote-managed plane has no local instance to stop — nothing on this
        # box to tear down, and the provider owns the service lifecycle. But a
        # cluster that SWITCHED local→remote may still have its old local
        # instance running; warn so it is torn down deliberately.
        from cli.commands._data_plane import remote_plane_host, warn_orphaned_local_instance

        print(f"\n→ data plane remote-managed ({remote_plane_host()}) — nothing to stop locally")
        warn_orphaned_local_instance()
        return 0
    data = _pg_data_dir()
    port = _redis_port()
    # Resolve the admin credential before any stop effect: the runtime URL's
    # ACL user can neither AUTH as `default` nor SHUTDOWN (`-@dangerous`).
    redis_env = None if port is None else _redis_cli_env(settings.data_plane.redis_admin_password)
    print("\n→ stopping per-cluster data plane")
    # Stop the pooler first (best-effort, no-op if it was never enabled) so clients
    # are disconnected before Postgres goes down.
    from cli.commands._pgbouncer import stop_pgbouncer

    stop_pgbouncer()
    owned_postgres.stop(data)
    print("  ✓ postgres stopped")
    if port is not None:
        subprocess.run(
            [_redis_cli_bin(), "-p", str(port), "shutdown", "nosave"],
            env=redis_env,
            check=False,
            capture_output=True,
        )
        print("  ✓ redis stopped")
    return 0
