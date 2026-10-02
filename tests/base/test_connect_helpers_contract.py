"""Contract: the migration restores the column the baseline schema, db/schema.sql, gives a fresh database."""

from __future__ import annotations

from typing import LiteralString, cast

import psycopg

from base.config import settings


def test_the_migration_restores_the_column_where_the_baseline_has_it() -> None:
    """With the column dropped, the migration restores it (twice over: it is
    idempotent) with the shape and comment `db/schema.sql` gives a fresh database.
    Run inside one transaction, so the suite database is untouched."""
    from base.paths import repo_root

    (up_file,) = (repo_root() / "migrations").glob("*_min-code-version.sql")
    column = (
        "SELECT data_type, is_nullable, column_default, "
        "col_description('deployment_state'::regclass, ordinal_position::int) "
        "FROM information_schema.columns "
        "WHERE table_name = 'deployment_state' AND column_name = 'min_code_version'"
    )
    with psycopg.connect(settings.data_plane.db_url) as conn:
        try:
            baseline = conn.execute(column).fetchone()
            assert baseline is not None and baseline[:3] == ("bigint", "NO", "0")

            conn.execute("ALTER TABLE deployment_state DROP COLUMN min_code_version")
            assert conn.execute(column).fetchone() is None

            up = cast(LiteralString, up_file.read_text())
            conn.execute(up)
            conn.execute(up)
            assert conn.execute(column).fetchone() == baseline
        finally:
            conn.rollback()
