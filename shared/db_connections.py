"""Postgres connection guards, URLs, direct connects, and sync pools."""

from typing import Any
from urllib.parse import urlsplit

import psycopg
from psycopg_pool import ConnectionPool

from shared.config import settings
from shared.config.data_plane import gateway_url_host, resolved_pool_size, sslmode_for_url
from shared.dotenv_boot import UNANCHORED_DB_SENTINEL
from shared.log import logger
from shared.url_secret import url_with_port


class UnanchoredHomeError(RuntimeError):
    """A DB connection was attempted from a dev checkout that resolved no home.

    The process's db_url is the unanchored sentinel rather than a real cluster
    database: this checkout is not the prod source, carries no `.ava_home`
    pointer, and AVA_HOME is unset, so home resolution fell back without a
    database (see shared/dotenv_boot.py). Raised instead of letting the
    connection silently reach the prod database the host .env points at.
    """


# TCP keepalive + connect timeout applied to every cluster Postgres connection —
# the psycopg/libpq mirror of shared/redis_client.py's `_RESILIENCE_KWARGS`. A
# laptop-grade runner that sleeps or changes networks wakes holding dead TCP
# flows; without these, a query already in flight on a half-dead socket hangs on
# the OS TCP-retransmit timeout (minutes, no application-level bound). ~30s idle,
# then a probe every 10s, 3 misses = dead (~60s), plus a 5s cap on establishing a
# new connection. These are libpq connection parameters (passed through psycopg's
# `**kwargs` into the conninfo), so they apply to the connect + every borrowed
# pool conn. Typed `dict[str, Any]` (like redis's `_RESILIENCE_KWARGS`) so
# `**`-unpacking into psycopg's typed keyword params doesn't trip the type checker.
#
# `connect_timeout` matters most where the connect is the *first* thing a process
# does: against a peer that black-holes packets (dropped, not ECONNREFUSED) an
# unbounded connect never errors, so the caller reads as "hung" rather than
# "failed". This constant is therefore the single definition of that posture, and
# is exported to the call sites that cannot go through `connect()` / `pool()`
# because they are parameterized on a URL rather than on settings: the agent's own
# pool kwargs (services/agent_host/pools.py), the boot-time schema assertion
# (shared/migrations.py:assert_schema_current), `ava.DB` (ava/_settings.py), the
# SDK shell-session index (ava/shell/sessions.py). Fail-fast behaviour is pinned
# by tests/shared/test_connect_fail_fast.py.
PG_KEEPALIVE_KWARGS: dict[str, Any] = {
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
    "connect_timeout": 5,
}

# Statement-level ceiling for every query through the sanctioned entry points
# (`shared.db.connect()` / `pool()`), appended as a libpq `options` string.
# The keepalives above bound a *dead* connection (~60s to detect); a *live but
# wedged* one (a lock wait, a planner path gone quadratic, a table bloat) would
# otherwise hold the query for as long as Postgres lets it — with no
# statement_timeout that is unbounded. 60s is far above any production query
# here (timeline reads are single-row checkpoint fetches; the heaviest admin
# queries stay well under) and far below the keepalive detection window, so a
# hung query fails fast with a clear OperationalError instead of pinning a
# gateway/daemon request for minutes.
#
# Deliberately NOT folded into PG_KEEPALIVE_KWARGS: the migration applier
# (cli/commands/migrations.py + the update/rollback wrappers in
# cli/commands/_update_git.py) dials `connect(direct=True, unbounded=True)` and
# its DDL runs may legitimately exceed 60s — the migration applier must stay
# unbounded. Every other connection point in the codebase goes through
# connect()/pool() (or, in the agent process, reuses this constant explicitly —
# see services/agent_host/pools.py / ava/_settings.py), so this one constant is the single
# statement-timeout definition.
PG_STATEMENT_TIMEOUT_OPTIONS = "-c statement_timeout=60000"
# The same ceiling as an explicit `SET` — the delivery path that works THROUGH
# PgBouncer: the pooler drops the libpq `options` startup parameter
# (ignore_startup_parameters), and `track_extra_parameters` cannot deliver
# statement_timeout either (PgBouncer only tracks parameters Postgres reports
# to clients, GUC_REPORT, and statement_timeout is not one of them — verified
# against 1.25 source + live probe). A client-side SET is forwarded to the
# backend like any query and sticks — pgbouncer transaction pooling does NOT
# reset backend session state between clients on the ordinary release path
# (server_reset_query_always=0, the 2026-09-03 ruling: always=1 fired after
# every transaction and its DISCARD ALL wiped this client-side SET; the
# 2026-09-02 P0 pooled read-only pollution rode the pre-fix era). The uniform
# ceiling
# therefore holds because every sanctioned pooled entry point restores the
# baseline session on use (see _restore_pooled_session).
# `connect()` / `pool()` issue it on every pooled dial/borrow;
# `cli/commands/_pgbouncer.py` also runs it as the pooler's `connect_query` so
# every pooled backend is bounded at birth regardless of the client's code path.
PG_STATEMENT_TIMEOUT_SET_SQL = "SET statement_timeout = 60000"
# The full kwargs the sanctioned entry points pass to psycopg, exported so the
# runtime connection points (services/agent_host/pools.py pool, ava/_settings.py ava.DB,
# agent/db.py CAS) can apply the same statement ceiling without re-deriving it.
# `options` is the direct-connection delivery path (Postgres parses it itself);
# pooled dials additionally run PG_STATEMENT_TIMEOUT_SET_SQL (see connect/pool).
PG_STATEMENT_TIMEOUT_KWARGS: dict[str, Any] = {
    **PG_KEEPALIVE_KWARGS,
    "options": PG_STATEMENT_TIMEOUT_OPTIONS,
}

# The session-level scrub that makes a pooled backend safe to borrow. RESET ALL
# clears every session GUC another borrower may have left behind (the 2026-09-02
# P0: a polluter's `SET default_transaction_read_only = on` on a pooled
# connection leaked onto shared backends and 500'd the message/schedule-stop
# APIs' writes) — then the statement ceiling is re-applied, because RESET ALL
# also clears the pooler connect_query's birth-time SET. Two statements, always
# together: a restore that reset without re-bounding would silently drop the F7
# ceiling for the next borrower.
PG_POOLED_BASELINE_RESTORE_SQL = ("RESET ALL", PG_STATEMENT_TIMEOUT_SET_SQL)


def _restore_pooled_session(conn: psycopg.Connection) -> None:
    """Scrub a pooled connection back to its baseline session state.

    PgBouncer transaction pooling hands any backend to any client transaction
    and never resets session state on the ordinary release path (measured on
    1.25.2, 2026-09-02 P0), so a borrower can inherit another client's session
    GUCs — `default_transaction_read_only=on` makes every write in the
    transaction fail with ReadOnlySqlTransaction. Restoring the baseline is the
    client-side fix: RESET ALL + the statement ceiling, run as one transaction
    at every sanctioned pooled entry point — on a fresh dial (`connect()`), on
    a pool backend's creation (`configure`), and on every borrow (`check`).
    Each restore also heals the shared backend it lands on, so a polluted
    backend stops hurting the next borrower.

    Used as the psycopg_pool `configure`/`check` hook and on pooled dials:
    PgBouncer drops the `options` startup parameter, so SQL is the one path
    that reaches the backend. The hooks must leave the connection IDLE
    (psycopg_pool discards a connection its callback leaves mid-transaction);
    `commit()` ends the restore transaction and is a no-op on an autocommit
    connection. On a dead connection the execute raises, which psycopg_pool
    treats as a failed check — the connection is discarded and replaced, the
    same discard path `ConnectionPool.check_connection` feeds.
    """
    conn.execute(PG_POOLED_BASELINE_RESTORE_SQL[0])
    conn.execute(PG_POOLED_BASELINE_RESTORE_SQL[1])
    conn.commit()


async def _restore_pooled_session_async(conn: psycopg.AsyncConnection) -> None:
    """Async twin of `_restore_pooled_session` for the host pools.

    The agent's `AsyncConnectionPool` (services/agent_host/pools.py) is built directly rather
    than through `pool()`, so its per-borrow liveness check doubles as the
    baseline scrub here — same RESET ALL + statement ceiling, same discard-on-
    failure semantics (psycopg_pool replaces a connection whose check raises).
    """
    await conn.execute(PG_POOLED_BASELINE_RESTORE_SQL[0])
    await conn.execute(PG_POOLED_BASELINE_RESTORE_SQL[1])
    await conn.commit()


# psycopg_pool's own `ConnectionPool(timeout=...)` default, restated as a name so
# `pool()` can pass it explicitly (a `float | None` sentinel forwarded via `**kwargs`
# is untypeable against ConnectionPool's overloads). Callers that must not block for
# this long pass their own — see `pool`'s docstring.
DEFAULT_POOL_TIMEOUT_S = 30.0


def _guard_db_url(url: str) -> str:
    """Refuse the unanchored sentinel; return the url otherwise. The single point
    every sanctioned connection passes through, so the prod-DB footgun is caught
    once here rather than at each call site.

    Raises:
        UnanchoredHomeError: url is the unanchored sentinel.
    """
    if url == UNANCHORED_DB_SENTINEL:
        raise UnanchoredHomeError(
            "refusing to open a DB connection: AVA_DB_URL is the never-dialed "
            "placeholder. Two ways to land here: this checkout resolved no AVA_HOME "
            "(not the prod source, no .ava_home pointer, AVA_HOME unset) — run "
            "`ava start` in this worktree (writes its .ava_home pointer) or export "
            "AVA_HOME=<unit home>; or this process built settings-lite "
            "(AVA_CONFIG_FETCH=skip, the maintenance verbs' gateway-down mode) and "
            "this operation needs the cluster config a fetch would have provided."
        )
    return url


def direct_db_url() -> str:
    """The admin-plane Postgres URL: this cluster's `AVA_DB_URL` never routed
    through PgBouncer.

    `AVA_DB_URL` carries the pooler listener port whenever pooling is enabled
    (the one-URL design: a normal process dials it as-is and never knows the
    pooler exists), so the admin plane — migrations (SESSION advisory locks),
    pg_dump (needs a real backend session), provisioning, auth probes — must
    derive the direct Postgres URL instead. The derivation swaps ONLY the port,
    from the pooler listener to the cluster's direct Postgres port, both read
    from the host-level registry record: the pooler is co-located with its
    Postgres on the gateway box, so host and credentials are identical. When the
    URL does not name the pooler (pooling off, an operator stand-in URL) it is
    returned verbatim — already direct.

    The record lookup matches the URL's host:port against EVERY local registry
    record, not just this home's (a URL can legitimately name another local
    cluster's pooler), and only when the URL names this box — a remote host's
    record is not local, so a split agent-runner's gateway-pointing URL cannot
    be resolved here. In that case the URL is returned as-is with a WARNING:
    the "direct" dial silently routes through the gateway's pooler (the
    migration authority model keeps a runner from mutating the schema in
    practice, but the exemption itself is unavailable and must not be silent).

    The unanchored sentinel passes through byte-identical (the connect guard
    matches it byte-for-byte), and a host with no registry record falls back to
    `AVA_DB_URL` as-is rather than guessing.
    """
    from shared.cluster import load_registry, record_pgbouncer_port, record_postgres_port
    from shared.machine import reachable_host
    from shared.netutil import is_loopback_host

    url = settings.data_plane.db_url
    if url == UNANCHORED_DB_SENTINEL:
        return url
    try:
        parts = urlsplit(url)
        port = parts.port
        host = (parts.hostname or "").lower()
    except ValueError:
        return url
    if port is None:
        return url
    # Only a loopback or self-named host can be resolved against the LOCAL
    # registry — a remote host's record lives on that box, not here.
    if is_loopback_host(host) or host == reachable_host().lower():
        for rec in load_registry().values():
            if port == record_pgbouncer_port(rec):
                # URL names this cluster's pooler -> swap to its direct pg port.
                return url_with_port(url, record_postgres_port(rec))
            if port == record_postgres_port(rec):
                # URL already names Postgres (pooling off / a stand-in on a
                # cluster port) -> already direct.
                return url
    # No local record explains this URL's port: a local operator stand-in, or a
    # split runner naming the gateway's pooler. A remote/SaaS plane (Task #1752)
    # is direct by definition — no local pooler exists — so it dials silently.
    if settings.data_plane.pgbouncer_enabled and (
        is_loopback_host(host) or host == reachable_host().lower() or host == gateway_url_host()
    ):
        logger.warning(
            "direct_db_url: AVA_DB_URL names {host}:{port}, which no local registry "
            "record's PgBouncer or Postgres port matches (a split agent-runner's "
            "URL names the gateway's pooler, resolvable only on the gateway box). "
            "Returning AVA_DB_URL as-is — the admin-plane dial routes through "
            "PgBouncer; the migration authority model keeps a non-gateway host "
            "from mutating the schema, but the direct exemption is unavailable here.",
            host=host,
            port=port,
        )
    return url


def connect(
    *,
    autocommit: bool = False,
    direct: bool = False,
    unbounded: bool = False,
) -> psycopg.Connection:
    """Open a new connection to the cluster Postgres.

    The single entry point for one-off connections, so call sites stop reading
    `settings.data_plane.db_url` by hand. By default this dials
    `settings.data_plane.db_url` — the cluster's one access URL (PgBouncer when
    enabled, direct Postgres when off; the port is chosen at URL generation, so
    the dial is a plain connect). The returned connection is a context manager:
    `with shared.db.connect() as conn: ...`. `autocommit` is passed through for
    the DDL / advisory-lock call sites that need it.

    `direct=True` forces the connection to the real Postgres, bypassing PgBouncer
    (the URL is derived from the registry record — `direct_db_url`). The admin
    plane MUST use it wherever a transaction pooler would break correctness —
    session-level state that outlives a single transaction: the migration applier
    holds a **session** advisory lock (`pg_advisory_lock`, in shared.migrations)
    across its whole apply loop, which transaction pooling would silently drop.
    `prepare_threshold=None` disables server-side prepared statements so the same
    connection is safe across the different backends a transaction pooler hands
    out (a prepared statement made on one backend does not exist on the next); it
    is a harmless no-op on a direct connection. psycopg3 semantics: `None` = never
    prepare; `0` = prepare on the FIRST execution (the opposite of this docstring's
    old claim — with the pooler untracked, every fresh connection prepares its
    first statement as the same `_pg3_0` name, and two of them on one backend
    raise DuplicatePreparedStatement).

    On a non-direct dial the session is scrubbed back to baseline — RESET ALL
    plus the statement ceiling as an explicit `SET` (PgBouncer drops the
    `options` startup parameter and never resets backend session state between
    clients, so a borrowed backend may carry another client's session GUCs; see
    _restore_pooled_session) — unless `unbounded=True`.

    `unbounded=True` (admin plane only — the migration applier) drops the
    statement-timeout ceiling entirely: migration DDL runs may legitimately
    exceed 60s (a large-table rebuild, a partition backfill), and the applier
    must stay unbounded. On a direct dial this means no `options` parameter; on
    a pooled dial the pooler's `connect_query` still bounds the backend at
    birth, so an unbounded pooled dial is not truly unbounded — pair it with
    `direct=True` wherever the ceiling must actually be off.

    Raises:
        UnanchoredHomeError: the resolved db_url is the unanchored sentinel.
    """
    dp = settings.data_plane
    url = dp.db_url if not direct else direct_db_url()
    sslmode = sslmode_for_url(url, dp.db_sslmode)
    conn = psycopg.connect(
        _guard_db_url(url),
        autocommit=autocommit,
        prepare_threshold=None,
        # sslmode only when the URL is silent (config is the fallback, never an override).
        **({"sslmode": sslmode} if sslmode else {}),
        **({} if unbounded else PG_STATEMENT_TIMEOUT_KWARGS),
    )
    if not direct and not unbounded:
        # Pooled dial: PgBouncer dropped the `options` startup parameter above
        # AND does not reset backend session state between clients, so scrub
        # the session back to baseline (RESET ALL + statement ceiling) before
        # handing the connection to the caller — a backend polluted by another
        # client's session-level SET must not reach this caller's writes
        # (2026-09-02 P0; direct dials already got the ceiling via options and
        # own their backend exclusively, so no scrub is needed there).
        _restore_pooled_session(conn)
    return conn


def pool(
    *,
    min_size: int | None = None,
    max_size: int | None = None,
    direct: bool = False,
    timeout: float = DEFAULT_POOL_TIMEOUT_S,
    check_connections: bool = False,
    autocommit: bool = False,
    row_factory: Any | None = None,
) -> ConnectionPool:
    """Open a ConnectionPool on the cluster Postgres (opened eagerly).

    Long-lived callers dial the cluster's one access URL (PgBouncer when
    enabled) unless `direct=True`; the caller owns and closes the pool.

    `min_size` / `max_size` default to the config fields (themselves the
    historical 1 / 2), so a remote/SaaS plane tunes its pool from config; an
    explicit caller value always wins.

    `autocommit` and `row_factory` apply to every borrowed connection. They
    exist for short-lived read pools whose callers must preserve a direct-read
    contract while still inheriting the shared pool's transport settings.

    This only sanctioned sync-pool builder applies `prepare_threshold=None`,
    `PG_KEEPALIVE_KWARGS`; `scripts/lint_pool_keepalives.py` rejects bypasses.

    `timeout` bounds how long `pool.connection()` waits for a connection before
    raising `PoolTimeout`. It is worth knowing about, not just tuning: `open=True`
    does NOT fail here when the DB is unreachable — the pool keeps retrying in
    background workers, so a dead data plane surfaces only at the first
    `pool.connection()`, one full timeout later. A caller on a bounded schedule (the
    watchdog's sequential
    tick, where a long wait delays every check behind it) must pass a short one;
    long-lived daemon pools keep the default.

    Pooled pools restore the baseline session (RESET ALL + the statement
    ceiling) at backend creation AND on every borrow — pgbouncer never resets
    backend session state between clients (2026-09-02 P0), so the borrow-time
    scrub is what keeps a backend polluted by another client's session-level
    SET from failing this borrower's writes. See _restore_pooled_session.

    Raises:
        UnanchoredHomeError: the resolved db_url is the unanchored sentinel.
    """
    dp = settings.data_plane
    url = dp.db_url if not direct else direct_db_url()
    min_size, max_size = resolved_pool_size(
        min_size, max_size, dp.db_pool_min_size, dp.db_pool_max_size
    )
    sslmode = sslmode_for_url(url, dp.db_sslmode)
    connection_kwargs: dict[str, Any] = {
        "prepare_threshold": None,
        **({"sslmode": sslmode} if sslmode else {}),
        **PG_STATEMENT_TIMEOUT_KWARGS,
    }
    if autocommit:
        connection_kwargs["autocommit"] = True
    if row_factory is not None:
        connection_kwargs["row_factory"] = row_factory
    return ConnectionPool(
        _guard_db_url(url),
        min_size=min_size,
        max_size=max_size,
        open=True,
        timeout=timeout,
        kwargs=connection_kwargs,
        # A pooled pool's backends are shared, never reset by pgbouncer, and
        # handed to any borrower — so both hooks restore the baseline session
        # (RESET ALL + statement ceiling): `configure` at backend creation,
        # `check` on EVERY borrow. The borrow-time restore is the load-bearing
        # half: it scrubs the backend the borrower is about to use (pgbouncer
        # prefers handing a client the backend it used last), so a backend
        # polluted by another client's session-level SET cannot fail this
        # borrower's writes (2026-09-02 P0: message/schedule-stop 500s). It
        # doubles as the checkout-time dead-connection check (a dead connection
        # raises and psycopg_pool discards + replaces it — Task #1027's
        # eviction, now implicit for pooled pools), at the cost of two
        # statements per checkout. Direct pools own their backends exclusively:
        # no scrub needed, the ceiling already arrived via `options`, and the
        # optional `check_connections` flag keeps its Task #1027 meaning.
        configure=_restore_pooled_session if not direct else None,
        check=(
            _restore_pooled_session
            if not direct
            else (ConnectionPool.check_connection if check_connections else None)
        ),
    )
