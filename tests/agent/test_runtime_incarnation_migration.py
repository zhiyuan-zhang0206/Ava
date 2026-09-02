"""Up/down shape equivalence and fail-loud baseline replay."""

from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

_MIGRATION = (
    Path(__file__).resolve().parents[2] / "migrations" / "20260902T174014_agent-runtime-incarnation"
)


def _shape(conn: psycopg.Connection) -> list[tuple[object, ...]]:
    return conn.execute(
        "SELECT attname, format_type(atttypid, atttypmod), attnotnull "
        "FROM pg_attribute WHERE attrelid = 'agents_meta'::regclass "
        "AND attname LIKE 'runtime_%' AND NOT attisdropped ORDER BY attname"
    ).fetchall()


def test_incarnation_migration_round_trip_matches_baseline(db_conn: psycopg.Connection) -> None:
    expected = _shape(db_conn)
    schema = "incarnation_" + uuid4().hex
    with db_conn.transaction(force_rollback=True):
        db_conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        db_conn.execute(
            sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(schema))
        )
        db_conn.execute("CREATE TABLE agents_meta (id bigint PRIMARY KEY)")
        up = _MIGRATION.with_suffix(".sql").read_text()
        down = _MIGRATION.with_suffix(".down.sql").read_text()
        db_conn.execute(sql.SQL(up))
        assert _shape(db_conn) == expected
        db_conn.execute(sql.SQL(up))  # current baseline + applied deltas must converge
        with pytest.raises(psycopg.errors.CheckViolation), db_conn.transaction():
            db_conn.execute("INSERT INTO agents_meta (id, runtime_kind) VALUES (1, 'invalid')")
        with pytest.raises(psycopg.errors.CheckViolation), db_conn.transaction():
            db_conn.execute("INSERT INTO agents_meta (id, runtime_protocol_version) VALUES (1, -1)")
        db_conn.execute(sql.SQL(down))
        assert _shape(db_conn) == []
        db_conn.execute(sql.SQL(up))
        assert _shape(db_conn) == expected


def test_incarnation_migration_rejects_incompatible_existing_type(
    db_conn: psycopg.Connection,
) -> None:
    schema = "incarnation_" + uuid4().hex
    with db_conn.transaction(force_rollback=True):
        db_conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        db_conn.execute(
            sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(schema))
        )
        db_conn.execute("CREATE TABLE agents_meta (id bigint, runtime_generation text)")
        with (
            pytest.raises(psycopg.errors.RaiseException, match="incompatible existing"),
            db_conn.transaction(),
        ):
            db_conn.execute(sql.SQL(_MIGRATION.with_suffix(".sql").read_text()))
