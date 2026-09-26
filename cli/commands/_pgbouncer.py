"""Per-cluster PgBouncer transaction pooler.

The pooled front door consumers dial past ~50 agents (each agent holds 2
Postgres connections; see `agent/db.py`).
PgBouncer is the third per-cluster data-plane process — a peer of this cluster's
own Postgres and Redis (`_cluster_instance.py`) — brought up on the cluster's own
`pgbouncer` port (a registry-record fact; the port is no longer materialized in
`.env` — AVA_DB_URL carries it) after schema preparation and group grants whenever
`AVA_PGBOUNCER_ENABLED` (ON by default: past ~50 agents pooling is the density
path). Setting it false is a kill-switch: nothing here runs, converge rewrites
AVA_DB_URL to the direct Postgres port, and every consumer talks to Postgres
directly through the one URL.

Auth — always authenticated, whatever the cluster secret (decisions/
2026-09-26-internal-data-plane-always-authenticated.md):

- **client → pgbouncer**: `auth_type = scram-sha-256` against a `userlist.txt`
  (0600) rendered by `shared.cluster.authority.render_userlist`: exactly the
  delivered write generation's two logins with their stored SCRAM verifiers,
  plus the operator admin-console entry `ava_pooler_admin` (a userlist-only
  name, never a PostgreSQL role). No plaintext password, no owner entry.
- **pgbouncer → postgres**: over the owner-only unix socket with SCRAM
  pass-through — the userlist verifier equals `pg_authid.rolpassword`, so the
  client's SCRAM keys authenticate the backend hop (`local all all
  scram-sha-256`); the pooler holds no server credential of its own.
- **userlist change = restart**: a SIGHUP reload keeps a removed user that
  already authenticated and still admits its new sessions, so changed userlist
  (or ini) bytes restart the pooler; unchanged bytes only reload.

`pool_mode = transaction`: agent/daemon client pools collapse onto a small set of
real Postgres backends. Every pooled consumer connects with `prepare_threshold=None`
(no server-side prepared statements — psycopg3's `0` would mean prepare on the
FIRST execution, and two fresh connections preparing the same `_pg3_0` name on one
backend raise DuplicatePreparedStatement), which is what makes transaction pooling
safe across the different backends a transaction hands out.

POSIX only (macOS brew / Linux apt `pgbouncer` on PATH). Windows fails fast upstream
in `complete_gateway_data_plane`, so this module is never reached there.
"""

from __future__ import annotations

import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

import shared.port_preflight
from cli.commands._cluster_instance import (
    _BIND_WAIT_TIMEOUT_S,
    _bind_addrs,
    _pg_socket_dir,
    _wait_for_reachable_bind,
)
from cli.commands._converge_spec import ConvergeCtx
from cli.commands._pooler_stop import OwnedPooler
from shared.cluster import ownership
from shared.cluster.authority import POOLER_ADMIN
from shared.machine import reachable_host
from shared.paths import ava_home
from shared.pg_tools import brew_prefix, is_macos
from shared.proc import process_alive

# Transaction-pooling defaults. max_client_conn
# caps total in-flight psycopg connections through the pooler; default_pool_size is
# the real Postgres backend pool per (db,user).
_MAX_CLIENT_CONN = 500
_DEFAULT_POOL_SIZE = 25

# libpq/psycopg send these startup parameters; PgBouncer rejects unknown ones unless
# told to ignore them. Real GUCs (search_path etc.) are deliberately NOT here.
_IGNORE_STARTUP_PARAMETERS = "extra_float_digits,options"


def _pgbouncer_dir() -> Path:
    d = ava_home() / "pgbouncer"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ini_path() -> Path:
    return _pgbouncer_dir() / "pgbouncer.ini"


def _userlist_path() -> Path:
    return _pgbouncer_dir() / "userlist.txt"


def _pidfile_path() -> Path:
    return _pgbouncer_dir() / "pgbouncer.pid"


def _logfile_path() -> Path:
    return _pgbouncer_dir() / "pgbouncer.log"


def pgbouncer_bin() -> str:
    """Resolve the pgbouncer binary. macOS: the brew keg (not symlinked onto PATH,
    like the redis binaries). Linux: PATH, then apt's `/usr/sbin` / `/usr/bin`
    (apt installs pgbouncer under /usr/sbin, which is often off a non-root PATH)."""
    if is_macos():
        return str(brew_prefix("pgbouncer") / "bin" / "pgbouncer")
    found = shutil.which("pgbouncer")
    if found:
        return found
    for candidate in ("/usr/sbin/pgbouncer", "/usr/bin/pgbouncer"):
        if Path(candidate).exists():
            return candidate
    return "pgbouncer"  # last resort — subprocess will surface a clear error


def _render_ini(*, pg_port: int, listen_port: int, db_name: str, cluster_secret: str) -> str:
    """The pgbouncer.ini for this cluster's pooler. One [databases] entry mapping the
    cluster db to the local Postgres over its owner-only unix socket; [pgbouncer]
    sets transaction pooling, SCRAM client auth against the userlist (always), and
    loopback + reachable binds (never all interfaces), matching Postgres's posture.
    The cluster secret decides only the bind: loopback alone without one.

    The backend socket dir is the one the RUNNING pg actually listens on
    (`_pg_socket_dir`, the exact directory the admin dial uses): `_start_pg` skips a
    pg that is already up, so across the path-only cutover a pre-cutover pg
    still serves the old name-keyed dir — rendering the canonical dir there
    would break every pooled query while admin readiness stayed green.

    Every pooled backend is born with the statement ceiling via `connect_query`,
    so a query is bounded regardless of which code path the client came through.
    The pooler drops the libpq `options` startup parameter, and
    `track_extra_parameters` cannot deliver statement_timeout (only GUC_REPORT
    parameters Postgres reports to clients can be tracked), so the connect_query
    SET is the one pooler-side path that reaches the backend. Same value +
    constant as shared/db.py's client-side SET (imported lazily — this module
    runs in data-plane bring-up, before any settings/env load is guaranteed).

    `server_reset_query = DISCARD ALL` — one statement, unquoted (pgbouncer
    1.25.2 runs the value verbatim: a quoted value was a syntax error
    (2026-09-02 P0, ~18M errors); a multi-statement value is rejected inside
    the implicit transaction pooling wraps it in (measured 2026-09-02 02:21).
    `server_reset_query_always = 0` keeps the reset to the SV_ACTIVE
    window (client vanished mid-transaction) and error returns — clean
    releases/disconnects never run it (measured 2026-09-03), so a client's own
    dial/borrow SETs (the statement ceiling) survive between its transactions.
    always=1 was tried and rejected (405 ruling 2026-09-03, option B): firing
    after EVERY transaction end, its DISCARD ALL wiped the client's SETs too
    (borrowers measured statement_timeout=0). Between-transaction pollution is
    defended client-side (shared/db.py baseline restore per dial/borrow +
    read-write write posture; 2026-09-02 P0)."""
    listen_addr = ", ".join(_bind_addrs(cluster_secret))
    socket_dir = _pg_socket_dir()
    from shared.db import PG_STATEMENT_TIMEOUT_SET_SQL

    connect_query = f"connect_query='{PG_STATEMENT_TIMEOUT_SET_SQL}'"
    # One statement, unquoted (verbatim pass-through; rationale in the
    # docstring above). DISCARD ALL clears the birth ceiling; no re-apply here.
    server_reset = "server_reset_query = DISCARD ALL"
    return "\n".join(
        [
            "[databases]",
            # host=<socket dir> routes pgbouncer -> Postgres over the owner-only
            # unix socket with SCRAM pass-through (no server credential); connect_query
            # births every pooled backend with the 60s statement ceiling (the
            # pooler drops the client's `options` startup parameter).
            f"{db_name} = host={socket_dir} port={pg_port} dbname={db_name} {connect_query}",
            "",
            "[pgbouncer]",
            f"listen_addr = {listen_addr}",
            f"listen_port = {listen_port}",
            "auth_type = scram-sha-256",
            f"auth_file = {_userlist_path()}",
            "pool_mode = transaction",
            server_reset,
            # always=0 (SV_ACTIVE-window reset only; rationale in docstring).
            "server_reset_query_always = 0",
            f"max_client_conn = {_MAX_CLIENT_CONN}",
            f"default_pool_size = {_DEFAULT_POOL_SIZE}",
            f"ignore_startup_parameters = {_IGNORE_STARTUP_PARAMETERS}",
            # Admin/stats console (`SHOW POOLS` etc.) for the userlist-only
            # operator entry; no database role can reach it.
            f"admin_users = {POOLER_ADMIN}",
            f"stats_users = {POOLER_ADMIN}",
            "log_connections = 0",
            "log_disconnections = 0",
            f"logfile = {_logfile_path()}",
            f"pidfile = {_pidfile_path()}",
            "",
        ]
    )


def _write_config(
    *, pg_port: int, listen_port: int, db_name: str, cluster_secret: str, userlist: bytes
) -> bool:
    """Write pgbouncer.ini + userlist.txt (0600); True when either file's bytes
    changed, which requires a pooler restart (a reload never revokes a user)."""
    from shared.private_storage import write_private_bytes

    ini = _render_ini(
        pg_port=pg_port, listen_port=listen_port, db_name=db_name, cluster_secret=cluster_secret
    ).encode()
    changed = False
    for path, body in ((_ini_path(), ini), (_userlist_path(), userlist)):
        try:
            current = path.read_bytes()
        except FileNotFoundError:
            current = None
        if current != body:
            write_private_bytes(path, body)
            changed = True
    return changed


def _pid_is_our_pooler(pid: int) -> bool:
    """Whether `pid` is really THIS home's pooler, and not a stranger that
    inherited the number.

    A stale pidfile can name a recycled process after a crash or explicit force
    stop. Validate its executable and exact configuration path before treating
    it as a candidate; native custody also captures its process birth before
    any reload or shutdown signal.

    The identity token is the config file the process was started with — the one
    argument that names a home. It is read back the way the process itself
    resolved it: the `-d <ini>` argument from its argv, resolved against its own
    working directory when that argument is relative.

    Both halves are load-bearing, and which one carries the path is a PLATFORM
    difference rather than a fallback. Measured on both:

        Linux, apt pgbouncer 1.18   argv keeps the absolute ini path;
                                    cwd stays wherever the launcher stood
        macOS, brew pgbouncer 1.25  argv is rewritten to a bare `pgbouncer.ini`;
                                    the process chdir()s into the config dir

    Neither field alone identifies the pooler on both. Resolving one against the
    other is the single rule that reads both, and it is exactly the resolution
    pgbouncer performed to find its own config.

    Compared as a resolved path, never as a substring: `~/.ava` is a prefix of
    `~/.ava-preview`, and matching pooler processes by substring is how a
    co-located cluster's tooling reaches the wrong home — what `pkill -f
    pgbouncer.ini` did to production on 2026-08-06.

    A process this user cannot introspect counts as NOT ours. A pooler that
    outlives its stop retains custody."""
    import psutil

    try:
        proc = psutil.Process(pid)
        if proc.name() != "pgbouncer":
            return False
        argv = proc.cmdline()
        if "-d" not in argv:
            return False
        ini = Path(argv[argv.index("-d") + 1])
        if not ini.is_absolute():
            ini = Path(proc.cwd()) / ini
        return ini.resolve() == _ini_path().resolve()
    except (psutil.Error, OSError, IndexError):
        return False


def _running_pid() -> int | None:
    """The pid of a live pgbouncer OWNED BY THIS HOME (from its pidfile), or None.

    Stop uses this for stale-pidfile cleanup before capturing native custody.
    A pidfile that no longer names our pooler — process gone,
    or the number since recycled onto someone else (`_pid_is_our_pooler`) — reads
    as None and is removed, so the next bring-up starts from a clean slate instead
    of re-deciding against the same dead number. A live stranger is reported: that
    line is the one that makes a cross-home pidfile visible before it costs an
    outage."""

    pidfile = _pidfile_path()
    if not pidfile.exists():
        return None
    try:
        pid = int(pidfile.read_text().strip())
    except (ValueError, OSError):
        return None
    if _pid_is_our_pooler(pid):
        return pid
    if process_alive(pid):
        print(
            f"  · {pidfile} names pid {pid}, which is not this home's pooler "
            "(recycled pid) — not signalling it; removing the stale pidfile",
            file=sys.stderr,
        )
    pidfile.unlink(missing_ok=True)
    return None


def _admin_reachable(listen_port: int, admin_password: str, host: str = "127.0.0.1") -> bool:
    """Authenticate to the pooler's admin console without opening a backend.

    Backend readiness is proven separately by the caller, as each delivered
    login. Public bind verification reads the socket table, never a self-dial.
    """
    import psycopg

    from shared.url_secret import url_with_userinfo

    url = url_with_userinfo(
        f"postgresql://@{host}:{listen_port}/pgbouncer", POOLER_ADMIN, admin_password
    )
    try:
        with psycopg.connect(url, connect_timeout=3, autocommit=True, prepare_threshold=None):
            return True
    except Exception:
        return False


def pgbouncer_public_listener_reachable(listen_port: int, role: str, cluster_secret: str) -> bool:
    """True when the pooler listens on the address remote consumers actually dial.

    The loopback probe (`pgbouncer_listener_reachable`) proves "the pooler process
    is there"; this one proves "the PUBLIC front door is open". A pooler whose
    `listen_addr` includes the reachable address but failed to bind it
    keeps running on loopback alone — pgbouncer treats a failed bind as a WARNING,
    not an error — and a loopback-only probe cannot tell the difference, so
    `AVA_DB_URL`'s public path stays silently dead for every enrolled agent-runner
    (task #1288: 2026-08-16 a boot-time address race left the pooler loopback-only
    for two days).

    A local socket-table read is the authoritative fact for this question. A
    network self-dial through the reachable address is a hairpin route that VPN
    filtering can intermittently block even while the listener remains bound;
    treating that routing failure as a missing bind causes destructive false
    restarts. The exact reachable address and IPv4/IPv6 wildcard binds all cover
    the public front door. An empty table proves nothing and remains degraded.

    A no-secret cluster's pooler binds loopback only by design (`_bind_addrs`), so
    there is no public listener to check — returns True without inspecting the
    host or socket table. `role` remains in the stable probe signature shared by
    the healthcheck and bring-up callers; socket inspection needs no credential."""
    del role
    if _bind_addrs(cluster_secret) == ["127.0.0.1"]:
        return True
    reachable = reachable_host()
    addrs = shared.port_preflight.listener_addrs(listen_port)
    return bool(addrs & {reachable, "0.0.0.0", "::", "*"})  # noqa: S104 — matching OS wildcard binds, not opening one


def _wait_for_reachable_bind_gated(cluster_secret: str) -> bool:
    """Bounded wait for the configured reachable bind address — only when needed.

    A secret-set cluster's pooler binds loopback + the reachable address
    (`_bind_addrs`), so a boot that races the private network must wait for the
    address before starting. A no-secret cluster binds loopback ONLY, whatever
    `AVA_MACHINE_HOST` says — waiting on it would let a stray ambient
    `AVA_MACHINE_HOST` hold a warm `ava start` hostage for a bind that never
    happens (the same ambient-leak class `_bind_addrs` documents, task #1113).

    Returns True immediately when no wait is needed (loopback-only bind, or the
    address already assigned); False on timeout so the caller fails fast."""
    if _bind_addrs(cluster_secret) == ["127.0.0.1"]:
        return True
    return _wait_for_reachable_bind()


def _accepting_pooler(listen_port: int) -> OwnedPooler | None:
    owner = ownership.pooler(_ini_path(), _pidfile_path())
    if owner is None:
        ownership.require_listener(None, listen_port, required=False)
        return None
    custodian = OwnedPooler(owner, listen_port, _ini_path())
    custodian.require_accepting()
    return custodian


def ensure_pgbouncer(
    *,
    pg_port: int,
    listen_port: int,
    db_name: str,
    cluster_secret: str,
    userlist: bytes,
    admin_password: str,
) -> int:
    """Bring up (or reload) this cluster's PgBouncer on `listen_port`, pooling in
    front of the local Postgres on `pg_port`. Idempotent. Returns 0 on success.

    `userlist` is the exact `auth_file` (`shared.cluster.authority.render_userlist`)
    and `admin_password` the admin-console credential it carries. Outcomes for a
    running pooler:

    - **Unchanged + fully bound** — SIGHUP reload, live connections never bounce.
    - **Changed config or userlist** — RESTARTED, never reloaded: PgBouncer keeps
      a removed user that already authenticated after a SIGHUP and still admits
      its new sessions, so only a fresh process revokes it.
    - **Degraded** (answering on loopback but missing the reachable listener) —
      restarted: a SIGHUP reload does not retry a listen_addr that failed to bind
      at startup (verified on pgbouncer 1.25.2). The restart waits (bounded) for
      the reachable address first, and a stop that did not take is reported.
    - **Not running** — started fresh.

    A restart uses the safe shutdown only; a pooler that will not stop (a client
    holding a transaction) keeps custody and fails this call rather than being
    killed underneath an ordinary start.

    Boot-time address race (task #1288): pgbouncer treats a failed bind on one
    `listen_addr` entry as a WARNING and keeps running on the rest, so a pooler
    born before the private network assigned the reachable address degrades to
    loopback-only while a loopback-only probe reads it as healthy. The wait guards
    ONLY the paths that (re)start a pooler. After any (re)start the pooler must
    prove it listens on the reachable address too.

    Only called when AVA_PGBOUNCER_ENABLED (gated by the caller in
    `complete_gateway_data_plane`)."""
    binary = pgbouncer_bin()
    if not Path(binary).exists() and shutil.which(binary) is None:
        # Enabled but pgbouncer is not installed. Fail fast (do NOT silently fall back
        # to direct — the operator asked for pooling): install it, then retry.
        how = "brew install pgbouncer" if is_macos() else "sudo apt-get install -y pgbouncer"
        print(
            f"  ✗ AVA_PGBOUNCER_ENABLED is set but pgbouncer is not installed ({binary!r}). "
            f"Install it (`{how}`) and retry, or unset AVA_PGBOUNCER_ENABLED to run direct.",
            file=sys.stderr,
        )
        return 1
    custodian = _accepting_pooler(listen_port)
    changed = _write_config(
        pg_port=pg_port,
        listen_port=listen_port,
        db_name=db_name,
        cluster_secret=cluster_secret,
        userlist=userlist,
    )
    if custodian is not None:
        owner = custodian.identity
        pid = owner.pid
        public = pgbouncer_public_listener_reachable(listen_port, POOLER_ADMIN, cluster_secret)
        # A running pooler whose files are unchanged and whose public listener
        # verifies is reloaded, never waited on: a transient blip on the private
        # network must not hold `ava start` hostage behind a serving pooler (P1).
        if public and not changed:
            process = psutil.Process(pid)
            if not owner.live():
                raise RuntimeError("PgBouncer identity changed before reload")
            process.send_signal(signal.SIGHUP)
            if not _admin_reachable(listen_port, admin_password):
                print(
                    f"  ✗ pgbouncer (127.0.0.1:{listen_port}) refused its admin credential",
                    file=sys.stderr,
                )
                return 1
            print(f"  ✓ pgbouncer already running (127.0.0.1:{listen_port}), reloaded")
            return 0
        if not public and not _wait_for_reachable_bind_gated(cluster_secret):
            print(
                f"  ✗ reachable bind address {reachable_host()!r} is not assigned to any "
                f"local interface after {int(_BIND_WAIT_TIMEOUT_S)}s — the degraded "
                "pgbouncer cannot be restarted into a healthy double bind. On reboot "
                "this means the private network has not come up yet; retry `ava start` "
                "once it is.",
                file=sys.stderr,
            )
            return 1
        if public:
            print(
                "  · pgbouncer configuration or userlist changed — restarting (a reload never revokes)"
            )
        else:
            print(
                f"  ✗ pgbouncer is NOT listening on the reachable address "
                f"{reachable_host()!r} — it degraded to loopback-only (task #1288) and "
                "remote agent-runners cannot reach the pooled AVA_DB_URL. Reload cannot "
                "re-bind it; restarting the pooler",
                file=sys.stderr,
            )
        if not custodian.stop(deadline=time.monotonic() + 5.0):
            print(
                f"  ✗ could not stop the running pooler (pid {pid}) — it survived the "
                "graceful stop; custody retained, not starting a second pooler on the same port",
                file=sys.stderr,
            )
            return 1
    return _launch_pooler(listen_port, cluster_secret, admin_password)


def _launch_pooler(listen_port: int, cluster_secret: str, admin_password: str) -> int:
    if not _wait_for_reachable_bind_gated(cluster_secret):
        # Fail fast BEFORE starting: a pooler born now would degrade to loopback-only
        # and the public AVA_DB_URL path would be silently dead (the 2026-08-16
        # outage shape). The boot retry keeps re-running `ava start`, so this only
        # needs to be true once the private network is actually up.
        print(
            f"  ✗ reachable bind address {reachable_host()!r} is not assigned to any "
            f"local interface after {int(_BIND_WAIT_TIMEOUT_S)}s — pgbouncer would "
            "silently degrade to loopback-only and every remote agent-runner would "
            "lose the pooled AVA_DB_URL. On reboot this means the private network has "
            "not come up yet; retry `ava start` once it is.",
            file=sys.stderr,
        )
        return 1
    result = subprocess.run(
        [pgbouncer_bin(), "-d", str(_ini_path())],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(
            f"  ✗ pgbouncer start failed (rc={result.returncode}); see {_logfile_path()}\n"
            f"    {result.stderr.strip()}",
            file=sys.stderr,
        )
        return 1
    # -d daemonizes and returns immediately; wait for the admin console to
    # authenticate. The caller proves each delivered login's pooled backend.
    for _ in range(60):
        if _admin_reachable(listen_port, admin_password):
            break
        time.sleep(0.1)
    else:
        print(
            f"  ✗ pgbouncer did not become ready on :{listen_port}; see {_logfile_path()}",
            file=sys.stderr,
        )
        return 1
    if not pgbouncer_public_listener_reachable(listen_port, POOLER_ADMIN, cluster_secret):
        # The pooler is up on loopback but NOT on the address consumers dial — the
        # silent degradation this whole function exists to never ship. Loud failure:
        # the boot retry / watchdog keeps re-running, and once the private network
        # is up the next pass restarts the pooler into a healthy double bind.
        print(
            f"  ✗ pgbouncer started but is NOT listening on the reachable address "
            f"{reachable_host()!r} — it degraded to loopback-only (see "
            f"{_logfile_path()}). Every remote agent-runner's pooled AVA_DB_URL is "
            "dead. Retry `ava start` once the private network is up; the watchdog "
            "healthcheck will keep re-attempting.",
            file=sys.stderr,
        )
        return 1
    ownership.require_listener(ownership.pooler(_ini_path(), _pidfile_path()), listen_port)
    print(f"  ✓ pgbouncer started (127.0.0.1:{listen_port}, transaction pooling)")
    return 0


def pgbouncer_listener_reachable(listen_port: int, admin_password: str) -> bool:
    """Is the POOLER itself up — the admin-console probe, with no server hop.

    The watchdog healthcheck's question: an end-to-end `SELECT 1` also fails when
    Postgres is down, and restarting the pooler is the wrong answer to that. This
    separates "the pooler process is gone" (repairable by `ensure_pgbouncer`) from
    "the pooler is fine and the backend behind it is not"."""
    return _admin_reachable(listen_port, admin_password)


def stop_pgbouncer(*, force: bool = False) -> None:
    """Stop this home's captured pooler; a previous drain is only waited on.

    `force` escalates a pooler that ignored the safe shutdown to SIGKILL; only a
    caller that owns an already-quiescent, already-revoked data plane passes it.
    """
    pid = _running_pid()
    if pid is None:
        return
    owner = ownership.pooler(_ini_path(), _pidfile_path())
    if owner is None:
        return
    custodian = OwnedPooler.from_config(owner, _ini_path())
    if not custodian.stop(deadline=time.monotonic() + 5.0, force=force):
        raise RuntimeError("PgBouncer stop incomplete; custody retained")


def _ensure_pgbouncer_step(ctx: ConvergeCtx) -> None:
    """Converge step: reconcile the one DB URL with the pooler toggle, and
    preflight the binary. Gateway-only.

    AVA_DB_URL's port is decided by AVA_PGBOUNCER_ENABLED at URL generation, so
    converge (which runs before the data-plane bring-up on every `ava start` /
    `ava cluster update`) keeps the `.env` value in sync, idempotently:

    1. Normalize AVA_DB_URL's port: the pooler listener (`record_pgbouncer_port`)
       when the toggle is on, the direct Postgres port (`record_postgres_port`)
       when off. Only a URL currently carrying the OTHER port of this cluster is
       rewritten — the pre-cutover 5433 (direct) value becomes 6433 (pooler) on
       an enabled cluster, and a kill-switch flip back is equally one line; an
       operator stand-in URL naming neither port is left untouched. Fresh births
       already carry the toggle-matched port from derive_env.
    2. Drop the retired AVA_PGBOUNCER_PORT key from .env — the pooler port is a
       registry fact only (data-plane bring-up + admin plane), never an env key.
    3. When the pooler is enabled but the binary is missing, warn with the exact
       install command; the data-plane bring-up then fail-fasts on the same
       condition, so a deliberately-enabled pooler never silently degrades to
       direct. The install itself lives in the provision scripts
       (`brew install pgbouncer` / apt), not here, so `ava start` never triggers
       a heavyweight package install."""
    from urllib.parse import urlsplit

    from dotenv import dotenv_values

    from shared.cluster import get_record, record_pgbouncer_port, record_postgres_port
    from shared.config import settings
    from shared.dotenv_boot import UNANCHORED_DB_SENTINEL
    from shared.envfile import remove_env, upsert_env
    from shared.url_secret import url_with_port

    if settings.data_plane.is_remote:
        # The pooler is a local-instance component; a remote/SaaS plane's URL
        # is the provider's (its port is not this cluster's), so converge must
        # neither rewrite it nor preflight the local binary.
        return
    rec = get_record(ctx.ava_home)
    if rec is None:
        return
    env_path = ctx.ava_home / ".env"
    current = (dotenv_values(env_path).get("AVA_DB_URL") or "").strip()
    normalized: str | None = None
    if current and current != UNANCHORED_DB_SENTINEL:
        try:
            port = urlsplit(current).port
        except ValueError:
            port = None
        if port is not None:
            pg = record_postgres_port(rec)
            pooler = record_pgbouncer_port(rec)
            want = pooler if settings.data_plane.pgbouncer_enabled else pg
            if port != want and port in (pg, pooler):
                normalized = url_with_port(current, want)
    if normalized:
        upsert_env(env_path, {"AVA_DB_URL": normalized}, audit_site="converge_pgbouncer")
    remove_env(env_path, {"AVA_PGBOUNCER_PORT"}, audit_site="converge_pgbouncer")
    if not settings.data_plane.pgbouncer_enabled:
        return
    binary = pgbouncer_bin()
    if Path(binary).exists() or shutil.which(binary) is not None:
        return
    how = "brew install pgbouncer" if is_macos() else "sudo apt-get install -y pgbouncer"
    print(f"  ! pgbouncer enabled but not installed ({binary!r}) — run `{how}`", file=sys.stderr)
