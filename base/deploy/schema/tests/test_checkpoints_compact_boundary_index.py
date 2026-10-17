from __future__ import annotations

from typing import LiteralString, cast

import psycopg

from base.cluster.machine import MachineIdentity
from base.deploy.schema.migrations import apply_pending_migrations
from base.deploy.schema.tests.migration_support import SCHEMA_SQL, throwaway_database

_BOUNDARY_INDEX = "20261017T203552_checkpoints-compact-boundary-index"
_CREATE_CHECKPOINTS = """
CREATE TABLE checkpoints (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    metadata JSONB,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
)
"""


def test_compact_boundary_index_upgrade_indexes_the_upstream_table(
    db_url: str,
    machine_identity: MachineIdentity,
) -> None:
    # meiva:note
    # The boundary index is a migration on a table the saver owns. `checkpoints` is
    # not part of `db/schema.sql`; a real cluster has it because a birth runs
    # `PostgresSaver.setup()` before migrations. This fixture shapes the upstream
    # table first so the upgrade path the index is written for is what gets
    # exercised, and a second apply proves the migration is idempotent.
    _ = db_url
    with throwaway_database("checkpoint_boundary_index") as url:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(cast(LiteralString, SCHEMA_SQL.read_text()))
            conn.execute(_CREATE_CHECKPOINTS)
        with psycopg.connect(url) as conn:
            applied = apply_pending_migrations(conn, machine_name=machine_identity.name)
            assert _BOUNDARY_INDEX in applied
            assert conn.execute(
                "SELECT indisvalid FROM pg_index"
                " WHERE indexrelid = to_regclass('public.checkpoints_compact_boundary_idx')"
            ).fetchone() == (True,)
        with psycopg.connect(url) as conn:
            assert apply_pending_migrations(conn, machine_name=machine_identity.name) == []


def test_compact_boundary_index_is_skipped_without_the_upstream_table(
    db_url: str,
    machine_identity: MachineIdentity,
) -> None:
    # meiva:note
    # A schema-only database has no `checkpoints`; the migration must not fail.
    # That database is the migration test fixture's shape, never a cluster: a birth
    # creates the table before migrations run, so the guard skips nothing that a
    # real cluster needs.
    _ = db_url
    with throwaway_database("checkpoint_boundary_guard") as url:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(cast(LiteralString, SCHEMA_SQL.read_text()))
        with psycopg.connect(url) as conn:
            applied = apply_pending_migrations(conn, machine_name=machine_identity.name)
            assert _BOUNDARY_INDEX in applied
            assert conn.execute(
                "SELECT to_regclass('public.checkpoints_compact_boundary_idx')"
            ).fetchone() == (None,)
