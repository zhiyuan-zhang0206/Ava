"""PgBouncer config generation + the direct-connection exemption contract.

Unit-level: the rendered pgbouncer.ini content (the userlist is rendered by
`base.cluster.authority.render_userlist`), and the code paths
that MUST bypass the pooler. The end-to-end wire behaviour (a real pgbouncer in
transaction pooling in front of Postgres) lives in test_pgbouncer_wire.py.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

from base.db import Database, DbConfig
from base.db.code_version_gate import ProcessDbGate
from cli.commands.data_plane import pgbouncer


def _assert_transaction_pooling_with_scram_clients(ini: str) -> None:
    # Transaction pooling is the whole point.
    assert "pool_mode = transaction" in ini
    # Client auth is scram against the userlist of generation verifiers.
    assert "auth_type = scram-sha-256" in ini
    assert f"auth_file = {pgbouncer._userlist_path()}" in ini
    assert "listen_port = 6433" in ini


def _assert_database_entry_forwards_over_owner_only_socket(ini: str) -> None:
    # The [databases] entry keys on the cluster db and forwards to the local pg over
    # its owner-only unix socket (host=<socket dir>) with SCRAM pass-through.
    assert "[databases]" in ini
    assert "ava_main = host=/" in ini
    assert "port=5433 dbname=ava_main" in ini


def _assert_backends_born_with_statement_ceiling(ini: str) -> None:
    # Every pooled backend is born with the statement ceiling (the pooler drops
    # the client's `options` startup parameter — the connect_query SET is the
    # one pooler-side delivery path; see base.db.PG_STATEMENT_TIMEOUT_SET_SQL).
    assert "connect_query='SET statement_timeout = 60000'" in ini


def _assert_release_reset_is_one_unquoted_discard_all(ini: str) -> None:
    # A backend whose client vanished mid-transaction is scrubbed back to its
    # connect_query-fresh state by the release reset: a session-level GUC (e.g.
    # a polluter's SET default_transaction_read_only = on) must never reach the
    # next borrower of a pooled backend. always=0 (explicit) keeps the reset to
    # that SV_ACTIVE window — always=1 fires after every transaction and its
    # DISCARD ALL wiped the client's own dial/borrow-time SETs (the statement
    # ceiling; measured 2026-09-03, 405 ruling option B). Between-transaction
    # pollution is defended client-side by base/db/__init__.py's baseline restore
    # (2026-09-02 P0 incident).
    assert "server_reset_query = DISCARD ALL" in ini
    # The reset is one statement and unquoted: pgbouncer 1.25.2 runs
    # server_reset_query verbatim, so a quoted value reaches Postgres as a
    # syntax error (2026-09-02 P0, ~18M errors), and a multi-statement value is
    # wrapped in an implicit transaction by transaction pooling — "DISCARD ALL
    # cannot run inside a transaction block" (measured 2026-09-02 02:21).
    assert "server_reset_query = DISCARD ALL; SET" not in ini
    assert "server_reset_query_always = 0" in ini
    # The reset does NOT re-apply the statement ceiling: DISCARD ALL clears the
    # birth-time connect_query SET, and the reset would need a second statement
    # to re-apply it — a shape transaction pooling rejects. The ceiling is
    # delivered by connect_query (every backend at birth) and by base/db/__init__.py's
    # client-side SET on every pooled use.
    reset_line = next(ln for ln in ini.splitlines() if ln.startswith("server_reset_query ="))
    assert reset_line == "server_reset_query = DISCARD ALL"


def _assert_quiet_connection_logs_and_userlist_only_console(ini: str) -> None:
    assert "log_connections = 0" in ini.splitlines()
    assert "log_disconnections = 0" in ini.splitlines()
    # admin/stats console is the userlist-only operator entry, never a database role.
    console = {"admin_users = ava_pooler_admin", "stats_users = ava_pooler_admin"}
    assert console <= set(ini.splitlines())


def test_render_ini_is_transaction_scram_and_socket_server() -> None:
    ini = pgbouncer._render_ini(
        pg_port=5433,
        listen_port=6433,
        db_name="ava_main",
        cluster_secret="s3cr3t",  # noqa: S106 — test fixture
    )
    _assert_transaction_pooling_with_scram_clients(ini)
    _assert_database_entry_forwards_over_owner_only_socket(ini)
    _assert_backends_born_with_statement_ceiling(ini)
    _assert_release_reset_is_one_unquoted_discard_all(ini)
    _assert_quiet_connection_logs_and_userlist_only_console(ini)


def test_render_ini_authenticates_without_secret_and_binds_loopback_only() -> None:
    """The internal data plane always authenticates: an empty cluster secret
    changes only the bind (loopback alone), never the SCRAM front door."""
    ini = pgbouncer._render_ini(pg_port=5433, listen_port=6433, db_name="ava", cluster_secret="")
    assert "auth_type = scram-sha-256" in ini.splitlines()
    assert "trust" not in ini
    assert "listen_addr = 127.0.0.1" in ini.splitlines()


def test_render_ini_binds_loopback_never_all_interfaces() -> None:
    ini = pgbouncer._render_ini(
        pg_port=5433,
        listen_port=6433,
        db_name="ava_main",
        cluster_secret="s3cr3t",  # noqa: S106 — test fixture
    )
    listen = next(ln for ln in ini.splitlines() if ln.startswith("listen_addr ="))
    assert "127.0.0.1" in listen
    assert "0.0.0.0" not in listen and "*" not in listen  # noqa: S104 — asserting we do NOT bind all interfaces


# ── direct-connection exemptions (the admin plane must never route through PgBouncer) ──


def test_migrations_apply_uses_direct_unbounded_connection() -> None:
    """The migration applier holds a SESSION advisory lock across its apply loop; a
    transaction pooler would drop it. It must open a direct connection, and its
    DDL may exceed the 60s statement ceiling — the dial must be unbounded too. A
    remote plane dials its provider URL that way; a local plane dials the owner
    authority over the postmaster's own socket, which carries no ceiling (proved
    on real Postgres in tests/components/base/test_pg_owner_authority.py)."""
    from cli.commands.lifecycle import migrations

    src = inspect.getsource(migrations.cmd_migrations_apply)
    assert "connect(direct=True, unbounded=True)" in src
    assert "local_owner_authority()" in src


@pytest.mark.parametrize("remote", [False, True])
def test_backup_defaults_to_a_direct_dump_source(
    monkeypatch: pytest.MonkeyPatch, remote: bool
) -> None:
    """The explicit plane reader selects either provider or local owner authority."""
    from services.backup import dump as backup

    database = MagicMock(spec=Database)
    database.direct_url.return_value = "postgresql://provider:5432/ava"
    authority = MagicMock()
    authority.verified_conninfo.return_value = "host=/owner/socket dbname=ava"
    local_owner = MagicMock(return_value=authority)
    monkeypatch.setattr(backup, "local_owner_authority", local_owner)
    reader = MagicMock(return_value=remote)

    result = backup.dump_source(database, is_remote_reader=reader)

    reader.assert_called_once_with()
    if remote:
        assert result == "postgresql://provider:5432/ava"
        database.direct_url.assert_called_once_with()
        local_owner.assert_not_called()
    else:
        assert result == "host=/owner/socket dbname=ava"
        local_owner.assert_called_once_with()
        authority.verified_conninfo.assert_called_once_with()
        database.direct_url.assert_not_called()


@pytest.mark.parametrize("direct", [False, True])
def test_base_db_connect_and_pool_dial_one_url_with_direct_escape(
    monkeypatch: pytest.MonkeyPatch, direct: bool
) -> None:
    """Dial the access URL or its recorded direct port with pooling-safe policy."""
    import base.cluster
    import base.db
    from base.db import connections

    config = DbConfig(
        db_url="postgresql://user:password@127.0.0.1:6433/ava",
        db_sslmode="disable",
        db_pool_min_size=1,
        db_pool_max_size=2,
        pgbouncer_enabled=True,
    )

    def record(_home: Path | None) -> SimpleNamespace:
        return SimpleNamespace(ports={"pgbouncer": 6433, "postgres": 5433})

    monkeypatch.setattr(base.cluster, "get_record", record)
    gate = MagicMock(spec=ProcessDbGate)
    gate.application_name.return_value = "pgbouncer-contract-test"
    gate.min_read_due.return_value = False
    conn = MagicMock()
    dial = MagicMock(return_value=conn)
    make_pool = MagicMock()
    monkeypatch.setattr(connections.psycopg, "connect", dial)
    monkeypatch.setattr(connections, "ConnectionPool", make_pool)
    expected_url = config.db_url.replace(":6433/", ":5433/") if direct else config.db_url

    assert base.db.connect(config=config, gate=gate, direct=direct) is conn
    assert dial.call_args.args == (expected_url,)
    assert dial.call_args.kwargs["prepare_threshold"] is None
    if direct:
        conn.execute.assert_not_called()
    else:
        _assert_session_reset(conn)

    base.db.pool(config=config, gate=gate, direct=direct)
    assert make_pool.call_args.args == (expected_url,)
    _assert_pool_hooks(make_pool, conn, direct=direct)

    if direct:
        dial.reset_mock()
        base.db.connect(config=config, gate=gate, direct=True, unbounded=True)
        assert dial.call_args.args == (expected_url,)
        assert "options" not in dial.call_args.kwargs
        assert dial.call_args.kwargs["prepare_threshold"] is None


def _assert_pool_hooks(make_pool: MagicMock, conn: MagicMock, *, direct: bool) -> None:
    """Pooled configure/check share the restore contract; direct owners bypass it."""
    policy = make_pool.call_args.kwargs
    assert policy["kwargs"]["prepare_threshold"] is None
    if direct:
        assert policy["configure"] is None
        assert policy["check"] is None
    else:
        assert policy["configure"] is policy["check"]
        for hook in (policy["configure"], policy["check"]):
            conn.reset_mock()
            hook(conn)
            _assert_session_reset(conn)


def _assert_session_reset(conn: MagicMock) -> None:
    """A dial and both pool hooks scrub and reapply the statement ceiling."""
    assert conn.execute.call_args_list == [
        call("RESET ALL"),
        call(
            "SELECT set_config('statement_timeout', '60000', false), "
            "set_config('application_name', %s, false)",
            ("pgbouncer-contract-test",),
        ),
    ]
    conn.commit.assert_called_once_with()
