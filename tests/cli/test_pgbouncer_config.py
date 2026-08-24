"""PgBouncer config generation + the direct-connection exemption contract.

Unit-level: the rendered pgbouncer.ini / userlist.txt content and the code paths
that MUST bypass the pooler. The end-to-end wire behaviour (a real pgbouncer in
transaction pooling in front of Postgres) lives in test_pgbouncer_wire.py.
"""

from __future__ import annotations

import inspect

from cli.commands import _pgbouncer


def test_render_userlist_quotes_role_and_secret() -> None:
    line = _pgbouncer._render_userlist("ava_main", "s3cr3t")
    # PgBouncer double-quotes both fields; scram client auth derives from the plaintext.
    assert line == '"ava_main" "s3cr3t"\n'


def test_render_userlist_escapes_embedded_quote() -> None:
    line = _pgbouncer._render_userlist("ava_main", 'a"b')
    assert line == '"ava_main" "a""b"\n'


def test_render_ini_is_transaction_scram_and_socket_server() -> None:
    ini = _pgbouncer._render_ini(
        pg_port=5433,
        listen_port=6433,
        db_name="ava_main",
        role="ava_main",
        cluster_secret="s3cr3t",  # noqa: S106 — test fixture
    )
    # Transaction pooling is the whole point.
    assert "pool_mode = transaction" in ini
    # Client auth is scram against the userlist; the pooled front door needs the secret.
    assert "auth_type = scram-sha-256" in ini
    assert f"auth_file = {_pgbouncer._userlist_path()}" in ini
    assert "listen_port = 6433" in ini
    # The [databases] entry keys on the cluster db and forwards to the local pg over
    # its trust unix socket (host=<socket dir>), so the server hop needs no credential.
    assert "[databases]" in ini
    assert "ava_main = host=/" in ini and "port=5433 dbname=ava_main" in ini
    # Every pooled backend is born with the statement ceiling (the pooler drops
    # the client's `options` startup parameter — the connect_query SET is the
    # one pooler-side delivery path; see shared.db.PG_STATEMENT_TIMEOUT_SET_SQL).
    assert "connect_query='SET statement_timeout = 60000'" in ini
    assert "log_connections = 0" in ini.splitlines()
    assert "log_disconnections = 0" in ini.splitlines()
    # admin/stats console is the cluster role.
    assert "admin_users = ava_main" in ini


def test_render_ini_is_trust_without_secret() -> None:
    """A no-secret cluster has no credential for scram — the pooled front door
    must not demand one (the cluster's whole posture is unauthenticated)."""
    ini = _pgbouncer._render_ini(
        pg_port=5433,
        listen_port=6433,
        db_name="ava_main",
        role="ava_main",
        cluster_secret="",
    )
    assert "auth_type = trust" in ini
    assert "auth_type = scram-sha-256" not in ini


def test_render_ini_binds_loopback_never_all_interfaces() -> None:
    ini = _pgbouncer._render_ini(
        pg_port=5433,
        listen_port=6433,
        db_name="ava_main",
        role="ava_main",
        cluster_secret="s3cr3t",  # noqa: S106 — test fixture
    )
    listen = next(ln for ln in ini.splitlines() if ln.startswith("listen_addr ="))
    assert "127.0.0.1" in listen
    assert "0.0.0.0" not in listen and "*" not in listen  # noqa: S104 — asserting we do NOT bind all interfaces


# ── direct-connection exemptions (the admin plane must never route through PgBouncer) ──


def test_migrations_apply_uses_direct_unbounded_connection() -> None:
    """The migration applier holds a SESSION advisory lock across its apply loop; a
    transaction pooler would drop it. It must open a direct connection, and its
    DDL may exceed the 60s statement ceiling — the dial must be unbounded too."""
    from cli.commands import migrations

    src = inspect.getsource(migrations.cmd_migrations_apply)
    assert "connect(direct=True, unbounded=True)" in src


def test_update_git_migration_paths_use_direct_unbounded_connection() -> None:
    """The update/rollback migration wrappers (`ava cluster update` /
    `ava cluster rollback` / failed-update recovery) are migration paths too:
    apply_pending_migrations and rollback_schema_to hold the SESSION advisory
    lock and run DDL, so they must dial direct + unbounded; the schema snapshot
    is part of the admin update path and must read the real Postgres (direct),
    but stays bounded (a plain read)."""
    from cli.commands import _update_git

    apply_src = inspect.getsource(_update_git.apply_pending_migrations)
    assert "connect(direct=True, unbounded=True)" in apply_src
    rollback_src = inspect.getsource(_update_git.rollback_schema_to)
    assert "connect(direct=True, unbounded=True)" in rollback_src
    snapshot_src = inspect.getsource(_update_git.current_schema_state)
    assert "connect(direct=True)" in snapshot_src
    assert "unbounded" not in snapshot_src


def test_backup_defaults_to_direct_db_url() -> None:
    """pg_dump needs a real Postgres session (consistent snapshot); it must use the
    admin-plane direct URL (shared.db.direct_db_url), never the one URL as-is
    (AVA_DB_URL carries the pooler port when pooling is on)."""
    from services import backup

    src = inspect.getsource(backup._run_backup)
    assert "direct_db_url" in src
    assert ".pooled_db_url" not in src


def test_shared_db_connect_and_pool_dial_one_url_with_direct_escape() -> None:
    """shared.db.connect/pool dial AVA_DB_URL (the one access URL) by default and
    expose direct=True (the admin plane derives the direct URL from the registry
    record); every connection disables server-side prepared statements
    (transaction-pooling safe). connect() also exposes unbounded=True — the
    migration applier's no-statement-ceiling escape — and pooled dials deliver
    the ceiling as an explicit SET (the pooler drops the `options` parameter)."""
    import shared.db

    for fn in (shared.db.connect, shared.db.pool):
        assert "direct" in inspect.signature(fn).parameters
        src = inspect.getsource(fn)
        assert "dp.db_url if not direct else direct_db_url()" in src
        assert "prepare_threshold" in src
    assert "unbounded" in inspect.signature(shared.db.connect).parameters
    assert "PG_STATEMENT_TIMEOUT_SET_SQL" in inspect.getsource(shared.db.connect)
    assert "configure=_apply_statement_timeout" in inspect.getsource(shared.db.pool)
