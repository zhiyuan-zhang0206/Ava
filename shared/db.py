"""Postgres operations shared by UI and kernel.

`shared/` is the UI/kernel boundary layer — `ui/*.py` does not import
`agent.*`; the two sides couple only via DB + Redis. This package
centralizes helpers used by both ends (`config` / `db` / `events` /
`exit_codes` / `schema.sql`).

This module contains **pure SQL, no business semantics, used by both
ends** helpers. Kernel-only (inbound claim, wait/mark/revert) is in
`agent/db.py`.
"""

import contextlib
import json
from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any, NamedTuple
from urllib.parse import urlsplit

import psycopg
from psycopg_pool import ConnectionPool

from shared.agents import AgentStatus
from shared.config import settings
from shared.config.data_plane import gateway_url_host, resolved_pool_size, sslmode_for_url
from shared.db_transaction import write_transaction
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
# pool kwargs (agent/loop.py), the boot-time schema assertion
# (shared/migrations.py:assert_schema_current), `ava.DB` (ava/_settings.py), the
# SDK shell-session index (ava/shell/sessions.py), and the self-respawn CAS
# (agent/db.py). Fail-fast behaviour is pinned by tests/shared/test_connect_fail_fast.py.
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
# see agent/loop.py / ava/_settings.py), so this one constant is the single
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
# agent-process connection points (agent/loop.py pool, ava/_settings.py ava.DB,
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
    """Async twin of `_restore_pooled_session` for the agent-process pools.

    The agent's `AsyncConnectionPool` (agent/loop.py) is built directly rather
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
    restarter healthcheck's stand-in dispatch runs inside the watchdog's sequential
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


class InboundRow(NamedTuple):
    """One row of inbound_messages (for timeline reads)."""

    id: int
    content: str
    kind: str
    source: str | None
    status: str
    created_at: datetime
    claimed_at: datetime | None = None


def fetch_one(cur: psycopg.Cursor, context: str) -> tuple[Any, ...]:
    """After `fetchone()`, assert there was a row — for
    `INSERT ... RETURNING` / aggregate queries where SQL contractually
    guarantees "exactly one row". `python -O` swallows assert, so we
    explicitly raise.

    `context` is a short tag from the caller (e.g. `"insert agent-1"`)
    — call sites are thin wrappers; the traceback pointing at the
    helper alone cannot tell which query failed; reading `cur.query`
    in the helper layer relies on unstable private API. In tests
    `assert` is fine; `python -O` does not run tests."""
    row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"expected exactly one row: {context}")
    return row


def create_agent(db: psycopg.Connection) -> int:
    """Create a new agent (agents + agents_meta row). label left NULL
    ("not set" semantics) — frontend fallback `#N`.

    The `agents` table name is constrained by LangGraph wire
    (`config["configurable"]["thread_id"]`); the public API of this
    function is "create agent". Also INSERTs the agents_meta row —
    per-agent counters for shell/monitor/schedule depend on the
    agents_meta row existing. The spawn path (gateway POST
    /api/agents with prompt) uses a BackgroundTask to LLM-generate a
    short name in the background and CAS-write; non-spawn paths
    (this function, eval callers) do not auto-name; the caller is
    expected to PATCH /api/agents/{id} manually as needed.
    """
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        new_id = fetch_one(cur, "insert new agent")[0]
    db.commit()
    return new_id


def agent_exists(db: psycopg.Connection, agent_id: int) -> bool:
    """Check whether an agent exists. Web endpoints use this as a 404 precondition."""
    with db.cursor() as cur:
        cur.execute("SELECT 1 FROM agents WHERE id = %s", (agent_id,))
        return cur.fetchone() is not None


def list_agents(db: psycopg.Connection) -> list[tuple[int, str | None]]:
    """Return all agents: (id, label). label None means "not set" —
    frontend fallback shows `#N` (see db/schema.sql `agents.label` comment)."""
    with db.cursor() as cur:
        cur.execute("SELECT id, label FROM agents ORDER BY id ASC")
        return cur.fetchall()


def publish_inbound_wake(agent_id: int, payload: str) -> None:
    """Best-effort Redis publish to wake an idle agent — the fast path paired
    with the claim loop's SELECT recheck. Also SETEXes the agent's wake key
    (`shared.cluster.wake_key`) as a durable breadcrumb so a wake lost to a
    disconnected listener is recovered on the listener's next (re)subscribe
    instead of waiting out the full recheck budget.

    Never raises: a wake lost here is recovered by `wait_for_inbound`'s SELECT
    within `timeout_s`, so the caller's INSERT+commit is never held hostage to
    Redis. But the failure is NOT swallowed blindly — a `NoPermissionError`
    (a `ResponseError`) means the publisher's redis ACL user is not granted this
    cluster's `<prefix>:inbound:*` channel (a channel-prefix or ACL misconfig),
    which would silently disable instant wake fleet-wide, so it is logged at
    WARNING. Transient failures (redis down) log at DEBUG. Channel is derived via
    `inbound_channel` so publish and `RedisInboundListener` subscribe stay in
    sync and stay inside the ACL grant."""
    from redis.exceptions import ResponseError

    from shared.cluster import WAKE_KEY_TTL_S, inbound_channel, wake_key
    from shared.redis_client import sync_redis

    channel = inbound_channel(agent_id)
    try:
        r = sync_redis()
        try:
            # redis-py types publish()'s **kwargs as Unknown, so the bound
            # method reads as partially-unknown; the call itself is fully typed.
            r.publish(channel, payload)  # pyright: ignore[reportUnknownMemberType]
            # Durable breadcrumb for the lost-wake window: pub/sub is
            # fire-and-forget, so a publish that lands while the agent's
            # listener is disconnected is otherwise unrecoverable until the
            # claim loop's 30s SELECT recheck. The listener GETDELs this key
            # on (re)subscribe and SELECTs immediately when it is present.
            r.set(wake_key(agent_id), payload, ex=WAKE_KEY_TTL_S)
        finally:
            r.close()
    except ResponseError as exc:
        logger.warning(
            "inbound wake publish to {ch!r} rejected by redis ({exc!r}) — the "
            "cluster redis ACL user lacks this channel; instant wake off, agents "
            "fall back to their SELECT recheck. Check ensure_cluster_redis_acl.",
            ch=channel,
            exc=exc,
        )
    except Exception as exc:
        logger.debug(
            "inbound wake publish to {ch!r} skipped ({exc!r}) — best-effort; the "
            "agent's SELECT recheck delivers within timeout_s.",
            ch=channel,
            exc=exc,
        )


def insert_inbound_message(
    db: psycopg.Connection,
    agent_id: int,
    content: str,
    source: str,
    kind: str = "chat",
    payload: dict[str, object] | None = None,
) -> int:
    """UI / gateway call: INSERT one inbound; the agent's claim node
    fetches and dispatches.

    Args:
        source: provenance tag — claim node reads it and goes
            through `shared/envelope.py:wrap_inbound` envelope prefix to tell
            the agent who the message came from. The UI passes
            `'user'`; a peer agent passes `'agent:N'`.
        kind: default `'chat'` (user dialogue). Other valid values
            see `db/schema.sql` CHECK constraint: `'compact_summary'`
            / `'compact_request'` / `'cancel'` / `'terminate'` / `'restart'`
            / `'restart_completed'` / `'resurrect'`. Non-'chat' usually
            pairs with `content=''` (control signal without payload).
        payload: optional JSONB sidecar. For a multimodal chat inbound this
            carries `{"content_blocks": [...]}` (the OpenAI-shaped text/image
            blocks); the claim node reads it to build a native multimodal
            HumanMessage while `content` holds the text part for legacy /
            envelope / timeline readers. None leaves the column NULL.

    Returns:
        The newly inserted inbound id — the caller can use it to
        publish an `inbound_arrived` event for the web UI to show in
        real time (spec §5).
    """
    # Map inbound kind → lifecycle event_type. Only chat messages between
    # agents produce a 'send_message' event; user→agent chat is not an
    # inter-agent event. Lifecycle kinds map 1:1 except compact_summary /
    # compact_request which are handled elsewhere (agent self-insert /
    # insert_compact_request_inbound).
    _kind_to_event: dict[str, str | None] = {
        "chat": "send_message",
        "system_note": "send_message",
        "terminate": "terminate",
        "restart": "restart",
        "cancel": "cancel",
        "resurrect": "resurrect",
        "restart_completed": "restart_completed",
        "fork": "fork",
    }
    event_type = _kind_to_event.get(kind)
    # Parse the lineage parent from source. For inter-agent chat it is the
    # sender; for a kind='fork' lifecycle inbound the source is the fork
    # identity marker "agent:{fork_source}" — and per the fork-lineage ruling
    # (2026-08-28, task #1879) the fork event's target_agent_id must be the
    # fork SOURCE (the lineage parent), never the executor.
    target_agent_id: int | None = None
    if event_type == "send_message" and source.startswith("agent:"):
        with contextlib.suppress(ValueError):
            target_agent_id = int(source.removeprefix("agent:"))
    elif event_type == "send_message":
        # user/UI → agent chat is not an inter-agent event; skip event write
        event_type = None
    elif event_type == "fork" and source.startswith("agent:"):
        with contextlib.suppress(ValueError):
            target_agent_id = int(source.removeprefix("agent:"))

    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
            "VALUES (%s, %s, %s, %s, %s::jsonb) RETURNING id",
            (agent_id, content, kind, source, json.dumps(payload) if payload else None),
        )
        new_id = fetch_one(cur, "insert inbound message")[0]
        if event_type is not None:
            from shared.audit_events import insert_event_log

            insert_event_log(
                event_type=event_type,
                agent_id=agent_id,
                source=source,
                target_agent_id=target_agent_id,
                payload={"inbound_id": new_id, "content": content}
                if content
                else {"inbound_id": new_id},
            )
    db.commit()
    # Publish to Redis to wake the idle agent. Agents subscribe to
    # `<prefix>:inbound:{agent_id}` (inbound_channel) via RedisInboundListener.
    # Fire-and-forget: the agent's defensive SELECT recheck catches inbound
    # within timeout_s regardless — but a NOPERM is logged, not swallowed.
    publish_inbound_wake(agent_id, str(new_id))
    return new_id


# A "live" agent is one currently holding a process (status running/idling) —
# the set a cluster-wide stop-the-world (quiesce before a schema migration) must
# drain. The three helpers below all key off this same predicate; the statuses
# live in ONE constant, passed as a parameter (`status = ANY(%s)`), so a "live"
# semantics change touches one line, not three literal copies (audit
# 05-gateway-lifecycle A3).
# R1 (Task #1021): the single "alive" predicate — status in {running, idling}
# AND lease unexpired. The lease (`agents_meta.lease_expires_at`, written at
# claim by `agent._starting.claim_agent_row`, renewed by the agent's loop)
# is the liveness authority: a process that died without writing 'terminated'
# leaves its status behind and the lease expires; a process that cannot renew
# (wedged, pre-lease code) is a zombie the reaper collects.
#
# The statuses live in ONE constant and the lease condition in ONE fragment
# (`ALIVE_SQL`), so a "live" semantics change touches one line, not the literal
# copies (audit 05-gateway-lifecycle A3 + r1-state-liveness).
ALIVE_STATUSES: tuple[str, ...] = (
    AgentStatus.RUNNING.value,
    AgentStatus.IDLING.value,
)

# The SQL half of the predicate. Every reader interpolates `status = ANY(%s)
# AND lease_expires_at > now()` (first parameter = list(ALIVE_STATUSES)) so
# "alive" stays one definition across queries of every shape.
ALIVE_SQL = "status = ANY(%s) AND lease_expires_at > now()"


def agent_is_alive(status: str | None, lease_expires_at: datetime | None) -> bool:
    """The Python half of the single alive predicate, for row-based checks.

    A row is alive iff its status says a process should own it AND its lease is
    unexpired. `None` lease (never granted — pre-lease code) reads as dead,
    matching the SQL fragment: the reaper collects such rows so they land on
    code that renews.
    """
    if status not in ALIVE_STATUSES:
        return False
    if lease_expires_at is None:
        return False
    return lease_expires_at > datetime.now(UTC)


# FYI notice (require_response=false) lifetime in the open queue: after this many
# days an unread FYI is auto-resolved ('read') — the open feed, the unread badge
# and the IM bridge stop carrying it (audit 05-gateway-lifecycle C1: open FYIs
# used to pile up forever). require_response notices NEVER expire — the user's
# answer is the only close. The expiry is applied as a query-side rule
# (created_at cutoff, `make_interval(days => ...)`) plus a lazy auto-resolve in
# the gateway's open-feed query, so no background sweeper is needed. One knob:
# change it here (and re-run the notice tests) to configure the TTL.
NOTICE_FYI_TTL_DAYS = 30


def signal_live_agents_restart(
    source: str, *, exclude_agent_ids: Collection[int] = (), machine: str | None = None
) -> list[int]:
    """Bulk-INSERT one kind='restart' inbound per live agent, wake each over Redis; return ids.

    The set-based form of the per-agent restart path (gateway
    restart_agent_op -> insert_inbound_message(kind='restart')): same
    inbound_messages contract, one INSERT ... SELECT over every live agent, and
    the same per-agent Redis wake so an *idling* agent restarts now instead of
    stalling to its SELECT recheck. That prompt drain matters here specifically:
    this is `ava cluster update`'s quiesce step, whose convergence loop keeps signalling
    until every live agent has drained — a 30s recheck lag per idle agent would
    drag the whole quiesce out. `source` tags the signal's origin
    (e.g. 'system:update').

    `machine` scopes the signal to one host's agents — the per-host quiesce the
    agent-runner self-update runs before it stops services (watchdog self-heal /
    a direct `ava cluster update` on a runner); None means the whole cluster
    (the rollout's stop-the-world).

    Args:
        source: tags the signal's origin (e.g. 'system:update').
        exclude_agent_ids: agents to skip in the bulk insert. The quiesce
            convergence loop passes its already-signalled set, so each pass
            signals only agents that newly became live — an agent respawned
            mid-quiesce, or one whose spawn completed mid-quiesce.
        machine: restrict to agents running on this machine (None = all).
    """
    with write_transaction() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "  # noqa: S608 — ALIVE_SQL is a module constant
            "SELECT id, '', 'restart', %s FROM agents_meta "
            f"WHERE {ALIVE_SQL} AND NOT (id = ANY(%s)) "
            "AND (%s::text IS NULL OR machine = %s) "
            "RETURNING agent_id",
            (source, list(ALIVE_STATUSES), list(exclude_agent_ids), machine, machine),
        )
        ids = [row[0] for row in cur.fetchall()]
        conn.commit()
    # Publish a wake per signalled agent (see insert_inbound_message + the
    # publish_inbound_wake docstring). Best-effort: a lost publish is recovered
    # by the agent's SELECT recheck. restart carries no user-facing inbound id,
    # so "0" (mirroring insert_compact_request_inbound).
    for aid in ids:
        publish_inbound_wake(aid, "0")
    return ids


def list_live_agent_ids(machine: str | None = None) -> list[int]:
    """IDs of agents currently holding a process (status running/idling).

    `machine` restricts to one host's agents (the per-host quiesce); None
    returns the whole cluster (the rollout's stop-the-world).
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM agents_meta "  # noqa: S608 — ALIVE_SQL is a module constant
            f"WHERE {ALIVE_SQL} AND (%s::text IS NULL OR machine = %s)",
            (list(ALIVE_STATUSES), machine, machine),
        )
        return [row[0] for row in cur.fetchall()]


def mark_agents_restarting(agent_ids: Collection[int]) -> list[int]:
    """CAS-mark live agents 'restarting' so the restarter respawns them on new
    code after the update (the force-reap leg of a quiesce timeout: the process
    is killed next, and status='restarting' is what the restarter's
    RespawnController picks up once the host unpauses).

    Only rows currently 'running'/'idling' are moved — an agent that exited
    cleanly in the meantime (consumed its restart signal) is untouched. Returns
    the ids actually marked.
    """
    if not agent_ids:
        return []
    with write_transaction() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status = 'restarting' "  # noqa: S608 — ALIVE_SQL is a module constant
            f"WHERE id = ANY(%s) AND {ALIVE_SQL} "
            "RETURNING id",
            (list(agent_ids), list(ALIVE_STATUSES)),
        )
        ids = [row[0] for row in cur.fetchall()]
        conn.commit()
    return ids


def list_pending_inbounds(db: psycopg.Connection, agent_id: int) -> list[InboundRow]:
    """List chat inbounds still queued for an agent, oldest first.

    Only `status='pending'` (the claim node has not picked these up yet)
    and `kind='chat'` (user-visible dialogue, not control signals). Once a
    message is claimed it enters the agent's in-memory messages and shows
    up in the timeline snapshot, so it is intentionally excluded here — the
    web UI renders these as a compact "pending" strip above the composer,
    distinct from the timeline.
    """
    with db.cursor() as cur:
        cur.execute(
            "SELECT id, content, kind, source, status, created_at, claimed_at "
            "FROM inbound_messages "
            "WHERE agent_id = %s AND status = 'pending' AND kind = 'chat' "
            "ORDER BY created_at ASC",
            (agent_id,),
        )
        return [InboundRow(*row) for row in cur.fetchall()]


def insert_compact_request_inbound(db: psycopg.Connection, agent_id: int) -> int:
    """UI / admin call: insert one kind='compact_request' inbound —
    the claim Node, on receiving, runs the backend Compaction LLM to
    generate a summary that replaces messages.

    The new design (Step 2 cleanup) merges the old
    framework_compact / agent_compact kinds: the user view no longer
    distinguishes modes; backend LLM summary generation is the unified
    path. Agent-initiated compact still goes through
    ava.self.compact() -> kind='compact_summary' (agent writes its
    own summary)."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind) VALUES (%s, %s, %s) "
            "RETURNING id",
            (agent_id, "", "compact_request"),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("compact request inbound INSERT returned no id")
        inbound_id = row[0]
        from shared.audit_events import insert_event_log

        insert_event_log(
            event_type="compact",
            agent_id=agent_id,
            source="user",
            payload={"compact_kind": "request"},
        )
    db.commit()
    # Publish to Redis for agent wake-up (see insert_inbound_message + the
    # publish_inbound_wake docstring).
    publish_inbound_wake(agent_id, str(inbound_id))
    return inbound_id


def list_inbound_messages(
    db: psycopg.Connection,
    agent_id: int,
    limit: int = 500,
) -> list[InboundRow]:
    """Read inbound_messages rows for the given agent (including
    done) in created_at ascending order.

    Used by the /timeline endpoint — fetched when merging three
    sources for external inbound records.
    """
    with db.cursor() as cur:
        cur.execute(
            "SELECT id, content, kind, source, status, created_at, claimed_at "
            "FROM inbound_messages "
            "WHERE agent_id = %s ORDER BY created_at ASC LIMIT %s",
            (agent_id, limit),
        )
        return [InboundRow(*r) for r in cur.fetchall()]
