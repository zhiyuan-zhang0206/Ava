"""Resource evidence is nullable unknown and cannot be retired after use."""

import threading
import time
from pathlib import Path
from typing import LiteralString, cast
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

_MIGRATION = (
    Path(__file__).resolve().parents[2] / "migrations/20260903T020938_incarnation-resources"
)


def _body(suffix: str) -> sql.SQL:
    return sql.SQL(cast(LiteralString, _MIGRATION.with_suffix(suffix).read_text()))


def _shape(conn: psycopg.Connection) -> list[tuple[object, ...]]:
    return conn.execute(
        "SELECT format_type(atttypid,atttypmod),attnotnull FROM pg_attribute "
        "WHERE attrelid='agents_meta'::regclass AND attname='incarnation_resources' "
        "AND NOT attisdropped"
    ).fetchall()


def test_resource_migration_matches_baseline_and_preserves_unknown(
    db_conn: psycopg.Connection,
) -> None:
    expected = _shape(db_conn)
    assert expected == [("jsonb", False)]
    with db_conn.transaction(force_rollback=True):
        schema = sql.Identifier("resources_" + uuid4().hex)
        db_conn.execute(sql.SQL("CREATE SCHEMA {}").format(schema))
        db_conn.execute(sql.SQL("SET LOCAL search_path TO {},public").format(schema))
        db_conn.execute("CREATE TABLE agents_meta(id bigint PRIMARY KEY)")
        db_conn.execute("INSERT INTO agents_meta VALUES(1)")
        with pytest.raises(psycopg.errors.UndefinedColumn), db_conn.transaction():
            db_conn.execute("SELECT incarnation_resources FROM agents_meta")
        db_conn.execute(_body(".sql"))
        db_conn.execute(_body(".sql"))
        assert _shape(db_conn) == expected
        assert db_conn.execute("SELECT incarnation_resources FROM agents_meta").fetchone() == (
            None,
        )
        db_conn.execute(_body(".down.sql"))
        assert _shape(db_conn) == []
        db_conn.execute(_body(".sql"))
        assert _shape(db_conn) == expected


def test_resource_migration_rejects_implicit_empty_default(db_conn: psycopg.Connection) -> None:
    with db_conn.transaction(force_rollback=True):
        schema = sql.Identifier("resources_" + uuid4().hex)
        db_conn.execute(sql.SQL("CREATE SCHEMA {}").format(schema))
        db_conn.execute(sql.SQL("SET LOCAL search_path TO {},public").format(schema))
        db_conn.execute(
            "CREATE TABLE agents_meta(id bigint,incarnation_resources jsonb DEFAULT '{}')"
        )
        with (
            pytest.raises(psycopg.errors.RaiseException, match="incompatible existing"),
            db_conn.transaction(),
        ):
            db_conn.execute(_body(".sql"))


@pytest.mark.parametrize("evidence", ["{}", '{"version":1,"requests":{}}', '"corrupt"'])
def test_resource_down_never_discards_used_or_malformed_evidence(
    db_conn: psycopg.Connection,
    evidence: str,
) -> None:
    with db_conn.transaction(force_rollback=True):
        schema = sql.Identifier("resources_" + uuid4().hex)
        db_conn.execute(sql.SQL("CREATE SCHEMA {}").format(schema))
        db_conn.execute(sql.SQL("SET LOCAL search_path TO {},public").format(schema))
        db_conn.execute("CREATE TABLE agents_meta(id bigint,incarnation_resources jsonb)")
        db_conn.execute("INSERT INTO agents_meta VALUES(1,%s::jsonb)", (evidence,))
        with (
            pytest.raises(psycopg.errors.RaiseException, match="writer retirement"),
            db_conn.transaction(),
        ):
            db_conn.execute(_body(".down.sql"))
        assert _shape(db_conn) == [("jsonb", False)]
        assert db_conn.execute(
            "SELECT incarnation_resources=%s::jsonb FROM agents_meta", (evidence,)
        ).fetchone() == (True,)


def test_down_waits_for_writer_then_checks_committed_evidence(db_conn: psycopg.Connection) -> None:
    """Real concurrent adoption cannot slip between the check and DROP."""
    schema = "resources_" + uuid4().hex
    db_conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    db_conn.execute(
        sql.SQL("CREATE TABLE {}.agents_meta(id bigint,incarnation_resources jsonb)").format(
            sql.Identifier(schema)
        )
    )
    db_conn.execute(
        sql.SQL("INSERT INTO {}.agents_meta VALUES(1,NULL)").format(sql.Identifier(schema))
    )
    db_conn.commit()
    failures: list[BaseException] = []
    started = threading.Event()
    try:
        with (
            psycopg.connect(db_conn.info.dsn) as writer,
            psycopg.connect(db_conn.info.dsn) as rollback,
        ):
            for connection in (writer, rollback):
                connection.execute("SELECT set_config('search_path',%s,false)", (schema,))
                connection.commit()
            writer.execute("UPDATE agents_meta SET incarnation_resources='{}'::jsonb WHERE id=1")

            def down() -> None:
                try:
                    with rollback.transaction():
                        started.set()
                        rollback.execute(_body(".down.sql"))
                except BaseException as exc:
                    failures.append(exc)

            thread = threading.Thread(target=down)
            thread.start()
            assert started.wait(2)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                waiting = db_conn.execute(
                    "SELECT 1 FROM pg_locks WHERE pid=%s AND mode='AccessExclusiveLock' AND NOT granted",
                    (rollback.info.backend_pid,),
                ).fetchone()
                db_conn.commit()
                if waiting:
                    break
                time.sleep(0.01)
            else:
                raise AssertionError("down never waited for the actual writer lock")
            writer.commit()
            thread.join(5)
            assert not thread.is_alive()
        assert len(failures) == 1 and isinstance(failures[0], psycopg.errors.RaiseException)
        assert "writer retirement" in str(failures[0])
        assert db_conn.execute(
            sql.SQL("SELECT incarnation_resources FROM {}.agents_meta").format(
                sql.Identifier(schema)
            )
        ).fetchone() == ({},)
        db_conn.commit()
    finally:
        db_conn.rollback()
        db_conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        db_conn.commit()
