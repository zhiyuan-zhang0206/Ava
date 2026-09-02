"""Additive lifecycle schema preserves legacy rows and refuses lossy rollback."""

from pathlib import Path
from typing import LiteralString, cast
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

_MIGRATION = (
    Path(__file__).resolve().parents[2] / "migrations/20260902T190150_durable-lifecycle-intent"
)


def _shape(conn: psycopg.Connection) -> list[tuple[object, ...]]:
    return conn.execute(
        "SELECT c.relname,a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull "
        "FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
        "WHERE a.attrelid IN ('agents_meta'::regclass,'inbound_messages'::regclass) "
        "AND a.attname IN ('target_generation','target_owner','applied_at','observed_at',"
        "'lifecycle_command_id') AND NOT a.attisdropped ORDER BY 1,2"
    ).fetchall()


def test_lifecycle_schema_up_down_and_legacy_compatibility(db_conn: psycopg.Connection) -> None:
    expected = _shape(db_conn)
    schema = "lifecycle_" + uuid4().hex
    up = sql.SQL(cast(LiteralString, _MIGRATION.with_suffix(".sql").read_text()))
    down = sql.SQL(cast(LiteralString, _MIGRATION.with_suffix(".down.sql").read_text()))
    with db_conn.transaction(force_rollback=True):
        db_conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        db_conn.execute(
            sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(schema))
        )
        db_conn.execute("CREATE TABLE agents_meta(id bigint PRIMARY KEY)")
        db_conn.execute(
            "CREATE TABLE inbound_messages(id bigint PRIMARY KEY,agent_id bigint NOT NULL,"
            "kind text NOT NULL,status text NOT NULL,claimed_at timestamptz)"
        )
        db_conn.execute("INSERT INTO agents_meta VALUES(1)")
        db_conn.execute("INSERT INTO inbound_messages VALUES(2,1,'restart','done',now())")
        db_conn.execute(up)
        assert _shape(db_conn) == expected
        db_conn.execute(up)
        assert db_conn.execute(
            "SELECT target_generation,target_owner,applied_at,observed_at FROM inbound_messages"
        ).fetchone() == (None, None, None, None)
        db_conn.execute(down)
        assert _shape(db_conn) == []
        db_conn.execute(up)
        assert _shape(db_conn) == expected
        with pytest.raises(psycopg.errors.CheckViolation), db_conn.transaction():
            db_conn.execute("UPDATE inbound_messages SET target_generation=%s", (uuid4(),))
        db_conn.execute("UPDATE agents_meta SET lifecycle_command_id=2 WHERE id=1")
        with (
            pytest.raises(psycopg.errors.RaiseException, match="archive lifecycle"),
            db_conn.transaction(),
        ):
            db_conn.execute(down)


def test_lifecycle_schema_refuses_incompatible_existing_type(db_conn: psycopg.Connection) -> None:
    schema = "lifecycle_" + uuid4().hex
    with db_conn.transaction(force_rollback=True):
        db_conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        db_conn.execute(
            sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(schema))
        )
        db_conn.execute("CREATE TABLE agents_meta(id bigint PRIMARY KEY)")
        db_conn.execute("CREATE TABLE inbound_messages(id bigint PRIMARY KEY,target_owner text)")
        with (
            pytest.raises(psycopg.errors.RaiseException, match="incompatible lifecycle"),
            db_conn.transaction(),
        ):
            db_conn.execute(
                sql.SQL(cast(LiteralString, _MIGRATION.with_suffix(".sql").read_text()))
            )
