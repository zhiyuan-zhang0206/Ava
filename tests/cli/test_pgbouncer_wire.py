"""End-to-end: a real PgBouncer in transaction pooling in front of a throwaway
Postgres. Proves the load-bearing wire behaviour the unit tests cannot:

- scram-sha-256 client auth against a plaintext userlist entry (the chosen auth
  scheme), with a credential-less server hop (here TCP loopback trust, mirroring
  the prod unix-socket trust),
- transaction pooling with `prepare_threshold=None` (never prepare) — the same
  query run across many autocommit transactions never hits "prepared statement
  does not exist" as different backends are handed out,
- LangGraph's PostgresSaver setup() DDL + put/get through the pooler (the flagged
  DDL-through-transaction-pooling risk).

Skipped when pgbouncer is not installed (CI installs it via scripts/provision;
a dev box gets it from `brew install pgbouncer`).
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
from collections.abc import Generator
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from cli.commands._pgbouncer import pgbouncer_bin
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
    pg_url: str, listen_addr: str = "127.0.0.1", pool_size: int = 2
) -> Generator[str]:
    """Start a transaction-pooling PgBouncer in front of the throwaway Postgres at
    `pg_url`; yield the pooled connection URL. Config mirrors cli/commands/_pgbouncer,
    but the server hop is TCP loopback (the throwaway's trust posture) rather than the
    prod unix socket — behaviourally the same credential-less trust hop.

    `listen_addr` lets a test bind the listener on a specific loopback address —
    e.g. 127.0.0.2 to prove the degraded-bind probe dials exactly the bound
    address (127.0.0.0/8 is local on Linux; macOS needs an lo0 alias, so the
    caller guards on sys.platform).

    `pool_size` sets `default_pool_size` — 2 (the default) makes concurrent
    clients genuinely reuse backends; 1 forces every client onto the same
    backend, which pollution tests need for determinism."""
    info = conninfo_to_dict(pg_url)
    pg_port = int(str(info["port"]))
    dbname = str(info["dbname"])
    role = "ava_citest"

    # Give the cluster role a scram verifier so client scram against the userlist works
    # (pg default password_encryption = scram-sha-256). ALTER ROLE ... PASSWORD is DDL
    # and cannot bind params; _SECRET is a fixed alnum test constant, so inline it.
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(f"ALTER ROLE {role} PASSWORD '{_SECRET}'")

    from shared.db import PG_STATEMENT_TIMEOUT_SET_SQL

    tmp = Path(tempfile.mkdtemp(prefix="ava-pgbouncer-test-"))
    listen_port = _free_port()
    userlist = tmp / "userlist.txt"
    userlist.write_text(f'"{role}" "{_SECRET}"\n')
    ini = tmp / "pgbouncer.ini"
    ini.write_text(
        "\n".join(
            [
                "[databases]",
                # Mirrors cli/commands/_pgbouncer._render_ini: every pooled
                # backend is born with the statement ceiling via connect_query.
                f"{dbname} = host=127.0.0.1 port={pg_port} dbname={dbname} "
                f"connect_query='{PG_STATEMENT_TIMEOUT_SET_SQL}'",
                "[pgbouncer]",
                f"listen_addr = {listen_addr}",
                f"listen_port = {listen_port}",
                "auth_type = scram-sha-256",
                f"auth_file = {userlist}",
                "pool_mode = transaction",
                # Mirrors _render_ini's load-bearing reset contract: scrub the
                # backend to connect_query-fresh state on EVERY return to the
                # pool (always=1), not only on client disconnect.
                "server_reset_query = 'DISCARD ALL; SET statement_timeout = 60000'",
                "server_reset_query_always = 1",
                "max_client_conn = 100",
                f"default_pool_size = {pool_size}",  # tiny, so transactions genuinely reuse backends
                "ignore_startup_parameters = extra_float_digits,options",
                f"admin_users = {role}",
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


def test_finalize_writes_override_a_poisoned_pooled_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finalizer posture and lock writes must override a poisoned backend.

    This reproduces the rollout-finalizer failure: a prior client sets the
    backend's default transaction posture read-only, then the tail's two
    compensating writes must start explicit read-write transactions before DML.
    """
    from shared import config, host_deploy_state
    from shared.cluster_lock import release_update_lock

    # pool_size=1: the poison and the compensating writes are forced onto the
    # SAME backend — with the shared harness default of 2 the writes could land
    # on the clean backend and pass without exercising the override (review
    # follow-up from #5716 on #1428).
    with postgres() as pg_url, _pgbouncer_in_front(pg_url, pool_size=1) as pooled:
        monkeypatch.setattr(config.settings.data_plane, "db_url", pooled)
        with psycopg.connect(pooled, autocommit=True, prepare_threshold=None) as reader:
            reader.execute("SET default_transaction_read_only = on")
        # Prove the backend is STILL poisoned when the writes run — the raw
        # dial below does not go through shared.db's baseline restore, so it
        # must observe the polluter's read_only (keeps this test's teeth even
        # if a future pgbouncer scrubs on release).
        with psycopg.connect(pooled, autocommit=True, prepare_threshold=None) as probe:
            row = probe.execute("SHOW default_transaction_read_only").fetchone()
            assert row is not None and str(row[0]) == "on"
        host_deploy_state.set_posture("idle")
        release_update_lock("pgbouncer-finalizer-test")


def test_recovery_claim_overrides_a_poisoned_pooled_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery can lock a lease on a backend a previous client made read-only."""
    from shared import config
    from shared.cluster_lock import claim_recovery_lock

    with postgres() as pg_url, _pgbouncer_in_front(pg_url) as pooled:
        monkeypatch.setattr(config.settings.data_plane, "db_url", pooled)
        with psycopg.connect(pooled, autocommit=True, prepare_threshold=None) as reader:
            reader.execute("SET default_transaction_read_only = on")

        claim = claim_recovery_lock("pgbouncer-recovery-test", observed=None)

    assert claim.acquired is True


def _poison_pooled_backends(pooled: str) -> None:
    """Set read-only defaults on both backends in the two-slot test pool.

    The first explicit transaction keeps one backend occupied while the second
    autocommit client sets the other backend's session default. Committing the
    first then releases two poisoned backends for deterministic follow-up writes.
    """
    with (
        psycopg.connect(pooled, autocommit=True, prepare_threshold=None) as first,
        psycopg.connect(pooled, autocommit=True, prepare_threshold=None) as second,
    ):
        first.execute("BEGIN")
        first.execute("SET default_transaction_read_only = on")
        second.execute("SET default_transaction_read_only = on")
        first.execute("COMMIT")


def _insert_agent(pg_url: str) -> int:
    """Insert one agent for foreign-keyed PgBouncer wire-test rows."""
    with psycopg.connect(pg_url) as conn:
        row = conn.execute("INSERT INTO agents DEFAULT VALUES RETURNING id").fetchone()
        assert row is not None
        return int(row[0])


def test_write_transaction_repairs_connect_and_watcher_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule A writes, including watcher cleanup DELETE, survive poisoned backends."""
    from shared import config
    from shared.cluster_lock import acquire_update_lock, release_update_lock
    from shared.watcher_registry import delete_watcher, mark_status, register_watcher

    with postgres() as pg_url, _pgbouncer_in_front(pg_url) as pooled:
        monkeypatch.setattr(config.settings.data_plane, "db_url", pooled)
        agent_id = _insert_agent(pg_url)
        _poison_pooled_backends(pooled)

        assert acquire_update_lock("pgbouncer-wire-test") is True
        release_update_lock("pgbouncer-wire-test")
        register_watcher(agent_id, 7, kind="at", name="poisoned", message="wake")
        mark_status(agent_id, 7, "missed")
        delete_watcher(agent_id, 7)


def test_schedule_provision_repairs_connect_write_on_poisoned_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3 Rule A schedule provisioning declares direct writes read-write."""
    from cli.commands.schedules import cmd_schedules_provision
    from shared import config

    with postgres() as pg_url, _pgbouncer_in_front(pg_url) as pooled:
        monkeypatch.setattr(config.settings.data_plane, "db_url", pooled)
        _poison_pooled_backends(pooled)

        assert cmd_schedules_provision() == 0

        with psycopg.connect(pg_url) as verify:
            row = verify.execute("SELECT count(*) FROM schedules").fetchone()
        assert row is not None and int(row[0]) > 0


def test_write_transaction_repairs_pooled_borrow_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule B's pool-borrow DELETE declares its transaction writable first."""
    from gateway.ttl_reaper import _delete_shell_row_blocking
    from shared import config
    from shared.db import pool

    with postgres() as pg_url, _pgbouncer_in_front(pg_url) as pooled:
        monkeypatch.setattr(config.settings.data_plane, "db_url", pooled)
        agent_id = _insert_agent(pg_url)
        with psycopg.connect(pg_url) as setup:
            setup.execute(
                "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at) "
                "VALUES (%s, %s, now())",
                (agent_id, 7),
            )
        _poison_pooled_backends(pooled)
        db_pool = pool(min_size=1, max_size=2)
        try:
            _delete_shell_row_blocking(db_pool, agent_id, 7, interrupted=False)
        finally:
            db_pool.close()

        with psycopg.connect(pg_url) as verify:
            row = verify.execute(
                "SELECT 1 FROM agent_shell_ttls WHERE agent_id = %s AND session_id = %s",
                (agent_id, 7),
            ).fetchone()
        assert row is None


def test_session_touch_repairs_pooled_borrow_on_poisoned_backend() -> None:
    """R3 Rule B session writes declare a raw pool borrow read-write first."""
    from gateway.session_store import touch_session

    with postgres() as pg_url, _pgbouncer_in_front(pg_url) as pooled:
        with psycopg.connect(pg_url) as setup:
            setup.execute(
                "INSERT INTO web_sessions (id, expires_at) VALUES (%s, now() + interval '1 hour')",
                ("pgbouncer-session-touch",),
            )
        _poison_pooled_backends(pooled)
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


def test_async_write_transaction_repairs_async_pool_write() -> None:
    """Rule C opens an explicit read-write transaction on an autocommit pool."""
    from shared.db_transaction import async_write_transaction

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
                await conn.execute("UPDATE deployment_state SET phase = phase WHERE id = 1")
        finally:
            await db_pool.close()

    with postgres() as pg_url, _pgbouncer_in_front(pg_url) as pooled:
        _poison_pooled_backends(pooled)
        asyncio.run(write_once(pooled))


def test_plain_autocommit_write_still_fails_on_poisoned_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression test is meaningful: unpostured pooled DML stays rejected."""
    from shared import config
    from shared.db import connect

    with postgres() as pg_url, _pgbouncer_in_front(pg_url) as pooled:
        monkeypatch.setattr(config.settings.data_plane, "db_url", pooled)
        _poison_pooled_backends(pooled)
        with connect(autocommit=True) as conn, pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("UPDATE deployment_state SET phase = phase WHERE id = 1")


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


def test_shared_connect_applies_statement_timeout_on_pooled_dial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shared.db.connect() delivers the statement ceiling through the pooler: the
    `options` startup parameter is dropped by PgBouncer, so the pooled dial runs
    the explicit SET (the client-side half of the delivery)."""
    from shared import config

    with postgres() as pg_url, _pgbouncer_in_front(pg_url) as pooled:
        dp = config.settings.data_plane
        # The one-URL design: AVA_DB_URL carries the pooled listener URL; dialing
        # it (direct=False) is a pooled dial and must issue the SET. The pooled
        # front door authenticates with scram against the userlist (role
        # `ava_citest`, the harness secret), so the URL settings carry must use
        # that role+password — the throwaway pg's own URL user (`ava`) has no
        # password and is not in the userlist.
        monkeypatch.setattr(dp, "db_url", pooled)

        import shared.db

        with shared.db.connect() as conn:
            assert _statement_timeout(conn) == "1min"


def test_shared_pool_applies_statement_timeout_on_pooled_dial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shared.db.pool() applies the SET on every new backend via the pool's
    configure hook — a borrowed connection through PgBouncer is bounded."""
    from shared import config

    with postgres() as pg_url, _pgbouncer_in_front(pg_url) as pooled:
        dp = config.settings.data_plane
        monkeypatch.setattr(dp, "db_url", pooled)

        import shared.db

        pool = shared.db.pool(min_size=1, max_size=2)
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
    """P4: the load-bearing premise of the degraded-bind probe — a psycopg dial
    to an address pgbouncer failed to bind actually FAILS, while the bound one
    answers. `_admin_reachable(host=...)` is what `pgbouncer_public_listener_
    reachable` trusts to tell a silently degraded pooler from a healthy one.

    127.0.0.2 is local on Linux (CI runs this); macOS needs an lo0 alias, so
    skip there."""
    import sys

    from cli.commands._pgbouncer import _admin_reachable

    if sys.platform == "darwin":
        pytest.skip("127.0.0.2 needs an lo0 alias on macOS")
    with (
        postgres() as pg_url,
        _pgbouncer_in_front(pg_url, listen_addr="127.0.0.2") as pooled,
    ):
        listen_port = int(str(conninfo_to_dict(pooled)["port"]))
        assert _admin_reachable(listen_port, "ava_citest", _SECRET, host="127.0.0.2") is True
        assert _admin_reachable(listen_port, "ava_citest", _SECRET, host="127.0.0.1") is False


# ── 2026-09-02 P0: pooled session-GUC pollution (read-only backend) ──────────
#
# pgbouncer transaction pooling hands any backend to any client transaction and
# does NOT reset backend session state on the ordinary release path (measured on
# 1.25.2: server_reset_query_always=1 is inert there). A polluter's session-level
# `SET default_transaction_read_only = on` therefore leaks onto shared backends
# and the next borrower's writes fail with ReadOnlySqlTransaction — the
# 2026-09-02 P0 that 500'd the agent message / schedule-stop APIs. The client
# side must scrub the session back to baseline on every pooled use; these tests
# pin that contract end to end.


def _poison_backend(pooled: str) -> None:
    """One client session-SETs read_only on a pooled connection and disconnects
    cleanly — the exact pollution vector of the 2026-09-02 P0. With a
    single-backend pooler the next borrower is forced onto the poisoned backend."""
    with psycopg.connect(pooled, autocommit=True, prepare_threshold=None) as polluter:
        polluter.execute("SET default_transaction_read_only = on")


def test_pooled_borrow_scrubs_a_poisoned_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pool-level regression: a borrower must never inherit another client's
    session-level SET. The pool's `check` hook (every borrow) restores the
    baseline session, so a write after a poison succeeds — and the borrowed
    session reads `default_transaction_read_only = off`."""
    import shared.db as shared_db
    from shared import config

    with postgres() as pg_url, _pgbouncer_in_front(pg_url, pool_size=1) as pooled:
        monkeypatch.setattr(config.settings.data_plane, "db_url", pooled)
        pool = shared_db.pool(min_size=1, max_size=1)
        try:
            # Force the pool's physical backend into existence (creation runs the
            # configure hook) BEFORE poisoning, so the borrow-time check hook —
            # not backend birth — is what saves the borrower below.
            with pool.connection() as conn:
                conn.execute("SELECT 1")
            _poison_backend(pooled)
            with pool.connection() as conn, conn.cursor() as cur:
                row = cur.execute("SHOW default_transaction_read_only").fetchone()
                assert row is not None and str(row[0]) == "off"
                cur.execute("CREATE TEMP TABLE pgb_poison_probe(i int)")
                cur.execute("INSERT INTO pgb_poison_probe VALUES (1)")
        finally:
            pool.close()


def test_message_insert_and_schedule_stop_survive_a_poisoned_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0 batch-5 regression: the agent message INSERT and the schedule stop
    UPDATE — the two API writes the user saw 500 — succeed when pgbouncer hands
    their transaction a backend another client polluted read-only. Runs the real
    write helpers through the sanctioned pool path against a poisoned
    single-backend pooler."""
    import shared.db as shared_db
    from gateway.routers.schedules import _update_blocking
    from shared import config
    from shared.chat_delivery import insert_chat_inbound_once

    with postgres() as pg_url, _pgbouncer_in_front(pg_url, pool_size=1) as pooled:
        monkeypatch.setattr(config.settings.data_plane, "db_url", pooled)

        # The inbound wake publish needs Redis, which this harness does not run;
        # the regression target is the durable DB write, so stub the wake out.
        def _no_wake(*_args: object, **_kwargs: object) -> None:
            return None

        monkeypatch.setattr("shared.chat_delivery.publish_inbound_wake", _no_wake)
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
        pool = shared_db.pool(min_size=1, max_size=1)
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
                )
                assert receipt.inserted
            # schedule stop UPDATE (POST /api/schedules/{id}/stop durable half:
            # SELECT ... FOR UPDATE then UPDATE ... RETURNING)
            row, _enabled_changed = _update_blocking(pool, schedule_id, {"enabled": False})
            assert row[4] is False
        finally:
            pool.close()
