"""Shared transaction-pooling PgBouncer test harness in front of a throwaway Postgres."""

from __future__ import annotations

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
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from base.cluster.dataplane.pooler import pgbouncer_bin
from base.db import PG_STATEMENT_TIMEOUT_SET_SQL
from base.deploy.tests.containers import free_port, wait_port
from base.host.db_authority.delivery import POOLER_ADMIN

SECRET = "pgbouncerwiretestsecret"  # noqa: S105 — test fixture, not a real credential


def pgbouncer_available() -> bool:
    binary = pgbouncer_bin()
    return Path(binary).exists() or shutil.which(binary) is not None


@contextlib.contextmanager
def pgbouncer_in_front(
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
    # and cannot bind params; SECRET is a fixed alnum test constant, so inline it.
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(f"ALTER ROLE {role} PASSWORD '{SECRET}'")
        if read_only_default:
            conn.execute(
                sql.SQL("ALTER DATABASE {} SET default_transaction_read_only = on").format(
                    sql.Identifier(dbname)
                )
            )

    tmp = Path(tempfile.mkdtemp(prefix="ava-pgbouncer-test-"))
    listen_port = free_port()
    userlist = tmp / "userlist.txt"
    # Mirrors _render_ini: the admin console belongs to the pooler admin alone.
    userlist.write_text(f'"{role}" "{SECRET}"\n"{POOLER_ADMIN}" "{SECRET}"\n')
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
        wait_port(listen_port, host=listen_addr)
        pooled = f"postgresql://{role}:{SECRET}@{listen_addr}:{listen_port}/{dbname}"
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


def admin_console_url(pooled: str) -> str:
    """The admin console of a `pgbouncer_in_front` pooler, dialed as the
    pooler admin — its only `admin_users` entry (the pooled role is refused)."""
    return make_conninfo(pooled, user=POOLER_ADMIN, password=SECRET, dbname="pgbouncer")
