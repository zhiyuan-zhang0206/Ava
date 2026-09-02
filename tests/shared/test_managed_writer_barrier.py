"""Real PostgreSQL operation/inventory fences; no production state or processes."""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import LiteralString, cast
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from pydantic import ValidationError

from shared.managed_writer_barrier import (
    ManagedUnit,
    ManagedUnitClosure,
    ManagedWriterBarrierError,
    ManagedWriterCollection,
    RolloutIdentity,
    lock_rollout,
    record_collection,
)


@pytest.fixture
def barrier_db(db_conn: psycopg.Connection) -> Iterator[psycopg.Connection]:
    # Temporary tables shadow only this connection's public fixture tables.
    db_conn.execute(
        "CREATE TEMP TABLE deployment_state (LIKE public.deployment_state INCLUDING ALL)"
    )
    db_conn.execute("CREATE TEMP TABLE machines (LIKE public.machines INCLUDING ALL)")
    db_conn.execute("CREATE TEMP TABLE machine_units (LIKE public.machine_units INCLUDING ALL)")
    db_conn.execute("INSERT INTO deployment_state(id) VALUES (1)")
    db_conn.execute(
        "INSERT INTO machines(name, paused_at, is_staging) VALUES ('runner', now(), true)"
    )
    db_conn.execute(
        "INSERT INTO machine_units(machine_name,home,stopped_at) VALUES ('runner','/ava',now())"
    )
    db_conn.commit()
    try:
        yield db_conn
    finally:
        db_conn.rollback()
        db_conn.execute(
            "DROP TABLE pg_temp.deployment_state, pg_temp.machines, pg_temp.machine_units"
        )
        db_conn.commit()


def _collection(conn: psycopg.Connection) -> ManagedWriterCollection:
    row = conn.execute(
        "UPDATE deployment_state SET phase='updating',kind='rollout',holder='candidate',"
        "acquired_at=clock_timestamp(),expires_at=clock_timestamp()+interval '1 minute',"
        "target_sha=%s RETURNING acquired_at",
        ("a" * 40,),
    ).fetchone()
    assert row is not None
    at = row[0]
    return ManagedWriterCollection(
        operation=RolloutIdentity(holder="candidate", acquired_at=at, target_sha="a" * 40),
        candidate_digest="b" * 64,
        challenge=uuid4(),
        collected_at=at,
        valid_until=at + timedelta(minutes=1),
        units=(
            ManagedUnitClosure(
                unit=ManagedUnit(machine="runner", home="/ava", inventory_digest="c" * 64),
                boot_id=uuid4(),
                observer_instance=uuid4(),
                observation_digest="d" * 64,
                outcome="old_writers_absent_relaunchers_fenced",
            ),
        ),
    )


def _record(conn: psycopg.Connection, collection: ManagedWriterCollection) -> None:
    record_collection(
        conn,
        collection,
        operation=collection.operation,
        candidate_digest=collection.candidate_digest,
        expected_challenge=collection.challenge,
        prepared_units=tuple(entry.unit for entry in collection.units),
    )


def test_stopped_paused_staging_unit_is_required(barrier_db: psycopg.Connection) -> None:
    collection = _collection(barrier_db)
    _record(barrier_db, collection)
    row = barrier_db.execute("SELECT managed_writer_evidence FROM deployment_state").fetchone()
    assert row is not None
    assert row[0] == collection.model_dump(mode="json")
    # An unchanged retry is idempotent but still checks the live operation.
    _record(barrier_db, collection)


@pytest.mark.parametrize("change", ["lease", "target", "roster", "orphan", "inventory"])
def test_changed_authority_or_inventory_refuses(
    barrier_db: psycopg.Connection, change: str
) -> None:
    collection = _collection(barrier_db)
    if change == "lease":
        barrier_db.execute(
            "UPDATE deployment_state SET acquired_at=acquired_at+interval '1 second'"
        )
    elif change == "target":
        barrier_db.execute("UPDATE deployment_state SET target_sha=%s", ("e" * 40,))
    elif change == "roster":
        barrier_db.execute(
            "INSERT INTO machine_units(machine_name,home) VALUES ('runner','/other')"
        )
    elif change == "orphan":
        barrier_db.execute("INSERT INTO machines(name) VALUES ('orphan')")
    else:
        barrier_db.execute("UPDATE machine_units SET home='/changed'")
    with pytest.raises(ManagedWriterBarrierError):
        _record(barrier_db, collection)
    assert barrier_db.execute(
        "SELECT managed_writer_evidence IS NULL FROM deployment_state"
    ).fetchone() == (True,)


def test_replayed_challenge_and_expired_collection_refuse(barrier_db: psycopg.Connection) -> None:
    collection = _collection(barrier_db)
    with pytest.raises(ManagedWriterBarrierError, match="challenge"):
        record_collection(
            barrier_db,
            collection,
            operation=collection.operation,
            candidate_digest=collection.candidate_digest,
            expected_challenge=uuid4(),
            prepared_units=(collection.units[0].unit,),
        )
    expired = collection.model_copy(update={"valid_until": collection.collected_at})
    with pytest.raises(ManagedWriterBarrierError, match="expired"):
        _record(barrier_db, expired)


def test_pre_schema_fence_does_not_read_new_column(barrier_db: psycopg.Connection) -> None:
    collection = _collection(barrier_db)
    barrier_db.execute("ALTER TABLE deployment_state DROP COLUMN managed_writer_evidence")
    assert lock_rollout(barrier_db, collection.operation) >= collection.collected_at


def test_delta_matches_baseline_and_down_refuses_evidence(barrier_db: psycopg.Connection) -> None:
    directory = Path(__file__).resolve().parents[2] / "migrations"
    name = "20260902T201145_managed-writer-evidence"
    up = cast(LiteralString, (directory / f"{name}.sql").read_text())
    down = cast(LiteralString, (directory / f"{name}.down.sql").read_text())
    collection = _collection(barrier_db)
    baseline = barrier_db.execute(
        "SELECT format_type(atttypid,atttypmod),attnotnull FROM pg_attribute "
        "WHERE attrelid='deployment_state'::regclass AND attname='managed_writer_evidence'"
    ).fetchone()
    barrier_db.execute("ALTER TABLE deployment_state DROP COLUMN managed_writer_evidence")
    barrier_db.execute(up)  # trusted repository migration, not caller SQL
    migrated = barrier_db.execute(
        "SELECT format_type(atttypid,atttypmod),attnotnull FROM pg_attribute "
        "WHERE attrelid='deployment_state'::regclass AND attname='managed_writer_evidence'"
    ).fetchone()
    assert migrated == baseline == ("jsonb", False)
    _record(barrier_db, collection)
    with (
        pytest.raises(psycopg.errors.RaiseException, match="explicitly retired"),
        barrier_db.transaction(),
    ):
        barrier_db.execute(down)
    barrier_db.execute("UPDATE deployment_state SET managed_writer_evidence=NULL")
    barrier_db.execute(down)


def test_unknown_fields_and_unsafe_home_refuse() -> None:
    with pytest.raises(ValidationError):
        ManagedUnit(machine="runner", home="/ava/../other", inventory_digest="a" * 64)
    with pytest.raises(ValidationError):
        ManagedUnit.model_validate(
            {"machine": "runner", "home": "/ava", "inventory_digest": "a" * 64, "online": True}
        )


def test_inventory_lock_wait_cannot_extend_expired_operation(db_conn: psycopg.Connection) -> None:
    """Two real connections: inventory waits must recheck time after acquiring it."""
    schema = "barrier_" + uuid4().hex
    db_conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    for table in ("deployment_state", "machines", "machine_units"):
        db_conn.execute(
            sql.SQL("CREATE TABLE {}.{} (LIKE public.{} INCLUDING ALL)").format(
                sql.Identifier(schema), sql.Identifier(table), sql.Identifier(table)
            )
        )
    db_conn.execute("SELECT set_config('search_path',%s,false)", (schema + ",public",))
    db_conn.execute("INSERT INTO deployment_state(id) VALUES (1)")
    db_conn.execute("INSERT INTO machines(name) VALUES ('runner')")
    db_conn.execute("INSERT INTO machine_units(machine_name,home) VALUES ('runner','/ava')")
    collection = _collection(db_conn)
    db_conn.execute("UPDATE deployment_state SET expires_at=clock_timestamp()+interval '1 second'")
    db_conn.commit()
    try:
        with psycopg.connect(db_conn.info.dsn) as worker:
            worker.execute("SELECT set_config('search_path',%s,false)", (schema + ",public",))
            worker.commit()
            worker_pid = worker.info.backend_pid
            db_conn.execute("LOCK TABLE machine_units IN ACCESS EXCLUSIVE MODE")
            with ThreadPoolExecutor(max_workers=1) as executor:

                def adopt() -> None:
                    with worker.transaction():
                        _record(worker, collection)

                future = executor.submit(adopt)
                deadline = time.monotonic() + 5
                while True:
                    row = db_conn.execute(
                        "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=%s AND NOT granted)",
                        (worker_pid,),
                    ).fetchone()
                    if row == (True,):
                        break
                    if time.monotonic() >= deadline:
                        db_conn.rollback()
                        raise AssertionError("worker never blocked on the inventory lock")
                    time.sleep(0.01)
                # Wait for actual server-side lease expiry while retaining the lock.
                db_conn.execute(
                    "SELECT pg_sleep(GREATEST(0,EXTRACT(EPOCH FROM "
                    "(expires_at-clock_timestamp())))+0.01) FROM deployment_state"
                )
                db_conn.commit()
                with pytest.raises(ManagedWriterBarrierError, match="live rollout"):
                    future.result(timeout=5)
            assert db_conn.execute(
                "SELECT managed_writer_evidence IS NULL FROM deployment_state"
            ).fetchone() == (True,)
    finally:
        db_conn.rollback()
        db_conn.execute("SELECT set_config('search_path','public',false)")
        db_conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        db_conn.commit()
