"""End-to-end: a real PgBouncer in transaction pooling in front of a throwaway
Postgres. Proves the load-bearing wire behaviour the unit tests cannot:

- scram-sha-256 client auth against a userlist entry, with a credential-less
  server hop (here TCP loopback trust — the pooling behavior under test is
  independent of the hop's auth; the production verifier userlist and SCRAM
  pass-through socket hop are proven in cli/commands/tests/test_single_box.py),
- transaction pooling with `prepare_threshold=None` (never prepare) — the same
  query run across many autocommit transactions never hits "prepared statement
  does not exist" as different backends are handed out,
- LangGraph's PostgresSaver setup() DDL + put/get through the pooler (the flagged
  DDL-through-transaction-pooling risk).

Skipped when pgbouncer is not installed (CI installs it through the
install-pg-redis composite action; a dev box uses `brew install pgbouncer`).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Generator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from base.cluster.authority import POOLER_ADMIN
from base.db import Database
from base.events.live.bus import EventBus
from cli.commands.data_plane.pgbouncer import pgbouncer_bin
from tests._containers import _free_port, _wait_port, postgres

_SECRET = "pgbouncerwiretestsecret"  # noqa: S105 — test fixture, not a real credential


def _pgbouncer_available() -> bool:
    binary = pgbouncer_bin()
    return Path(binary).exists() or shutil.which(binary) is not None


pytestmark = pytest.mark.skipif(
    not _pgbouncer_available(), reason="pgbouncer not installed (brew/apt install pgbouncer)"
)


@contextlib.contextmanager
def _pgbouncer_in_front(
    pg_url: str,
    listen_addr: str = "127.0.0.1",
    pool_size: int = 2,
    *,
    read_only_default: bool = False,
) -> Generator[str]:
    """Start a transaction-pooling PgBouncer in front of the throwaway Postgres at
    `pg_url`; yield the pooled connection URL. Pooling config mirrors
    cli/commands/pgbouncer; the server hop is TCP loopback under the throwaway's
    trust posture rather than the production SCRAM pass-through socket hop.

    `listen_addr` lets a test bind the listener on a specific loopback address —
    e.g. 127.0.0.2 to prove the degraded-bind probe dials exactly the bound
    address (127.0.0.0/8 is local on Linux; macOS needs an lo0 alias, so the
    caller guards on sys.platform).

    `pool_size` sets `default_pool_size` — 2 (the default) makes concurrent
    clients genuinely reuse backends; 1 forces every client onto the same
    backend, which pollution tests need for determinism.

    `read_only_default` makes the database's session default read-only before
    the pooler first connects. PgBouncer >= 1.26 tracks
    `default_transaction_read_only` from the first backend's report and aligns
    every client to it, so the default must predate that first backend; one
    client's `SET` no longer reaches another client, and `RESET ALL` restores
    this default rather than clearing it."""
    info = conninfo_to_dict(pg_url)
    pg_port = int(str(info["port"]))
    dbname = str(info["dbname"])
    role = "ava_citest"

    # Give the cluster role a scram verifier so client scram against the userlist works
    # (pg default password_encryption = scram-sha-256). ALTER ROLE ... PASSWORD is DDL
    # and cannot bind params; _SECRET is a fixed alnum test constant, so inline it.
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(f"ALTER ROLE {role} PASSWORD '{_SECRET}'")
        if read_only_default:
            conn.execute(
                sql.SQL("ALTER DATABASE {} SET default_transaction_read_only = on").format(
                    sql.Identifier(dbname)
                )
            )

    from base.db import PG_STATEMENT_TIMEOUT_SET_SQL

    tmp = Path(tempfile.mkdtemp(prefix="ava-pgbouncer-test-"))
    listen_port = _free_port()
    userlist = tmp / "userlist.txt"
    # Mirrors _render_ini: the admin console belongs to the pooler admin alone.
    userlist.write_text(f'"{role}" "{_SECRET}"\n"{POOLER_ADMIN}" "{_SECRET}"\n')
    ini = tmp / "pgbouncer.ini"
    ini.write_text(
        "\n".join(
            [
                "[databases]",
                # Mirrors cli/commands/pgbouncer._render_ini: every pooled
                # backend is born with the statement ceiling via connect_query.
                f"{dbname} = host=127.0.0.1 port={pg_port} dbname={dbname} "
                f"connect_query='{PG_STATEMENT_TIMEOUT_SET_SQL}'",
                "[pgbouncer]",
                f"listen_addr = {listen_addr}",
                f"listen_port = {listen_port}",
                "auth_type = scram-sha-256",
                f"auth_file = {userlist}",
                "pool_mode = transaction",
                # Mirrors _render_ini's reset contract (option B, 2026-09-03
                # ruling): one statement, unquoted — pgbouncer 1.25.2 runs
                # server_reset_query verbatim (a quoted value is a syntax error,
                # 2026-09-02 P0) and transaction pooling wraps a multi-statement
                # reset in an implicit transaction ("DISCARD ALL cannot run
                # inside a transaction block"). always=0: the reset runs only in
                # the SV_ACTIVE window, so clean releases/disconnects keep the
                # backend's session (birth connect_query ceiling and a client's
                # own SETs survive — measured 2026-09-03); between-transaction
                # pollution is defended client-side by base/db/__init__.py's baseline
                # restore on every pooled dial and borrow.
                "server_reset_query = DISCARD ALL",
                "server_reset_query_always = 0",
                "max_client_conn = 100",
                f"default_pool_size = {pool_size}",  # tiny, so transactions genuinely reuse backends
                "ignore_startup_parameters = extra_float_digits,options",
                f"admin_users = {POOLER_ADMIN}",
                f"logfile = {tmp / 'pgbouncer.log'}",
                f"pidfile = {tmp / 'pgbouncer.pid'}",
                "",
            ]
        )
    )
    subprocess.run(  # noqa: S603 — argv is the resolved pgbouncer path + our generated ini
        [pgbouncer_bin(), "-d", str(ini)], check=True, capture_output=True, text=True
    )
    try:
        _wait_port(listen_port, host=listen_addr)
        pooled = f"postgresql://{role}:{_SECRET}@{listen_addr}:{listen_port}/{dbname}"
        # pgbouncer -d races the listener; wait for a real authenticated answer.
        deadline = time.monotonic() + 15
        while True:
            try:
                with psycopg.connect(pooled, prepare_threshold=None) as c:
                    c.execute("SELECT 1")
                break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        yield pooled
    finally:
        with contextlib.suppress(Exception):
            pid = int((tmp / "pgbouncer.pid").read_text().strip())
            os.kill(pid, signal.SIGTERM)
        shutil.rmtree(tmp, ignore_errors=True)


def _admin_console_url(pooled: str) -> str:
    """The admin console of a `_pgbouncer_in_front` pooler, dialed as the
    pooler admin — its only `admin_users` entry (the pooled role is refused)."""
    return make_conninfo(pooled, user=POOLER_ADMIN, password=_SECRET, dbname="pgbouncer")


def test_scram_client_auth_and_pooled_select() -> None:
    with (
        postgres() as pg_url,
        _pgbouncer_in_front(pg_url) as pooled,
        psycopg.connect(pooled, prepare_threshold=None) as conn,
    ):
        row = conn.execute("SELECT 1").fetchone()
        assert row is not None and row[0] == 1


def test_wrong_secret_is_rejected() -> None:
    with postgres() as pg_url, _pgbouncer_in_front(pg_url) as pooled:
        bad = pooled.replace(_SECRET, "not-the-secret")
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(bad, prepare_threshold=None, connect_timeout=5)


def test_transaction_pooling_never_prepare() -> None:
    """The same query across many autocommit transactions (each may land on a
    different backend under default_pool_size=2) never errors with
    prepare_threshold=None — the property that makes transaction pooling safe
    (psycopg3's `0` prepares on the first execution; two fresh connections
    preparing the same `_pg3_0` name on one backend collide)."""
    with (
        postgres() as pg_url,
        _pgbouncer_in_front(pg_url) as pooled,
        psycopg.connect(pooled, autocommit=True, prepare_threshold=None) as conn,
    ):
        for i in range(50):
            row = conn.execute("SELECT %s::int", (i,)).fetchone()
            assert row is not None and row[0] == i


@contextlib.contextmanager
def _read_only_default_pooler(pg_url: str, pool_size: int = 2) -> Generator[str]:
    """A pooler whose every session defaults to read-only, checked before yielding.

    The forcing condition for the explicit `SET TRANSACTION READ WRITE`
    declarations. PgBouncer >= 1.26 keeps one client's `SET
    default_transaction_read_only` from reaching another, so an unasked-for
    read-only default now comes from outside the client: a pooler that does not
    track the parameter (a remote-managed data plane's), or a database or role
    default, which is what this reproduces. `RESET ALL` restores such a default
    rather than clearing it, so the session scrub cannot help and only the
    explicit declaration gets a write through. The probe proves the condition is
    live, so a passing write is the declaration's doing.
    """
    with _pgbouncer_in_front(pg_url, pool_size=pool_size, read_only_default=True) as pooled:
        with psycopg.connect(pooled, autocommit=True, prepare_threshold=None) as probe:
            row = probe.execute("SHOW default_transaction_read_only").fetchone()
            assert row is not None and str(row[0]) == "on"
        yield pooled


def _direct_writer(pg_url: str) -> psycopg.Connection:
    """A direct setup/verify connection that writes despite a read-only default."""
    return psycopg.connect(pg_url, options="-c default_transaction_read_only=off")


def _insert_agent(pg_url: str) -> int:
    """Insert one agent for foreign-keyed PgBouncer wire-test rows."""
    with _direct_writer(pg_url) as conn:
        row = conn.execute("INSERT INTO agents DEFAULT VALUES RETURNING id").fetchone()
        assert row is not None
        return int(row[0])


def test_write_transaction_overrides_a_read_only_default_on_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule A writes (`set_posture`'s upsert, opened by `Database.write_transaction()` on its
    own dial) land in sessions that default to read-only."""
    from base import config
    from base.deploy.state import host_deploy_state

    with postgres() as pg_url, _read_only_default_pooler(pg_url, pool_size=1) as pooled:
        monkeypatch.setattr(config.settings.data_plane, "db_url", pooled)
        host_deploy_state.set_posture(Database.from_settings(), "paused")

        with psycopg.connect(pg_url) as verify:
            row = verify.execute("SELECT posture FROM host_deploy_state").fetchone()
        assert row is not None and row[0] == "paused"


def test_schedule_provision_overrides_a_read_only_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3 Rule A schedule provisioning declares direct writes read-write."""
    from base import config
    from cli.commands.management.schedules import cmd_schedules_provision

    with postgres() as pg_url, _read_only_default_pooler(pg_url) as pooled:
        monkeypatch.setattr(config.settings.data_plane, "db_url", pooled)
        assert cmd_schedules_provision() == 0

        with psycopg.connect(pg_url) as verify:
            row = verify.execute("SELECT count(*) FROM schedules").fetchone()
        assert row is not None and int(row[0]) > 0


def test_write_transaction_overrides_a_read_only_default_on_pool_borrow(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
) -> None:
    """Rule B's pool-borrow DELETE declares its transaction writable first."""
    from base import config
    from base.db import pool
    from services.upkeep.ttl_reaper.shells import delete_shell_row

    with postgres() as pg_url, _read_only_default_pooler(pg_url) as pooled:
        monkeypatch.setattr(config.settings.data_plane, "db_url", pooled)
        agent_id = _insert_agent(pg_url)
        with _direct_writer(pg_url) as setup:
            setup.execute(
                "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at) "
                "VALUES (%s, %s, now())",
                (agent_id, 7),
            )
        db_pool = pool(min_size=1, max_size=2)
        try:
            delete_shell_row(db_pool, database, event_bus, agent_id, 7, interrupted=False)
        finally:
            db_pool.close()

        with psycopg.connect(pg_url) as verify:
            row = verify.execute(
                "SELECT 1 FROM agent_shell_ttls WHERE agent_id = %s AND session_id = %s",
                (agent_id, 7),
            ).fetchone()
        assert row is None


def test_session_touch_overrides_a_read_only_default() -> None:
    """R3 Rule B session writes declare a raw pool borrow read-write first."""
    from gateway.auth.session_store import touch_session

    with postgres() as pg_url, _read_only_default_pooler(pg_url) as pooled:
        with _direct_writer(pg_url) as setup:
            setup.execute(
                "INSERT INTO web_sessions (id, expires_at) VALUES (%s, now() + interval '1 hour')",
                ("pgbouncer-session-touch",),
            )
        db_pool = ConnectionPool(
            pooled,
            min_size=1,
            max_size=2,
            open=True,
            kwargs={"prepare_threshold": None},
        )
        try:
            touch_session(db_pool, "pgbouncer-session-touch")
        finally:
            db_pool.close()

        with psycopg.connect(pg_url) as verify:
            row = verify.execute(
                "SELECT last_seen_at FROM web_sessions WHERE id = %s",
                ("pgbouncer-session-touch",),
            ).fetchone()
        assert row is not None and row[0] is not None


def test_async_write_transaction_overrides_a_read_only_default() -> None:
    """Rule C opens an explicit read-write transaction on an autocommit pool."""
    from base.db.transaction import async_write_transaction

    async def write_once(pooled: str) -> None:
        db_pool = AsyncConnectionPool(
            pooled,
            min_size=1,
            max_size=2,
            open=False,
            kwargs={"autocommit": True, "prepare_threshold": None},
        )
        await db_pool.open()
        try:
            async with async_write_transaction(db_pool) as conn:
                await conn.execute(
                    "UPDATE deployment_state SET min_code_version = min_code_version WHERE id = 1"
                )
        finally:
            await db_pool.close()

    with postgres() as pg_url, _read_only_default_pooler(pg_url) as pooled:
        asyncio.run(write_once(pooled))


def test_plain_autocommit_write_still_fails_under_a_read_only_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The read-only-default tests have teeth: an unpostured pooled write is rejected."""
    from base import config
    from base.db import connect

    with postgres() as pg_url, _read_only_default_pooler(pg_url, pool_size=1) as pooled:
        monkeypatch.setattr(config.settings.data_plane, "db_url", pooled)
        with connect(autocommit=True) as conn, pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute(
                "UPDATE deployment_state SET min_code_version = min_code_version WHERE id = 1"
            )


def test_pooler_isolates_one_clients_read_only_posture_from_another() -> None:
    """PgBouncer >= 1.26 keeps a client's `default_transaction_read_only` to itself.

    With one pooled backend, a client that sets the default read-only and holds
    its connection shares the backend with a second client, and the second
    client still sees its own (writable) default: the cross-client leak behind
    the 2026-09-02 P0 cannot happen in the pooler this repo pins.
    """
    with (
        postgres() as pg_url,
        _pgbouncer_in_front(pg_url, pool_size=1) as pooled,
        psycopg.connect(pooled, autocommit=True, prepare_threshold=None) as poisoner,
        psycopg.connect(pooled, autocommit=True, prepare_threshold=None) as other,
    ):
        poisoner.execute("SET default_transaction_read_only = on")
        row = other.execute("SHOW default_transaction_read_only").fetchone()
        assert row is not None and str(row[0]) == "off"
        other.execute(
            "UPDATE deployment_state SET min_code_version = min_code_version WHERE id = 1"
        )
        row = poisoner.execute("SHOW default_transaction_read_only").fetchone()
        assert row is not None and str(row[0]) == "on"


def _statement_timeout(conn: psycopg.Connection) -> str:
    """The backend's statement_timeout as a string, with a row-asserted fetchone."""
    row = conn.execute("SHOW statement_timeout").fetchone()
    assert row is not None
    return str(row[0])


def test_connect_query_bounds_pooled_backends_at_birth() -> None:
    """A pooled backend is born with statement_timeout=60s via the connect_query,
    even for a client that never issues the SET itself — the pooler-side half of
    the statement-timeout delivery. Without connect_query this client (options
    startup parameter dropped by the pooler, no explicit SET) would see 0."""
    with (
        postgres() as pg_url,
        _pgbouncer_in_front(pg_url) as pooled,
        psycopg.connect(pooled, autocommit=True, prepare_threshold=None) as conn,
    ):
        # No options=, no SET — the backend's connect_query is the only source.
        assert _statement_timeout(conn) == "1min"


def test_base_connect_applies_statement_timeout_on_pooled_dial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """base.db.connect() delivers the statement ceiling through the pooler: the
    `options` startup parameter is dropped by PgBouncer, so the pooled dial runs
    the explicit SET (the client-side half of the delivery)."""
    from base import config

    with postgres() as pg_url, _pgbouncer_in_front(pg_url) as pooled:
        dp = config.settings.data_plane
        # The one-URL design: AVA_DB_URL carries the pooled listener URL; dialing
        # it (direct=False) is a pooled dial and must issue the SET. The pooled
        # front door authenticates with scram against the userlist (role
        # `ava_citest`, the harness secret), so the URL settings carry must use
        # that role+password — the throwaway pg's own URL user (`ava`) has no
        # password and is not in the userlist.
        monkeypatch.setattr(dp, "db_url", pooled)

        import base.db

        with base.db.connect() as conn:
            assert _statement_timeout(conn) == "1min"


def test_base_pool_applies_statement_timeout_on_pooled_dial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """base.db.pool() applies the SET on every new backend via the pool's
    configure hook — a borrowed connection through PgBouncer is bounded."""
    from base import config

    with postgres() as pg_url, _pgbouncer_in_front(pg_url) as pooled:
        dp = config.settings.data_plane
        monkeypatch.setattr(dp, "db_url", pooled)

        import base.db

        pool = base.db.pool(min_size=1, max_size=2)
        try:
            with pool.connection() as conn:
                assert _statement_timeout(conn) == "1min"
        finally:
            pool.close()


def test_langgraph_saver_setup_and_roundtrip_through_pgbouncer() -> None:
    """PostgresSaver.setup() DDL + put/get run through the pooler in transaction
    mode — the flagged DDL-through-transaction-pooling risk. setup() is idempotent
    (throwaway pg already has the tables); this asserts it does not choke via the
    pooler and a checkpoint round-trips."""
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.checkpoint.postgres import PostgresSaver

    with (
        postgres() as pg_url,
        _pgbouncer_in_front(pg_url) as pooled,
        PostgresSaver.from_conn_string(pooled) as saver,
    ):
        saver.setup()
        cfg: RunnableConfig = {"configurable": {"thread_id": "pgbouncer-wire", "checkpoint_ns": ""}}
        ckpt = empty_checkpoint()
        saved_cfg = saver.put(cfg, ckpt, {}, {})
        got = saver.get_tuple(saved_cfg)
        assert got is not None
        assert got.checkpoint["id"] == ckpt["id"]


def test_admin_probe_reaches_the_bound_address_only() -> None:
    """P4: the admin-console probe authenticates as the pooler admin and dials
    exactly the address it names — an address pgbouncer did not bind FAILS,
    while the bound one answers. (Public-bind verification itself reads the
    socket table, `pgbouncer_public_listener_reachable`, never a self-dial.)

    127.0.0.2 is local on Linux (CI runs this); macOS needs an lo0 alias, so
    skip there."""
    import sys

    from base.cluster.dataplane.pooler import admin_reachable

    if sys.platform == "darwin":
        pytest.skip("127.0.0.2 needs an lo0 alias on macOS")
    with (
        postgres() as pg_url,
        _pgbouncer_in_front(pg_url, listen_addr="127.0.0.2") as pooled,
    ):
        listen_port = int(str(conninfo_to_dict(pooled)["port"]))
        assert admin_reachable(listen_port, _SECRET, host="127.0.0.2") is True
        assert admin_reachable(listen_port, _SECRET, host="127.0.0.1") is False


# ── Pooled session-GUC pollution (2026-09-02 P0) ────────────────────────────
#
# pgbouncer transaction pooling hands any backend to any client transaction and
# does NOT reset backend session state on the ordinary release path (the reset
# runs only in the SV_ACTIVE window, always=0). A session-level SET therefore
# stays on the shared backend for the next borrower. PgBouncer >= 1.26 tracks
# the parameters PostgreSQL reports to clients per client, which closed the
# read-only vector of the 2026-09-02 P0 (500s on the agent message /
# schedule-stop APIs) for the pooler this repo pins; parameters PostgreSQL
# does not report (search_path, statement_timeout, lock_timeout, work_mem, …)
# still leak, and a remote-managed data plane's pooler may track nothing. The
# client side must scrub the session back to baseline on every pooled use;
# these tests pin that contract end to end with an untracked poison.

_POISON_SQL = ("SET search_path = nowhere", "SET statement_timeout = 0")


def _poison_backend(pooled: str) -> None:
    """Leave untracked session GUCs on the single pooled backend, then prove
    they reached the next client.

    One client breaks name resolution (every unqualified statement now fails
    with UndefinedTable) and lifts the statement ceiling, then disconnects
    cleanly. A raw client that runs no scrub must observe both on the same
    backend: if a future pooler or PostgreSQL starts tracking them, this fails
    loudly instead of letting the scrub tests below pass against a clean
    backend. The raw client resets nothing, so the backend stays poisoned."""
    with psycopg.connect(pooled, autocommit=True, prepare_threshold=None) as polluter:
        for statement in _POISON_SQL:
            polluter.execute(statement)
    with psycopg.connect(pooled, autocommit=True, prepare_threshold=None) as raw:
        row = raw.execute("SHOW search_path").fetchone()
        assert row is not None and str(row[0]) == "nowhere"
        assert _statement_timeout(raw) == "0"


def test_pooled_borrow_scrubs_a_poisoned_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pool-level regression: a borrower must never inherit another client's
    session-level SET. The pool's `check` hook (every borrow) restores the
    baseline session, so the borrowed session resolves names and carries the
    statement ceiling again, and an unqualified write succeeds."""
    import base.db as base_db
    from base import config

    with postgres() as pg_url, _pgbouncer_in_front(pg_url, pool_size=1) as pooled:
        monkeypatch.setattr(config.settings.data_plane, "db_url", pooled)
        pool = base_db.pool(min_size=1, max_size=1)
        try:
            # Force the pool's physical backend into existence (creation runs the
            # configure hook) BEFORE poisoning, so the borrow-time check hook —
            # not backend birth — is what saves the borrower below.
            with pool.connection() as conn:
                conn.execute("SELECT 1")
            _poison_backend(pooled)
            with pool.connection() as conn:
                row = conn.execute("SHOW search_path").fetchone()
                assert row is not None and str(row[0]) != "nowhere"
                assert _statement_timeout(conn) == "1min"
                conn.execute(
                    "UPDATE deployment_state SET min_code_version = min_code_version WHERE id = 1"
                )
        finally:
            pool.close()


def test_message_insert_and_schedule_stop_survive_a_poisoned_backend(
    monkeypatch: pytest.MonkeyPatch,
    publish_wake: Callable[[int, str], bool],
) -> None:
    """P0 batch-5 regression: the agent message INSERT and the schedule stop
    UPDATE — the two API writes the user saw 500 — succeed when pgbouncer hands
    their transaction a backend another client left polluted. Runs the real
    write helpers through the sanctioned pool path against a poisoned
    single-backend pooler."""
    import base.db as base_db
    from base import config
    from base.agents.messages.chat_delivery import insert_chat_inbound_once
    from gateway.schedules.router import _update_blocking

    with postgres() as pg_url, _pgbouncer_in_front(pg_url, pool_size=1) as pooled:
        monkeypatch.setattr(config.settings.data_plane, "db_url", pooled)

        # The inbound wake publish needs Redis, which this harness does not run; it never
        # raises, and the regression target is the durable DB write.
        with psycopg.connect(pg_url, autocommit=True) as admin:
            row = admin.execute(
                "INSERT INTO agents (label) VALUES ('poison-probe-agent') RETURNING id"
            ).fetchone()
            assert row is not None
            agent_id: int = row[0]
            row = admin.execute(
                "INSERT INTO schedules (name, script, command, enabled) "
                "VALUES ('poison-probe-schedule', 'print(1)', 'python schedule.py', true) "
                "RETURNING id"
            ).fetchone()
            assert row is not None
            schedule_id: int = row[0]
        pool = base_db.pool(min_size=1, max_size=1)
        try:
            with pool.connection() as conn:
                conn.execute("SELECT 1")
            _poison_backend(pooled)
            # message INSERT (POST /api/agents/{id}/messages durable half)
            with pool.connection() as conn:
                receipt = insert_chat_inbound_once(
                    conn,
                    agent_id=agent_id,
                    content="poison-probe",
                    source="user",
                    payload=None,
                    client_message_id=None,
                    publish_wake=publish_wake,
                )
                assert receipt.inserted
            # schedule stop UPDATE (POST /api/schedules/{id}/stop durable half:
            # SELECT ... FOR UPDATE then UPDATE ... RETURNING)
            row, _enabled_changed = _update_blocking(pool, schedule_id, {"enabled": False})
            assert row[4] is False
        finally:
            pool.close()


def test_pooled_dial_names_its_process_and_code_version_and_keeps_the_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pooled `base.db.connect()` whose borrow also reads the cluster's minimum
    code version (the restore's combined statement) still bounds the backend, and
    the pooler's own client list names the connection `ava:<process>:v<version>`:
    the observability half of the code-version gate."""
    from base import config
    from base.db import code_version_gate as gate
    from base.native_process import code_version
    from base.telemetry import process_name

    with postgres() as pg_url, _pgbouncer_in_front(pg_url) as pooled:
        monkeypatch.setattr(config.settings.data_plane, "db_url", pooled)
        monkeypatch.setattr(code_version, "get", lambda: 4321)
        monkeypatch.setattr(code_version, "_db_gate_exempt", False)
        monkeypatch.setattr(gate, "_last_read_at", None)  # the minimum is read on this dial

        import base.db

        with base.db.connect() as conn:
            assert gate.min_read_due() is False  # the dial did read it
            assert _statement_timeout(conn) == "1min"
            with psycopg.connect(_admin_console_url(pooled), autocommit=True) as console:
                cursor = console.execute("SHOW CLIENTS")
                columns = [column.name for column in cursor.description or ()]
                names = {row[columns.index("application_name")] for row in cursor.fetchall()}
    assert f"ava:{process_name()}:v4321" in names
