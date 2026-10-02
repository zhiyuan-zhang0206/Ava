"""Postgres connection guards, URLs, direct connects, and sync + async pools."""

from typing import Any, LiteralString
from urllib.parse import urlsplit

import psycopg
from psycopg.rows import tuple_row
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from base.db.code_version_gate import application_name, min_read_due, observe_minimum
from base.db.config import DbConfig, db_config_from_settings
from base.host.env.dotenv_boot import PLACEHOLDER_DB_URL
from base.host.net.url_secret import url_with_port
from base.log import logger

# `base.config.data_plane`'s helpers are imported inside the dial functions
# that call them (`connect` / `pool` / `direct_db_url`), never at module level:
# this module is on the exec child's boot path (`agent.db` -> `base.db` ->
# here) and a module-level import would pull the full eager config chain
# (pydantic_settings included) back into a lite boot (task #3621). Dialing
# upgrades the config chain anyway (the first `settings.data_plane` read), so
# the deferred import is a sys.modules hit at call time.


class PlaceholderDbUrlError(RuntimeError):
    """A DB connection was attempted by a process that holds no cluster connection facts.

    The process's db_url is the never-dialed placeholder rather than a real
    cluster database: its home declares none (no `.env` with AVA_DB_URL), or it
    built settings-lite and never fetched them (see base/config/_lite.py).
    Raised instead of dialing anything.
    """


class NoDatabaseAuthorityError(RuntimeError):
    """This process holds no database login for its write-generation home.

    The home's `.env` carries only the credential-free endpoint; logins are
    delivered by the root launcher, or to an operator process running the
    home's admitted runtime (`base.host.env.dotenv_boot._deliver_operator_authority`).
    Raised at the dial instead of an opaque authentication failure.
    """


# TCP keepalive + connect timeout applied to every cluster Postgres connection —
# the psycopg/libpq mirror of base/events/live/redis_client.py's `RESILIENCE_KWARGS`. A
# laptop-grade runner that sleeps or changes networks wakes holding dead TCP
# flows; without these, a query already in flight on a half-dead socket hangs on
# the OS TCP-retransmit timeout (minutes, no application-level bound). ~30s idle,
# then a probe every 10s, 3 misses = dead (~60s), plus a 5s cap on establishing a
# new connection. These are libpq connection parameters (passed through psycopg's
# `**kwargs` into the conninfo), so they apply to the connect + every borrowed
# pool conn. Typed `dict[str, Any]` (like redis's `RESILIENCE_KWARGS`) so
# `**`-unpacking into psycopg's typed keyword params doesn't trip the type checker.
#
# `connect_timeout` matters most where the connect is the *first* thing a process
# does: against a peer that black-holes packets (dropped, not ECONNREFUSED) an
# unbounded connect never errors, so the caller reads as "hung" rather than
# "failed". This constant is therefore the single definition of that posture:
# `connect()` / `connect_url()` / `pool()` / `async_pool()` apply it, bounded or
# not (a caller may shorten `connect_url`'s connect timeout for a probe).
# Fail-fast behaviour is pinned by ava/tests/test_connect_fail_fast.py.
PG_KEEPALIVE_KWARGS: dict[str, Any] = {
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
    "connect_timeout": 5,
}

# Statement-level ceiling for every query through the sanctioned entry points
# (`base.db.connect()` / `pool()`), appended as a libpq `options` string.
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
# (cli/commands/lifecycle/migrations.py) dials `connect(direct=True, unbounded=True)` and
# its DDL runs may legitimately exceed 60s — the migration applier must stay
# unbounded. The sanctioned entry points deliver this one ceiling — as `options`
# on a direct dial, as PG_STATEMENT_TIMEOUT_SET_SQL on a pooled one — so it is
# the single statement-timeout definition.
_STATEMENT_TIMEOUT_MS: LiteralString = "60000"
PG_STATEMENT_TIMEOUT_OPTIONS = f"-c statement_timeout={_STATEMENT_TIMEOUT_MS}"
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
# `cli/commands/data_plane/pgbouncer.py` also runs it as the pooler's `connect_query` so
# every pooled backend is bounded at birth regardless of the client's code path.
PG_STATEMENT_TIMEOUT_SET_SQL = f"SET statement_timeout = {_STATEMENT_TIMEOUT_MS}"
# The full kwargs the bounded dials (`connect()` / `connect_url()` / `pool()`)
# pass to psycopg. `options` is the direct-connection delivery path (Postgres
# parses it itself); pooled dials additionally run PG_STATEMENT_TIMEOUT_SET_SQL
# (see connect/pool).
PG_STATEMENT_TIMEOUT_KWARGS: dict[str, Any] = {
    **PG_KEEPALIVE_KWARGS,
    "options": PG_STATEMENT_TIMEOUT_OPTIONS,
}

# The session-level scrub that makes a pooled backend safe to borrow. RESET ALL
# clears every session GUC another borrower may have left behind (the 2026-09-02
# P0: a polluter's `SET default_transaction_read_only = on` on a pooled
# connection leaked onto shared backends and 500'd the message/schedule-stop
# APIs' writes) — then what RESET ALL also cleared is re-applied: the statement
# ceiling (the pooler connect_query's birth-time SET) and this connection's
# `application_name` (PgBouncer takes a client's RESET of a parameter it tracks
# as that client's own new value, and would list the connection as unnamed).
# Two statements, always together: a restore that reset without re-bounding
# would silently drop the F7 ceiling for the next borrower. The second is
# `set_config(..., false)`, `SET` as an expression, one placeholder: the name.
_PG_POOLED_SESSION_SQL: LiteralString = (
    f"set_config('statement_timeout', '{_STATEMENT_TIMEOUT_MS}', false), "
    "set_config('application_name', %s, false)"
)
PG_POOLED_BASELINE_RESTORE_SQL: tuple[LiteralString, LiteralString] = (
    "RESET ALL",
    f"SELECT {_PG_POOLED_SESSION_SQL}",
)
# The second statement on the borrow that also reads the cluster's minimum code
# version (`base.db.code_version_gate`): same round trip, one more column. A
# cluster with no `deployment_state` row has recorded no minimum: 0, never a refusal.
PG_POOLED_RESTORE_WITH_MIN_SQL: LiteralString = (
    f"SELECT {_PG_POOLED_SESSION_SQL}, "  # noqa: S608 — module constants only, no caller input
    "COALESCE((SELECT min_code_version FROM public.deployment_state WHERE id = 1), 0)"
)


def _restore_pooled_session(conn: psycopg.Connection) -> None:
    """Scrub a pooled connection back to its baseline session state.

    PgBouncer transaction pooling hands any backend to any client transaction
    and never resets session state on the ordinary release path, so a borrower
    can inherit another client's session GUCs. PgBouncer >= 1.26 isolates the
    parameters PostgreSQL reports to clients (default_transaction_read_only,
    the 2026-09-02 P0 vector), but unreported ones still leak — another
    client's `search_path` fails every unqualified statement, its
    `statement_timeout = 0` lifts the F7 ceiling — and a remote-managed data
    plane's pooler may isolate nothing. Restoring the baseline is the
    client-side fix: RESET ALL + the statement ceiling, run as one transaction
    at every sanctioned pooled entry point — on a fresh dial (`connect()`), on
    a pool backend's creation (`configure`), and on every borrow (`check`).
    Each restore also heals the shared backend it lands on, so a polluted
    backend stops hurting the next borrower. The same statement re-names the
    connection and, at most every `MIN_REFRESH_INTERVAL_S`, reads the cluster's
    minimum code version: a process below it exits here
    (`base.db.code_version_gate`).

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
    name = (application_name(),)
    if min_read_due():
        # tuple_row: a pool built with a dict row_factory must not change the shape read.
        with conn.cursor(row_factory=tuple_row) as cur:
            cur.execute(PG_POOLED_RESTORE_WITH_MIN_SQL, name)
            row = cur.fetchone()
        if row is None:
            raise RuntimeError("the restore statement returned no row")
        observe_minimum(int(row[2]))
    else:
        conn.execute(PG_POOLED_BASELINE_RESTORE_SQL[1], name)
    conn.commit()


async def _restore_pooled_session_async(conn: psycopg.AsyncConnection) -> None:
    """Async twin of `_restore_pooled_session`: `async_pool()`'s per-borrow check.

    The liveness check doubles as the baseline scrub — same RESET ALL +
    statement ceiling, same discard-on-failure semantics (psycopg_pool replaces
    a connection whose check raises), same name and code-version read.
    """
    await conn.execute(PG_POOLED_BASELINE_RESTORE_SQL[0])
    name = (application_name(),)
    if min_read_due():
        async with conn.cursor(row_factory=tuple_row) as cur:
            await cur.execute(PG_POOLED_RESTORE_WITH_MIN_SQL, name)
            row = await cur.fetchone()
        if row is None:
            raise RuntimeError("the restore statement returned no row")
        observe_minimum(int(row[2]))
    else:
        await conn.execute(PG_POOLED_BASELINE_RESTORE_SQL[1], name)
    await conn.commit()


# psycopg_pool's own `ConnectionPool(timeout=...)` default, restated as a name so
# `pool()` can pass it explicitly (a `float | None` sentinel forwarded via `**kwargs`
# is untypeable against ConnectionPool's overloads). Callers that must not block for
# this long pass their own — see `pool`'s docstring.
DEFAULT_POOL_TIMEOUT_S = 30.0


def _statement_kwargs(url: str) -> dict[str, Any]:
    """The statement ceiling, after any startup options `url` carries itself: a
    psycopg keyword argument would otherwise replace the URL's `options`."""
    from psycopg.conninfo import conninfo_to_dict

    own = conninfo_to_dict(url).get("options")
    if not own:
        return PG_STATEMENT_TIMEOUT_KWARGS
    return {**PG_STATEMENT_TIMEOUT_KWARGS, "options": f"{own} {PG_STATEMENT_TIMEOUT_OPTIONS}"}


def _refuse_placeholder(url: str) -> None:
    """Refuse the never-dialed placeholder, whichever entry point was handed it.

    Raises:
        PlaceholderDbUrlError: url is the placeholder.
    """
    if url == PLACEHOLDER_DB_URL:
        raise PlaceholderDbUrlError(
            "refusing to open a DB connection: AVA_DB_URL is the never-dialed "
            "placeholder. Two ways to land here: this process's home "
            "($AVA_HOME, else ~/.ava) has no .env declaring a cluster — start a "
            "cluster there, or set AVA_HOME to a home that has one; or this "
            "process built settings-lite "
            "(AVA_CONFIG_FETCH=skip, the maintenance verbs' gateway-down mode) and "
            "this operation needs the cluster config a fetch would have provided."
        )


def _guard_db_url(url: str) -> str:
    """Refuse the placeholder URL and an undelivered credential-free endpoint;
    return the url otherwise. The single point every settings-resolved connection
    passes through, so both footguns are caught once here rather than at each
    call site.

    Raises:
        PlaceholderDbUrlError: url is the placeholder.
        NoDatabaseAuthorityError: this home keeps a write-generation ledger, no
            login was delivered to this process, and url carries no password.
    """
    _refuse_placeholder(url)
    from base.host.env import dotenv_boot

    refusal = dotenv_boot.db_authority_refusal()
    if refusal is not None:
        try:
            password = urlsplit(url).password
        except ValueError:
            password = None
        if not password:
            raise NoDatabaseAuthorityError(
                f"refusing to dial the credential-free database endpoint: {refusal}"
            )
    return url


def direct_db_url(config: DbConfig | None = None) -> str:
    """The admin-plane Postgres URL: this cluster's `AVA_DB_URL` never routed
    through PgBouncer.

    `AVA_DB_URL` carries the pooler listener port whenever pooling is enabled
    (the one-URL design: a normal process dials it as-is and never knows the
    pooler exists), so the admin plane — migrations (SESSION advisory locks),
    pg_dump (needs a real backend session), provisioning, auth probes — must
    derive the direct Postgres URL instead. The derivation swaps ONLY the port,
    from the pooler listener to the cluster's direct Postgres port, both read
    from this home's own cluster record: the pooler is co-located with its
    Postgres on the gateway box, so host and credentials are identical. When the
    URL does not name the pooler (pooling off, an operator stand-in URL) it is
    returned verbatim — already direct.

    The record lookup matches the URL's host:port against this home's record
    only, and only when the URL names this box. A home knows no other cluster, so
    a unit whose URL names another home's pooler (a split agent-runner on this or
    another box) cannot be resolved here. In that case the URL is returned as-is
    with a WARNING:
    the "direct" dial silently routes through the gateway's pooler (the
    migration authority model keeps a runner from mutating the schema in
    practice, but the exemption itself is unavailable and must not be silent).

    The placeholder URL passes through byte-identical (the connect guard
    matches it byte-for-byte), and a home with no record (no gateway capability)
    keeps `AVA_DB_URL` as-is rather than guessing.
    """
    return _direct_url(config or db_config_from_settings())


def _direct_url(cfg: DbConfig) -> str:
    """`direct_db_url` for one resolved config."""
    from base.cluster import get_record
    from base.cluster.machine import reachable_host
    from base.config.data_plane import gateway_url_host
    from base.host.net.predicates import is_loopback_host
    from base.paths import ava_home

    url = cfg.db_url
    if url == PLACEHOLDER_DB_URL:
        return url
    try:
        parts = urlsplit(url)
        port = parts.port
        host = (parts.hostname or "").lower()
    except ValueError:
        return url
    if port is None:
        return url
    # Only a loopback or self-named host can be resolved against this home's
    # record — a remote host's record lives on that box, not here.
    rec = (
        get_record(ava_home())
        if is_loopback_host(host) or host == reachable_host().lower()
        else None
    )
    if rec is not None:
        if port == rec.ports["pgbouncer"]:
            # URL names this cluster's pooler -> swap to its direct pg port.
            return url_with_port(url, rec.ports["postgres"])
        if port == rec.ports["postgres"]:
            # URL already names Postgres (pooling off / a stand-in on a cluster
            # port) -> already direct.
            return url
    # This home's record does not explain the URL's port: a local operator
    # stand-in, or a split runner naming the gateway's pooler. A remote/SaaS plane (Task #1752)
    # is direct by definition — no local pooler exists — so it dials silently.
    if cfg.pgbouncer_enabled and (
        is_loopback_host(host) or host == reachable_host().lower() or host == gateway_url_host()
    ):
        logger.warning(
            "direct_db_url: AVA_DB_URL names {host}:{port}, which this home's record "
            "does not name as its PgBouncer or Postgres port (a split agent-runner's "
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
    config: DbConfig | None = None,
) -> psycopg.Connection:
    """Open a new connection to the cluster Postgres.

    The single entry point for one-off connections, so call sites stop reading
    `settings.data_plane.db_url` by hand. By default this dials the
    `db_url` of `DbConfig` (built from the live settings unless `config` names
    one; a `Database` handle always passes its own) — the cluster's one access URL (PgBouncer when
    enabled, direct Postgres when off; the port is chosen at URL generation, so
    the dial is a plain connect). The returned connection is a context manager:
    `with base.db.connect() as conn: ...`. `autocommit` is passed through for
    the DDL / advisory-lock call sites that need it.

    `direct=True` forces the connection to the real Postgres, bypassing PgBouncer
    (the URL is derived from the registry record — `direct_db_url`). The admin
    plane MUST use it wherever a transaction pooler would break correctness —
    session-level state that outlives a single transaction: the migration applier
    holds a **session** advisory lock (`pg_advisory_lock`, in base.deploy.schema.migrations)
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
    must stay unbounded. The keepalives and connect timeout stay: a long DDL on
    a remote link is exactly the flow a dead peer would otherwise pin. On a
    direct dial this means no `options` parameter; on a pooled dial the
    pooler's `connect_query` still bounds the backend at birth, so an unbounded
    pooled dial is not truly unbounded — pair it with `direct=True` wherever the
    ceiling must actually be off.

    Raises:
        PlaceholderDbUrlError: the resolved db_url is the placeholder.
        NoDatabaseAuthorityError: this home keeps a write-generation ledger and
            the resolved db_url is a credential-free endpoint this process was
            given no login for (see `_guard_db_url`).
    """
    from base.config.data_plane import sslmode_for_url

    cfg = config or db_config_from_settings()
    url = _guard_db_url(cfg.db_url if not direct else direct_db_url(cfg))
    sslmode = sslmode_for_url(url, cfg.db_sslmode)
    conn = psycopg.connect(
        url,
        autocommit=autocommit,
        prepare_threshold=None,
        # sslmode only when the URL is silent (config is the fallback, never an override).
        **({"sslmode": sslmode} if sslmode else {}),
        # Through the pooler the connection names its process and code version,
        # so PgBouncer's SHOW CLIENTS shows who holds it.
        **({} if direct else {"application_name": application_name()}),
        **_transport_kwargs(url, unbounded=unbounded),
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


def connect_url(
    url: str,
    *,
    autocommit: bool = False,
    unbounded: bool = False,
    connect_timeout: int = PG_KEEPALIVE_KWARGS["connect_timeout"],
) -> psycopg.Connection:
    """Open one connection to an explicit Postgres target the caller names.

    `connect()` dials the cluster's own URL from settings; this is the admin
    plane's door for every other target — a dump source, a replication login
    under test, a scratch instance a restore check provisions, the pooler's
    admin console, a URL a probe or boot check is handed. The caller owns the
    target: the URL carries its host, credential, database, startup options and
    `sslmode`, and nothing here reads settings, so no cluster login is
    substituted and the configured `AVA_DB_SSLMODE` is not injected. (This
    module still resolves a home and imports settings at load, so a process
    that must do neither — the restricted restore worker, the throwaway-Postgres
    tooling — dials on its own; see the `postgres-dial` allowed map in
    scripts/structure/locality.py.)

    The door owns the transport posture, the same as `connect()`'s:
    `prepare_threshold=None`, `PG_KEEPALIVE_KWARGS`, and the statement ceiling
    appended after any startup options the URL carries itself. `connect_timeout`
    bounds establishing the connection (a short probe passes its own);
    `unbounded=True` drops the statement ceiling for a caller whose statements
    may legitimately run long (DDL, full-table reads of a restored copy), and
    keeps the keepalives. The session is never scrubbed: an explicit target is
    a backend the caller dials directly, not a borrowed pooled one (a probe of
    the cluster's pooled URL runs only read-only statements).

    Raises:
        PlaceholderDbUrlError: url is the placeholder.
    """
    _refuse_placeholder(url)
    transport: dict[str, Any] = {
        **_transport_kwargs(url, unbounded=unbounded),
        "connect_timeout": connect_timeout,
    }
    return psycopg.connect(url, autocommit=autocommit, prepare_threshold=None, **transport)


def _transport_kwargs(url: str, *, unbounded: bool) -> dict[str, Any]:
    """Keepalives always; the statement ceiling unless the dial is unbounded."""
    return PG_KEEPALIVE_KWARGS if unbounded else _statement_kwargs(url)


def pool(
    *,
    min_size: int | None = None,
    max_size: int | None = None,
    direct: bool = False,
    timeout: float = DEFAULT_POOL_TIMEOUT_S,
    check_connections: bool = False,
    autocommit: bool = False,
    row_factory: Any | None = None,
    config: DbConfig | None = None,
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
    `PG_KEEPALIVE_KWARGS`; the structure gate's `postgres-dial` rule rejects bypasses.

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
        PlaceholderDbUrlError: the resolved db_url is the placeholder.
        NoDatabaseAuthorityError: this home keeps a write-generation ledger and
            the resolved db_url is a credential-free endpoint this process was
            given no login for (see `_guard_db_url`).
    """
    from base.config.data_plane import resolved_pool_size, sslmode_for_url

    cfg = config or db_config_from_settings()
    url = _guard_db_url(cfg.db_url if not direct else direct_db_url(cfg))
    min_size, max_size = resolved_pool_size(
        min_size, max_size, cfg.db_pool_min_size, cfg.db_pool_max_size
    )
    sslmode = sslmode_for_url(url, cfg.db_sslmode)
    connection_kwargs: dict[str, Any] = {
        "prepare_threshold": None,
        **({"sslmode": sslmode} if sslmode else {}),
        **({} if direct else {"application_name": application_name()}),
        **_statement_kwargs(url),
    }
    if autocommit:
        connection_kwargs["autocommit"] = True
    if row_factory is not None:
        connection_kwargs["row_factory"] = row_factory
    return ConnectionPool(
        url,
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


def async_pool(
    pool_class: type[AsyncConnectionPool[psycopg.AsyncConnection]],
    *,
    min_size: int,
    max_size: int,
    timeout: float,
    config: DbConfig | None = None,
    **pool_kwargs: Any,
) -> AsyncConnectionPool[psycopg.AsyncConnection]:
    """An unopened async pool on the cluster's access URL (the agent host's pools).

    The async twin of `pool()`. It is returned closed because the caller's event
    loop opens it (`await pool.open()`). `pool_class` is the `AsyncConnectionPool`
    subclass to build (the host's `agent.db.LoggingConnectionPool`, which times
    every borrow) so this module never imports the agent layer; `pool_kwargs`
    are that subclass's own arguments.

    The transport posture is fixed here: autocommit (the checkpoint saver
    expects it), `prepare_threshold=None` (borrows hop PgBouncer backends),
    `PG_KEEPALIVE_KWARGS`, the configured sslmode when the URL is silent, and
    the baseline-session restore as the per-borrow `check`, which is also what
    delivers the statement ceiling (PgBouncer drops the `options` startup
    parameter). See _restore_pooled_session.

    Raises:
        PlaceholderDbUrlError: the resolved db_url is the placeholder.
        NoDatabaseAuthorityError: this home keeps a write-generation ledger and
            the resolved db_url is a credential-free endpoint this process was
            given no login for (see `_guard_db_url`).
    """
    from base.config.data_plane import sslmode_for_url

    cfg = config or db_config_from_settings()
    sslmode = sslmode_for_url(cfg.db_url, cfg.db_sslmode)
    return pool_class(
        _guard_db_url(cfg.db_url),
        min_size=min_size,
        max_size=max_size,
        timeout=timeout,
        open=False,
        kwargs={
            "autocommit": True,
            "prepare_threshold": None,
            **({"sslmode": sslmode} if sslmode else {}),
            "application_name": application_name(),
            **PG_KEEPALIVE_KWARGS,
        },
        check=_restore_pooled_session_async,
        **pool_kwargs,
    )
