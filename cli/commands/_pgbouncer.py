"""Per-cluster PgBouncer transaction pooler.

The pooled front door consumers dial past ~50 agents (each agent holds 2
Postgres connections; see `agent/db.py`).
PgBouncer is the third per-cluster data-plane process — a peer of this cluster's
own Postgres and Redis (`_cluster_instance.py`) — brought up on the cluster's own
`pgbouncer` port (a registry-record fact; the port is no longer materialized in
`.env` — AVA_DB_URL carries it) right after Postgres whenever
`AVA_PGBOUNCER_ENABLED` (ON by default: past ~50 agents pooling is the density
path). Setting it false is a kill-switch: nothing here runs, converge rewrites
AVA_DB_URL to the direct Postgres port, and every consumer talks to Postgres
directly through the one URL.

Auth (mirrors the redis requirepass model, avoiding a scram-verifier auth_query):

- **client → pgbouncer**: `auth_type = scram-sha-256` against a `userlist.txt`
  holding the cluster owner role with its independent DB-admin password in
  plain text (0600, rewritten every start), plus the least-privilege `ava_runner`
  role with its own password (`AVA_RUNNER_DB_PASSWORD`, Task #1236) once the
  cluster has one — the credential the projected runner `AVA_DB_URL` dials
  with. PgBouncer derives the SCRAM server-side verification from the
  plaintext, so an external / LAN client still needs its role's password; there is no
  passwordless pooled front door. A no-secret cluster (single-box, no auth
  anywhere) sets `auth_type = trust` instead and binds loopback only — the
  pooler never opens a passwordless front door to the LAN.
- **pgbouncer → postgres**: over Postgres's local unix socket (`local all all
  trust` — the same 0700 owner-only provisioning socket), so PgBouncer needs no
  server credential at all and can never drift off a rotated scram verifier. This
  ties PgBouncer to the same host as its Postgres, which it always is (both are
  this cluster's data plane on the gateway box).

`pool_mode = transaction`: agent/daemon client pools collapse onto a small set of
real Postgres backends. Every pooled consumer connects with `prepare_threshold=None`
(no server-side prepared statements — psycopg3's `0` would mean prepare on the
FIRST execution, and two fresh connections preparing the same `_pg3_0` name on one
backend raise DuplicatePreparedStatement), which is what makes transaction pooling
safe across the different backends a transaction hands out.

POSIX only (macOS brew / Linux apt `pgbouncer` on PATH). Windows fails fast upstream
in `ensure_cluster_instance`, so this module is never reached there.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import shared.port_preflight
from cli.commands._cluster_instance import (
    _BIND_WAIT_TIMEOUT_S,
    _bind_addrs,
    _live_pg_socket_dir,
    _wait_for_reachable_bind,
)
from cli.commands._converge_spec import ConvergeCtx
from shared.cluster.derive import RUNNER_ROLE
from shared.machine import reachable_host
from shared.paths import ava_home
from shared.pg_tools import brew_prefix, is_macos
from shared.proc import process_alive

# Transaction-pooling defaults. max_client_conn
# caps total in-flight psycopg connections through the pooler; default_pool_size is
# the real Postgres backend pool per (db,user).
_MAX_CLIENT_CONN = 500
_DEFAULT_POOL_SIZE = 25

# How often `_terminate_verified` re-probes while waiting out its grace period, and
# how long it lets a force kill land before calling the process a survivor. Both are
# reaction times for a stop that is already happening, not judgments about whether a
# host is making progress, so they are deliberately not in the `shared.deploy_timing`
# lattice.
_TERMINATE_POLL_S = 0.2
_FORCE_KILL_SETTLE_S = 0.5

# libpq/psycopg send these startup parameters; PgBouncer rejects unknown ones unless
# told to ignore them. Real GUCs (search_path etc.) are deliberately NOT here.
_IGNORE_STARTUP_PARAMETERS = "extra_float_digits,options"


def runner_password_from_env() -> str:
    """This home's AVA_RUNNER_DB_PASSWORD from its .env FILE, or "".

    Kept as a pooler-local seam for callers and tests; the secret ownership is
    in ``shared.cluster`` so non-CLI consumers never import this CLI module.
    An absent/empty value means the cluster predates the runner-role cutover.
    """
    from shared.cluster import runner_password_from_env as read_runner_password

    return read_runner_password(ava_home())


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


def _render_userlist(
    role: str,
    db_admin_password: str,
    runner_role: str | None = None,
    runner_password: str | None = None,
) -> str:
    """`"user" "password"` lines — the cluster role with its DB-admin password, plus the
    `ava_runner` entry with its own password once the cluster has one. PgBouncer
    double-quotes both fields; escape any embedded quote.

    The runner entry appears only when a runner password exists: a legacy
    cluster that never ran `ava cluster ensure-db-role` keeps a byte-identical
    userlist, and a scram entry with an empty password would reject every
    ava_runner dial anyway (nobody dials as ava_runner before the cutover, so
    the entry's absence is silent either way)."""
    esc = db_admin_password.replace('"', '""')
    lines = [f'"{role}" "{esc}"']
    if runner_role and runner_password:
        esc_runner = runner_password.replace('"', '""')
        lines.append(f'"{runner_role}" "{esc_runner}"')
    return "\n".join(lines) + "\n"


def _render_ini(
    *, pg_port: int, listen_port: int, db_name: str, role: str, cluster_secret: str
) -> str:
    """The pgbouncer.ini for this cluster's pooler. One [databases] entry mapping the
    cluster db to the local Postgres over its trust unix socket; [pgbouncer] sets
    transaction pooling, client auth against the userlist, and loopback +
    reachable binds (never all interfaces), matching Postgres's posture.

    Client auth follows the authenticated-cluster posture: `scram-sha-256` when
    a bearer is set (the pooled front door then requires the independent owner
    or runner DB password), `trust` when the cluster has none — a no-secret
    cluster is fully unauthenticated, so the pooler cannot demand a credential
    the cluster does not have (and its `listen_addr` is loopback-only, gated by
    the same posture flag, so trust never reaches the LAN).

    The backend socket dir is the one the RUNNING pg actually listens on
    (`_live_pg_socket_dir`, same probe the admin dial uses): `_start_pg` skips a
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
    socket_dir = _live_pg_socket_dir(pg_port)
    from shared.db import PG_STATEMENT_TIMEOUT_SET_SQL

    connect_query = f"connect_query='{PG_STATEMENT_TIMEOUT_SET_SQL}'"
    # One statement, unquoted (verbatim pass-through; rationale in the
    # docstring above). DISCARD ALL clears the birth ceiling; no re-apply here.
    server_reset = "server_reset_query = DISCARD ALL"
    return "\n".join(
        [
            "[databases]",
            # host=<socket dir> routes pgbouncer -> Postgres over the trust unix
            # socket, so the pooler needs no server credential; connect_query
            # births every pooled backend with the 60s statement ceiling (the
            # pooler drops the client's `options` startup parameter).
            f"{db_name} = host={socket_dir} port={pg_port} dbname={db_name} {connect_query}",
            "",
            "[pgbouncer]",
            f"listen_addr = {listen_addr}",
            f"listen_port = {listen_port}",
            "auth_type = scram-sha-256" if cluster_secret else "auth_type = trust",
            f"auth_file = {_userlist_path()}",
            "pool_mode = transaction",
            server_reset,
            # always=0 (SV_ACTIVE-window reset only; rationale in docstring).
            "server_reset_query_always = 0",
            f"max_client_conn = {_MAX_CLIENT_CONN}",
            f"default_pool_size = {_DEFAULT_POOL_SIZE}",
            f"ignore_startup_parameters = {_IGNORE_STARTUP_PARAMETERS}",
            # Admin/stats console reachable as the cluster role (over the same TCP
            # listener) for `SHOW POOLS` etc.
            f"admin_users = {role}",
            f"stats_users = {role}",
            "log_connections = 0",
            "log_disconnections = 0",
            f"logfile = {_logfile_path()}",
            f"pidfile = {_pidfile_path()}",
            "",
        ]
    )


def _write_config(
    *,
    pg_port: int,
    listen_port: int,
    db_name: str,
    role: str,
    cluster_secret: str,
    db_admin_password: str = "",
    runner_role: str | None = None,
    runner_password: str | None = None,
) -> None:
    """Write pgbouncer.ini + userlist.txt (0600) fresh every start, so a
    DB-admin or runner-password rotation or port change is reflected (a running
    pooler is then reloaded)."""
    self_ini = _ini_path()
    self_ini.write_text(
        _render_ini(
            pg_port=pg_port,
            listen_port=listen_port,
            db_name=db_name,
            role=role,
            cluster_secret=cluster_secret,
        )
    )
    userlist = _userlist_path()
    userlist.write_text(_render_userlist(role, db_admin_password, runner_role, runner_password))
    userlist.chmod(0o600)


def _pid_is_our_pooler(pid: int) -> bool:
    """Whether `pid` is really THIS home's pooler, and not a stranger that
    inherited the number.

    A pidfile holds a bare integer and the OS recycles pids. pgbouncer unlinks its
    pidfile on a clean exit but cannot on a force kill — and `_terminate_verified`
    force-kills a straggler after 5s while a pooler holding live clients drains for
    minutes, so an ordinary `ava stop` on a busy cluster is the normal way to
    leave a stale pidfile behind, not an exotic one.

    What that costs is concrete: `ensure_pgbouncer` treats a live pid as "already
    running", SIGHUPs it and returns success. Once the number has been recycled,
    every `ava start` signals an unrelated process (SIGHUP terminates most things
    that are not pgbouncer) and starts no pooler — and since only a successful
    start rewrites the pidfile, the state sustains itself. `AVA_DB_URL` carries
    the pooler port, so the cluster is left with no database path at all.

    This is also what makes the pooler the odd sibling in `stop_cluster_instance`:
    Postgres is addressed by its data directory (`pg_ctl -D`) and redis by port +
    this cluster's password, so neither can be aimed at the wrong process, let
    alone the wrong home. And the house rule already exists one layer down —
    `shared/posixproc.py` records a session's pid *and* its start-time and calls a
    record live only when both match, precisely "to defeat pid recycling". The
    pooler was the one signalling path left trusting the number alone.

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
    outlives its stop is loud and idempotently restartable; a cross-cluster kill
    is neither."""
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

    The one seam both signalling paths read — `stop_pgbouncer`'s SIGTERM and
    `ensure_pgbouncer`'s reload SIGHUP (which is a kill for most processes that
    are not pgbouncer). A pidfile that no longer names our pooler — process gone,
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


def _admin_reachable(
    listen_port: int, role: str, cluster_secret: str, host: str = "127.0.0.1"
) -> bool:
    """Readiness probe that authenticates the client (scram) WITHOUT a server hop.

    Connects to PgBouncer's virtual `pgbouncer` admin database — a successful open
    proves the listener is up and client scram works, but opens no backend
    connection. This is what readiness must use: the pooler is brought up BEFORE the
    cluster role/db is provisioned (both birth and a fresh start start the data plane
    first, then provision), so a probe that dialed the real db would fail with `role
    "ava_<cluster>" does not exist` and hang the bring-up. The admin console speaks
    only the simple query protocol, so we open + close without a query (psycopg's
    extended-protocol execute is rejected there).

    `host` defaults to loopback — the listener that is always up once the pooler
    runs. Public-bind verification is deliberately separate: it reads the local
    socket table rather than making a network self-dial."""
    import psycopg

    from shared.url_secret import url_with_userinfo

    url = url_with_userinfo(f"postgresql://@{host}:{listen_port}/pgbouncer", role, cluster_secret)
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


def _reachable(listen_port: int, db_name: str, role: str, cluster_secret: str) -> bool:
    """True if the pooler answers an authenticated SELECT 1 through the listener to the
    cluster db — end-to-end proof that client scram + the trust socket server hop both
    work. Used by the `ava status` probe, where the runtime role/db already exist (not
    for bring-up readiness, which predates provisioning — see `_admin_reachable`)."""
    import psycopg

    from shared.url_secret import url_with_userinfo

    url = url_with_userinfo(
        f"postgresql://@127.0.0.1:{listen_port}/{db_name}", role, cluster_secret
    )
    try:
        with psycopg.connect(url, connect_timeout=3, prepare_threshold=None) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


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


def ensure_pgbouncer(
    *,
    pg_port: int,
    listen_port: int,
    db_name: str,
    role: str,
    cluster_secret: str,
    db_admin_password: str = "",
    runner_password: str | None = None,
) -> int:
    """Bring up (or reload) this cluster's PgBouncer on `listen_port`, pooling in
    front of the local Postgres on `pg_port`. Idempotent. Returns 0 on success.

    Three outcomes for a running pooler, only one of which touches its sockets:

    - **Healthy + fully bound** (verified on the reachable address too) — SIGHUP
      reload (re-reads auth_file + settings), live connections never bounce.
    - **Degraded** (answering on loopback but missing the reachable listener) —
      RESTARTED, not reloaded: a SIGHUP reload does not retry a listen_addr that
      failed to bind at startup (verified on pgbouncer 1.25.2), so only a process
      restart re-binds it. The restart waits (bounded) for the reachable address
      first, and a terminate that did not take is reported, not papered over.
    - **Not running** — started fresh.

    Boot-time address race (task #1288): pgbouncer treats a failed bind on one
    `listen_addr` entry as a WARNING and keeps running on the rest, so a pooler
    born before the private network assigned the reachable address degrades to
    loopback-only while a loopback-only probe reads it as healthy — the pooled
    `AVA_DB_URL` public path stays silently dead for every remote agent-runner.
    The wait guards ONLY the paths that (re)start a pooler; a running pooler
    whose public listener verifies reloads without ever consulting the address,
    so a transient network blip cannot hold `ava start` hostage. After any
    (re)start the pooler must prove it listens on the reachable address too.

    When `runner_password` is omitted, it is resolved from this home's `.env`
    before the userlist rewrite.

    Only called when AVA_PGBOUNCER_ENABLED (gated by the caller in
    `ensure_cluster_instance`)."""
    db_admin_password = db_admin_password or cluster_secret
    if runner_password is None:
        runner_password = runner_password_from_env()
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
    _write_config(
        pg_port=pg_port,
        listen_port=listen_port,
        db_name=db_name,
        role=role,
        cluster_secret=cluster_secret,
        db_admin_password=db_admin_password,
        runner_role=RUNNER_ROLE if runner_password else None,
        runner_password=runner_password,
    )
    pid = _running_pid()
    if pid is not None:
        # A running pooler whose public listener verifies is reloaded, never waited
        # on: a transient blip on the private network must not hold `ava start`
        # hostage behind an already-serving pooler (P1).
        if pgbouncer_public_listener_reachable(listen_port, role, cluster_secret):
            # Raw SIGHUP is safe HERE and nowhere else in this file: `signal.SIGHUP`
            # is undefined on Windows, but a pooler only exists on a gateway unit and
            # the gateway capability is POSIX-only (no native Windows redis to drive),
            # so this line is unreachable there. `_terminate_verified` below had the
            # same shape and was NOT unreachable — it is called from `_do_stop` on
            # every platform — which is why it goes through `shared.proc` now.
            os.kill(pid, signal.SIGHUP)  # online reload of ini + userlist
            print(f"  ✓ pgbouncer already running (127.0.0.1:{listen_port}), reloaded")
            _report_backend_verification(listen_port, db_name, role, db_admin_password)
            return 0
        # A running pooler that is not on the reachable address is a degraded one
        # (born in a boot-time address race). Reload cannot fix it — pgbouncer
        # never retries a listen_addr that failed to bind — so tear it down and
        # restart below. Wait for the address first; the terminate result is
        # checked so a survivor is reported as the real cause, not a generic
        # start failure (P7).
        if not _wait_for_reachable_bind_gated(cluster_secret):
            print(
                f"  ✗ reachable bind address {reachable_host()!r} is not assigned to any "
                f"local interface after {int(_BIND_WAIT_TIMEOUT_S)}s — the degraded "
                "pgbouncer cannot be restarted into a healthy double bind. On reboot "
                "this means the private network has not come up yet; retry `ava start` "
                "once it is.",
                file=sys.stderr,
            )
            return 1
        print(
            f"  ✗ pgbouncer is NOT listening on the reachable address "
            f"{reachable_host()!r} — it degraded to loopback-only (task #1288) and "
            "remote agent-runners cannot reach the pooled AVA_DB_URL. Reload cannot "
            "re-bind it; restarting the pooler",
            file=sys.stderr,
        )
        if not _terminate_verified(pid, label="pgbouncer"):
            print(
                f"  ✗ could not stop the degraded pooler (pid {pid}) — it survived the "
                "force kill; not starting a second pooler on the same port",
                file=sys.stderr,
            )
            return 1
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
    # -d daemonizes and returns immediately; wait for the listener to authenticate a
    # client (admin console — no backend, since the cluster role is provisioned later).
    for _ in range(60):
        if _admin_reachable(listen_port, role, db_admin_password):
            break
        time.sleep(0.1)
    else:
        print(
            f"  ✗ pgbouncer did not become ready on :{listen_port}; see {_logfile_path()}",
            file=sys.stderr,
        )
        return 1
    if not pgbouncer_public_listener_reachable(listen_port, role, cluster_secret):
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
    print(f"  ✓ pgbouncer started (127.0.0.1:{listen_port}, transaction pooling)")
    _report_backend_verification(listen_port, db_name, role, db_admin_password)
    return 0


def _report_backend_verification(
    listen_port: int, db_name: str, role: str, cluster_secret: str
) -> None:
    """After readiness, attempt ONE real pooled backend connection and say what
    happened. Admin readiness alone cannot see a broken backend route (e.g. a
    stale socket dir), so on an already-provisioned cluster this is the line
    that makes such a break loud at bring-up. On a fresh birth the role/db are
    provisioned AFTER the pooler comes up, so a failure here is expected then —
    reported as ⚠, never as success."""
    if _reachable(listen_port, db_name, role, cluster_secret):
        print("  ✓ pgbouncer backend verified (pooled SELECT 1)")
        return
    print(
        "  ⚠ pgbouncer backend not verifiable yet (role/db not provisioned yet, or the "
        "backend route is broken — `ava status` probes the pooled path end-to-end)",
        file=sys.stderr,
    )


def pgbouncer_reachable(listen_port: int, db_name: str, role: str, cluster_secret: str) -> bool:
    """`ava status` probe: does the pooler answer an authenticated SELECT 1."""
    return _reachable(listen_port, db_name, role, cluster_secret)


def pgbouncer_listener_reachable(listen_port: int, role: str, cluster_secret: str) -> bool:
    """Is the POOLER itself up — the admin-console probe, with no server hop.

    The watchdog healthcheck's question, and deliberately not
    `pgbouncer_reachable`'s: an end-to-end `SELECT 1` also fails when Postgres is
    down, and restarting the pooler is the wrong answer to that. This separates
    "the pooler process is gone" (repairable here, by `ensure_pgbouncer`) from
    "the pooler is fine and the backend behind it is not" (nothing this check
    should touch)."""
    return _admin_reachable(listen_port, role, cluster_secret)


def stop_pgbouncer() -> None:
    """Stop this cluster's PgBouncer (best-effort; a not-running pooler is a no-op).
    Counterpart of ensure_pgbouncer for `ava stop` / `ava cluster down`."""
    pid = _running_pid()
    if pid is None:
        return
    # SIGTERM, then VERIFY the process actually exits: pgbouncer runs daemonized
    # and can outlive the signal when it is mid-pool-drain (observed on a preview
    # teardown 2026-08-06 — `ava stop` printed "pgbouncer stopped" while the
    # process still held its socket and kept running). SIGKILL a straggler after
    # the grace period and report honestly either way — a stop must never CLAIM
    # success it did not verify.
    _terminate_verified(pid, label="pgbouncer")


def _terminate_verified(pid: int, *, label: str, timeout_s: float = 5.0) -> bool:
    """Ask `pid` to stop, wait up to `timeout_s`, force-kill a straggler, and report
    the verified outcome. A PID that is already gone counts as stopped (the race
    between the pidfile read and the signal is covered either way).

    Returns True iff the process is confirmed gone (or was already); False when it
    survived the force kill — the caller can then report the stop as incomplete
    instead of claiming success it did not verify (Task #965).

    **Every step goes through `shared.proc`, and that is the whole fix.** This was
    written in raw POSIX signals — `os.kill(pid, signal.SIGTERM)`, `os.kill(pid, 0)`
    as the liveness probe, `signal.SIGKILL` — each of which is exactly one of the
    spellings `shared.proc` exists to keep off a call site: on Windows `os.kill(pid,
    0)` does not probe, it calls TerminateProcess (and raised `[WinError 87]` here),
    and `signal.SIGKILL` is not defined at all (`process_alive` probes via psutil on
    Windows and `os.kill(pid, 0)` on POSIX). `_reap_orphan_listeners` calls this
    on **every** platform from inside `_do_stop`, so a Windows host raised out of the
    middle of its own stop the moment any orphan held a unit port — which is what
    killed win's 2026-08-12 11:40 self-update and, through the lease its aborted
    chain never cleared, the two rollouts after it.

    `PermissionError` — a pid this user may not touch, the POSIX twin of the same
    failure — is handled on all three legs rather than one: `process_alive` reads it
    as alive, and `request_stop` / `force_kill` return quietly instead of raising. It
    used to escape as an unhandled exception from whichever leg met it first. The
    outcome is now the honest one: nothing could be delivered, the process is still
    there, and the stop reports a survivor rather than claiming a success it cannot
    see."""
    import time

    from shared.proc import force_kill, request_stop

    if not process_alive(pid):
        print(f"  ✓ {label} stopped")
        return True
    request_stop(pid)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not process_alive(pid):
            print(f"  ✓ {label} stopped")
            return True
        time.sleep(_TERMINATE_POLL_S)
    force_kill(pid)
    time.sleep(_FORCE_KILL_SETTLE_S)
    if not process_alive(pid):
        print(f"  ⚠ {label} stopped (forced kill)")
        return True
    print(f"  ⚠ {label} survived the force kill — kill manually (pid {pid})", file=sys.stderr)
    return False


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
