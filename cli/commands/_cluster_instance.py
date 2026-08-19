"""Per-cluster native Postgres+Redis bring-up.

Every cluster (including `main`) runs its OWN Postgres and Redis under its
`$AVA_HOME` on its own allocated ports, so two co-located clusters share no data
plane and cannot reach into each other's database/channels — this is the only
data-plane path; there is no shared host instance.

Model (mirrors `shared.pg_tools.throwaway_postgres`, but persistent + authed):

- Postgres `initdb`s into `$AVA_HOME/pg` (cold) — cached through a host-level
  template dir so a new cluster / a test spins up by directory copy, not a fresh
  multi-second init — and is started via `pg_ctl` on the cluster's pg port.
  pg_hba: the local unix socket is `trust` (provisioning by the initdb superuser
  is passwordless), every TCP connection is `scram-sha-256` when the cluster has
  a secret (a co-located cluster hitting the port still needs it). A no-secret
  cluster writes local + loopback `trust` only, and binds loopback alone. The
  runtime role carries the secret; provisioning connects over the socket.
- Redis runs `redis-server` on the cluster's redis port with `requirepass` = the
  cluster secret when one is set; the cluster's ACL user is added on top (the
  runtime identity). A no-secret cluster runs without requirepass and without an
  ACL user, on the loopback-only bind. Data dir under `$AVA_HOME/redis`. The
  secret reaches redis through a 0600 `redis.conf` and reaches `redis-cli`
  through `$REDISCLI_AUTH` — never argv, which `ps` shows to any local user
  (issue #974).

The db / role / ACL identifier is names-as-data: callers read it from the
cluster's own `.env` URLs (`shared.cluster.identity_from_url`) — or pass the
fixed `DATA_PLANE_IDENTITY` at install-time birth — and thread it in via
`ensure_cluster_instance(identity=...)`; nothing here derives it from a name.

Bind posture — loopback + this host's reachable address, never all interfaces —
only the port is per-cluster. A single-box cluster is reachable only at loopback,
so the bind collapses to loopback alone.

POSIX only (macOS brew / Linux pg_ctl + redis-server). Windows has no native
pg_ctl/redis-server on PATH — a Windows per-cluster data plane is a follow-up;
`ensure_cluster_instance` fails fast there rather than mis-starting.
"""

from __future__ import annotations

import getpass
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from shared.cluster import ensure_cluster_redis_acl
from shared.config import settings
from shared.machine import reachable_host
from shared.paths import ava_home
from shared.pg_tools import PG_BIN_LINUX, brew_prefix, is_macos, pg_shm_args, pg_tool, pg_tz_args
from shared.platform_backend import get_backend

_LOOPBACK_ALIASES = frozenset({"127.0.0.1", "::1", "localhost", "ip6-localhost"})

# A single cluster's own pg needs far less than a per-host shared budget. `main`
# on its own instance carries the whole prod fleet, so keep enough headroom (the
# gateway + long-running services alone hold ~25, then ~3-4 per agent).
_PG_MAX_CONNECTIONS = 500

# Bounded wait for this host's non-loopback bind address (AVA_MACHINE_HOST) to
# appear on a local interface before binding pg/redis to it. On reboot brew /
# launchd can start `ava` before the private network has assigned the address,
# so binding it fails and the whole autostart dies.
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


def _redis_server_bin() -> str:
    return str(brew_prefix("redis") / "bin" / "redis-server") if is_macos() else "redis-server"


def _redis_cli_bin() -> str:
    return str(brew_prefix("redis") / "bin" / "redis-cli") if is_macos() else "redis-cli"


def _pg_bin(name: str) -> str:
    return str(pg_tool(name)) if is_macos() else str(PG_BIN_LINUX / name)


def _bind_addrs(cluster_secret: str) -> list[str]:
    """Loopback plus this host's reachable address, de-duplicated (loopback alone
    when reachable resolves to localhost — the single-box default).

    A no-secret cluster binds LOOPBACK ONLY, whatever the reachable address says:
    with the data plane unauthenticated (no scram / no requirepass), a
    non-loopback bind would expose pg/redis to the LAN. Auth and reachability
    move together — an operator who wants a LAN-reachable data plane sets the
    cluster secret.

    `cluster_secret` is the CALLER-PASSED cluster secret (the same value the hba
    is written from and the pooler/redis are configured with), never read from
    `settings` — a process that inherited a sibling cluster's
    AVA_CLUSTER_SECRET (a prod-sourced shell running an install) must not widen
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
    """True if `addr` is currently assigned to a local interface (bindable). A pg /
    redis listener can only bind an address the kernel has on an interface; before
    the private-network interface comes up, binding its address fails with
    EADDRNOTAVAIL. Probing with a throwaway bind is the portable check."""
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
    every TCP connection is scram (a co-located cluster needs the cluster secret).
    Reachable/trusted ranges get the same scram treatment.

    A no-secret cluster has NO scram host lines at all: local trust + loopback
    trust only. There is no credential to check, and the data plane binds
    loopback alone (`_bind_addrs`), so no remote host can reach it anyway — the
    auth-less posture never extends past this machine.

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


def _pg_socket_dir() -> Path:
    """A SHORT, cluster-unique socket directory. The Postgres socket path
    (`<dir>/.s.PGSQL.<port>`) is capped at 103 bytes, so it cannot live under a
    deep `$AVA_HOME` / pytest-tmp data dir — a short `/tmp/ava-pg-<home-slug>`
    (keyed on the cluster home path, never a name) stays well under the cap. The
    socket only serves local provisioning (the runtime connects over TCP); 0700
    keeps it owner-only."""
    from shared.cluster import home_slug

    d = Path("/tmp") / f"ava-pg-{home_slug(ava_home())}"  # noqa: S108 — short socket path, owner-only below
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def _live_pg_socket_dir(pg_port: int, probe_root: Path = Path("/tmp")) -> Path:  # noqa: S108 — the OS-fixed short socket root
    """The socket dir the RUNNING pg instance on `pg_port` actually listens on.

    Normally the canonical `_pg_socket_dir()`. A pg started by pre-path-only code
    still listens under the old name-keyed `/tmp/ava-pg-<cluster>` until its next
    restart, so the admin dial probes every `<probe_root>/ava-pg-*` dir for a live
    `.s.PGSQL.<pg_port>` socket — the port is this cluster's own allocated one,
    so a match is unambiguous (data, not a name). Falls back to the canonical dir
    when nothing is listening yet (fresh birth: the socket appears when
    `_start_pg` starts pg there). `probe_root` is /tmp in production (where the
    short socket dirs live); tests inject a scratch root."""
    canonical = _pg_socket_dir()
    if (canonical / f".s.PGSQL.{pg_port}").exists():
        return canonical
    for d in probe_root.glob("ava-pg-*"):
        if (d / f".s.PGSQL.{pg_port}").exists():
            return d
    return canonical


def _pg_running(pg_port: int) -> bool:
    out = subprocess.run(
        [_pg_bin("pg_isready"), "-h", "127.0.0.1", "-p", str(pg_port)],
        capture_output=True,
        check=False,
    )
    return out.returncode == 0


def _start_pg(pg_port: int, cluster_secret: str) -> int:
    data = _ensure_pg_data()
    (data / "pg_hba.conf").write_text(_pg_hba_body(cluster_secret))
    if _pg_running(pg_port):
        print(f"  ✓ postgres already running (127.0.0.1:{pg_port})")
        # The hba file was just rewritten; a running server keeps the copy it
        # loaded at start until reloaded. Install-time birth starts pg BEFORE
        # the cluster's .env exists (no secret yet -> trust hba), and the first
        # `ava start` then rewrites it with the real posture — without a reload
        # the server keeps serving the STALE hba: a no-secret cluster born from
        # a prod-sourced shell served scram keyed to a foreign secret, and a
        # secret cluster kept an unenforced trust hba on TCP (Task #1113: the
        # first-start migration failed `fe_sendauth: no password supplied`).
        # pg_ctl reload is a SIGHUP — a no-op when the content is unchanged.
        result = subprocess.run(
            [_pg_bin("pg_ctl"), "-D", str(data), "reload"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(
                f"  ✗ pg_ctl reload failed (rc={result.returncode}); the new "
                f"pg_hba.conf is not in effect until a reload: "
                f"{result.stderr.strip()}",
                file=sys.stderr,
            )
            return 1
        print("    pg_hba.conf reloaded into the running server")
        return 0
    if not _wait_for_reachable_bind():
        print(
            f"  ✗ reachable bind address {reachable_host()!r} is not assigned to any "
            f"local interface after {int(_BIND_WAIT_TIMEOUT_S)}s — postgres cannot bind "
            f"it. On reboot this means the private network has not come "
            f"up yet; retry `ava start` once it is.",
            file=sys.stderr,
        )
        return 1
    listen = ",".join(_bind_addrs(cluster_secret))
    result = subprocess.run(
        [
            _pg_bin("pg_ctl"),
            "-D",
            str(data),
            "-l",
            str(data / "pg.log"),
            "-w",
            "-t",
            "60",
            "start",
            "-o",
            f"-p {pg_port} -c listen_addresses={listen} "
            f"-c unix_socket_directories={_pg_socket_dir()} "
            f"-c max_connections={_PG_MAX_CONNECTIONS} "
            f"{pg_tz_args()} {pg_shm_args()}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(
            f"  ✗ pg_ctl start failed (rc={result.returncode}); see {data / 'pg.log'}\n"
            f"    {result.stderr.strip()}",
            file=sys.stderr,
        )
        return 1
    print(f"  ✓ postgres started (127.0.0.1:{pg_port})")
    return 0


def pg_admin_url(pg_port: int) -> str:
    """The provisioning admin connection for this cluster's instance: the initdb
    superuser over the local unix socket (trust), so provisioning is passwordless.
    psycopg reads `host=<socket-dir>` + `port` from the query string. Dials the
    socket dir the running instance actually listens on (`_live_pg_socket_dir`),
    so an admin call keeps working while a pre-cutover pg is still up on the old
    name-keyed dir."""
    return (
        f"postgresql://{getpass.getuser()}@/postgres"
        f"?host={_live_pg_socket_dir(pg_port)}&port={pg_port}"
    )


def _redis_cli_env(cluster_secret: str) -> dict[str, str]:
    """Child env that authenticates `redis-cli` without putting the secret on its
    command line — `-a <secret>` is argv, which `ps` shows to any local user
    (issue #974). `$REDISCLI_AUTH` is redis-cli's own answer to exactly this
    (it is also why the tool prints no auth warning for it). A no-secret cluster
    sets nothing — `REDISCLI_AUTH=""` would make redis-cli send an AUTH the
    server has no password for."""
    if not cluster_secret:
        return dict(os.environ)
    return {**os.environ, "REDISCLI_AUTH": cluster_secret}


def _redis_running(redis_port: int, cluster_secret: str) -> bool:
    out = subprocess.run(
        [_redis_cli_bin(), "-h", "127.0.0.1", "-p", str(redis_port), "ping"],
        env=_redis_cli_env(cluster_secret),
        capture_output=True,
        text=True,
        check=False,
    )
    return "PONG" in (out.stdout or "")


def _write_redis_conf(data: Path, cluster_secret: str) -> Path:
    """Write the cluster's `requirepass` into a 0600 `redis.conf` and return it.

    `redis-server --requirepass <secret>` would carry the cluster secret on argv.
    redis overwrites its own process title moments later, so `ps` only sees it
    during startup — but a startup window is still a window, and the conf file is
    the mechanism redis itself documents. Everything else stays a flag: redis
    applies flags after the config file, so the per-cluster port/bind/dir still
    win (and a stale conf from an older start cannot pin them).

    A no-secret cluster writes NO requirepass line — redis then serves without
    auth, on the loopback-only bind (`_bind_addrs`) that the same secret gates."""
    conf = data / "redis.conf"
    fd = os.open(conf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        # The secret's charset is constrained to URL-safe unreserved characters
        # (see DataPlaneSettings.cluster_secret), so it needs no escaping — but
        # quote it anyway so a future widening cannot silently truncate it.
        if cluster_secret:
            handle.write(f'requirepass "{cluster_secret}"\n')
    return conf


def _start_redis(redis_port: int, cluster_secret: str, identity: str) -> int:
    if _redis_running(redis_port, cluster_secret):
        print(f"  ✓ redis already running (127.0.0.1:{redis_port})")
        # Re-affirm the ACL user on every start (survives a restart that drops
        # the in-memory ACL) — including no-secret clusters, whose identity
        # user is created with `nopass` (see _ensure_redis_acl).
        return _ensure_redis_acl(redis_port, cluster_secret, identity)
    # Same boot race as Postgres: binding the reachable address before it is on an
    # interface fails (redis exits outright — loud, but it takes the whole start down
    # pointlessly). Wait bounded for it first (task #1288) — but only when this
    # cluster actually binds it: a no-secret cluster binds loopback alone
    # (`_bind_addrs`), so a stray AVA_MACHINE_HOST must not hold a warm start hostage.
    if _bind_addrs(cluster_secret) != ["127.0.0.1"] and not _wait_for_reachable_bind():
        print(
            f"  ✗ reachable bind address {reachable_host()!r} is not assigned to any "
            f"local interface after {int(_BIND_WAIT_TIMEOUT_S)}s — redis cannot bind "
            f"it. On reboot this means the private network has not come "
            f"up yet; retry `ava start` once it is.",
            file=sys.stderr,
        )
        return 1
    data = _redis_data_dir()
    data.mkdir(parents=True, exist_ok=True)
    args = [
        _redis_server_bin(),
        str(_write_redis_conf(data, cluster_secret)),  # requirepass — see the helper
        "--daemonize",
        "yes",
        "--port",
        str(redis_port),
        "--bind",
        *_bind_addrs(cluster_secret),
        "--protected-mode",
        "no",
        "--dir",
        str(data),
        "--logfile",
        str(data / "redis.log"),
        "--save",
        "",
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
        if _redis_running(redis_port, cluster_secret):
            break
        time.sleep(0.1)
    else:
        print(f"  ✗ redis did not become ready on :{redis_port}", file=sys.stderr)
        return 1
    print(f"  ✓ redis started (127.0.0.1:{redis_port})")
    # A no-secret cluster keeps requirepass off, but the ACL user still exists
    # with `nopass` (see _ensure_redis_acl) — the runtime URLs carry the
    # identity as username, and redis-py AUTHes when a URL has a username, so a
    # missing user would WRONGPASS forever and the redis wake bus would never
    # deliver to agents.
    return _ensure_redis_acl(redis_port, cluster_secret, identity)


def _ensure_redis_acl(redis_port: int, cluster_secret: str, identity: str) -> int:
    """Add the cluster's ACL user (the runtime identity, names-as-data — passed in
    by the caller, never derived from a name) on top of `requirepass`. requirepass
    authenticates the `default` admin user, which provisions the ACL user.
    Re-affirmed every start (survives a restart that drops the in-memory ACL).

    Empty `cluster_secret` (single-box no-auth): requirepass stays off and the
    user is created with `nopass` — the identity-carrying runtime URLs still
    AUTH as a named user, and the AUTH must succeed for the wake bus."""
    admin = (
        f"redis://default:{cluster_secret}@127.0.0.1:{redis_port}"
        if cluster_secret
        else f"redis://127.0.0.1:{redis_port}"
    )
    try:
        ensure_cluster_redis_acl(
            identity,
            redis_admin_url=admin,
            cluster_secret=cluster_secret,
            channel_prefix=settings.data_plane.events_channel.removesuffix(":events"),
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
    identity: str,
    runner_password: str | None = None,
) -> int:
    """Bring up (or reload) this cluster's PgBouncer pooler in front of the local
    Postgres. Only reached when AVA_PGBOUNCER_ENABLED; the db name and role (the
    pooled front door's scram identity) are the caller-passed data-plane
    identity (names-as-data — db and role share the identifier).

    `runner_password` (the gateway .env AVA_RUNNER_DB_PASSWORD) is threaded at
    install birth — the .env does not exist yet then — and resolved from the
    home's .env file on every later bring-up (the userlist carries an
    `ava_runner` entry only once the cluster has a runner credential)."""
    from cli.commands._pgbouncer import ensure_pgbouncer, runner_password_from_env

    return ensure_pgbouncer(
        pg_port=pg_port,
        listen_port=listen_port,
        db_name=identity,
        role=identity,
        cluster_secret=cluster_secret,
        runner_password=runner_password
        if runner_password is not None
        else runner_password_from_env(),
    )


def ensure_cluster_instance(
    *,
    pg_port: int,
    redis_port: int,
    cluster_secret: str,
    pgbouncer_port: int,
    identity: str,
    runner_password: str | None = None,
) -> int:
    """Bring up this cluster's own Postgres + Redis (+ PgBouncer when enabled) on its
    allocated ports (idempotent). Returns 0 on success. The Postgres role/db/schema
    are provisioned separately by cluster_lifecycle._provision against pg_admin_url().

    `identity` is the cluster's data-plane identifier (db / role / redis ACL
    user), names-as-data: an existing cluster's caller reads it from the
    cluster's own `.env` URLs (`shared.cluster.identity_from_url`); install-time
    birth passes the fixed `DATA_PLANE_IDENTITY`. `runner_password` is the
    gateway .env AVA_RUNNER_DB_PASSWORD, threaded at birth (no .env yet) and
    resolved from the file otherwise; it lands in the pooler's userlist as the
    `ava_runner` credential.

    PgBouncer is brought up after Postgres whenever AVA_PGBOUNCER_ENABLED (ON by
    default); setting it false is a kill-switch — the pooler never starts, converge
    rewrites AVA_DB_URL to the direct Postgres port, and consumers reach Postgres
    directly through the one URL, an instant, zero-data-plane-change rollback."""
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
            "      ava enroll --gateway <url> --machine-name <name> \\\n"
            "                 --machine-host <this-host-private-ip>\n"
            "    What a gateway would additionally require: future/infra/windows-gateway.md",
            file=sys.stderr,
        )
        return 1
    print(f"\n→ per-cluster data plane (pg :{pg_port}, redis :{redis_port})")
    if (rc := _start_pg(pg_port, cluster_secret)) != 0:
        return rc
    if (rc := _start_redis(redis_port, cluster_secret, identity)) != 0:
        return rc
    if settings.data_plane.pgbouncer_enabled:
        return _start_pgbouncer(
            pg_port=pg_port,
            listen_port=pgbouncer_port,
            cluster_secret=cluster_secret,
            identity=identity,
            runner_password=runner_password,
        )
    return 0


def _redis_reachable(redis_port: int) -> bool:
    """True if this cluster's redis answers PING as the `default` user (requirepass =
    the cluster secret). Used by `ava status`; degrades to False on any error."""
    import redis as _redis

    client = _redis.Redis(
        host="127.0.0.1",
        port=redis_port,
        # A no-secret cluster has no requirepass — pass None so redis-py sends
        # no AUTH (an empty-string password would send `AUTH ""` and fail).
        password=settings.data_plane.cluster_secret or None,
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
    drifted off the cluster secret reports `✗ ... connect failed` rather than a
    false `✓` — that drift silently fails every DB-backed endpoint while pg_isready
    alone (loopback `trust`) stays green. Redis pings with the cluster secret. Both
    ports come from this cluster's own db_url / redis_url (its per-cluster
    instance).

    The probe goes POOLED, never `direct=True` (user ruling 2026-08: every consumer
    sits behind PgBouncer): the pooled `SELECT 1` proves the path consumers
    actually use — client scram against the pooler's userlist (rewritten every
    start from the cluster secret) plus the pooler → Postgres trust-socket hop.
    PG-side scram verifier drift is unreachable through the pooler by design (the
    backend hop carries no credential), so a direct probe would test a path no
    consumer dials. With PgBouncer disabled `pooled_db_url == db_url` and the probe
    is direct anyway."""
    import shared.db

    pg_port = urlsplit(settings.data_plane.db_url).port or 5432
    redis_port = urlsplit(settings.data_plane.redis_url).port or 6379
    if not _pg_running(pg_port):
        print(f"  ✗ postgres (127.0.0.1:{pg_port}) unreachable")
    else:
        try:
            with shared.db.connect() as conn:  # pooled front door (PgBouncer when enabled)
                conn.execute("select 1")
            print(f"  ✓ postgres (127.0.0.1:{pg_port})")
        except Exception as exc:
            lines = [ln.strip() for ln in str(exc).splitlines() if ln.strip()]
            detail = lines[-1] if lines else "connection failed"
            print(f"  ✗ postgres (127.0.0.1:{pg_port}) reachable but connect failed: {detail}")
    print(f"  {'✓' if _redis_reachable(redis_port) else '✗'} redis (127.0.0.1:{redis_port})")
    if settings.data_plane.pgbouncer_enabled:
        from cli.commands._pgbouncer import pgbouncer_reachable
        from shared.cluster import db_identity, get_record, record_pgbouncer_port

        # The pooler LISTENS on the registry-derived port (`ensure_cluster_instance`
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
            ok = pgbouncer_reachable(port, identity, identity, settings.data_plane.cluster_secret)
            print(f"  {'✓' if ok else '✗'} pgbouncer (127.0.0.1:{port}, transaction pooling)")


def _redis_endpoint() -> tuple[int, str | None] | None:
    """(port, password) of this cluster's redis from settings.data_plane.redis_url, or None
    if not resolvable (no instance to stop). The password is None on a no-secret
    cluster (redis has no requirepass then)."""
    from urllib.parse import urlsplit

    parts = urlsplit(settings.data_plane.redis_url)
    if not parts.port:
        return None
    # parts.password may already be None on a no-secret URL — `or` keeps that.
    return parts.port, parts.password or settings.data_plane.cluster_secret or None


def stop_cluster_instance() -> int:
    """Stop this cluster's own Postgres + Redis (data preserved on disk). The
    counterpart of ensure_cluster_instance for `ava stop` / `ava cluster down` of a
    cluster running its own instance. Best-effort: a not-running instance is a
    no-op success."""
    data = _pg_data_dir()
    print("\n→ stopping per-cluster data plane")
    # Stop the pooler first (best-effort, no-op if it was never enabled) so clients
    # are disconnected before Postgres goes down.
    from cli.commands._pgbouncer import stop_pgbouncer

    stop_pgbouncer()
    if (data / "PG_VERSION").exists():
        subprocess.run(
            [_pg_bin("pg_ctl"), "-D", str(data), "-m", "fast", "stop"],
            check=False,
            capture_output=True,
        )
        print("  ✓ postgres stopped")
    endpoint = _redis_endpoint()
    if endpoint is not None:
        port, password = endpoint
        subprocess.run(
            [_redis_cli_bin(), "-p", str(port), "shutdown", "nosave"],
            env=_redis_cli_env(password or ""),
            check=False,
            capture_output=True,
        )
        print("  ✓ redis stopped")
    return 0
