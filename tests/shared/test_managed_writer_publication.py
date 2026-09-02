"""Real PostgreSQL publication transitions; fixture permits are not activation."""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from shared.managed_writer_barrier import (
    ManagedUnit,
    ManagedUnitClosure,
    ManagedWriterBarrierError,
    ManagedWriterCollection,
    RolloutIdentity,
)
from shared.managed_writer_publication import (
    CommittedPublication,
    PendingPublication,
    PublishedUnit,
    WriterPublication,
    adopt_pending_collection,
    begin_pending_publication,
    recover_pending_publication,
    require_current_publication,
)


@pytest.fixture
def publication_db(db_conn: psycopg.Connection) -> Iterator[psycopg.Connection]:
    for table in ("deployment_state", "machines", "machine_units"):
        # Fixed test identifiers only; no external input or production schema writes.
        db_conn.execute(
            sql.SQL("CREATE TEMP TABLE {} (LIKE public.{} INCLUDING ALL)").format(
                sql.Identifier(table), sql.Identifier(table)
            )
        )
    db_conn.execute("INSERT INTO deployment_state(id) VALUES(1)")
    db_conn.execute("INSERT INTO machines(name,paused_at) VALUES('runner',now())")
    db_conn.execute(
        "INSERT INTO machine_units(machine_name,home,stopped_at) VALUES('runner','/ava',now())"
    )
    db_conn.commit()
    try:
        yield db_conn
    finally:
        db_conn.rollback()
        db_conn.execute(
            "DROP TABLE pg_temp.deployment_state,pg_temp.machine_units,pg_temp.machines"
        )
        db_conn.commit()


def unit() -> PublishedUnit:
    return PublishedUnit(
        machine="runner",
        home="/ava",
        inventory_digest="a" * 64,
        artifact_digest="b" * 64,
        manifest_digest="c" * 64,
    )


def seed_current(conn: psycopg.Connection) -> CommittedPublication:
    row = conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    now = row[0]
    current = CommittedPublication(
        publication_id=uuid4(),
        operation=RolloutIdentity(
            holder="completed-holder", acquired_at=now - timedelta(hours=1), target_sha="d" * 40
        ),
        committed_at=now - timedelta(minutes=30),
        units=(unit(),),
    )
    conn.execute(
        "UPDATE deployment_state SET managed_writer_evidence=%s",
        (Jsonb(WriterPublication(current=current).model_dump(mode="json")),),
    )
    return current


def pending(conn: psycopg.Connection, current: CommittedPublication) -> PendingPublication:
    row = conn.execute(
        "UPDATE deployment_state SET phase='updating',kind='rollout',holder='next',"
        "acquired_at=clock_timestamp(),expires_at=clock_timestamp()+interval '1 minute',"
        "target_sha=%s RETURNING acquired_at",
        ("e" * 40,),
    ).fetchone()
    assert row is not None
    return PendingPublication(
        operation=RolloutIdentity(holder="next", acquired_at=row[0], target_sha="e" * 40),
        predecessor=current.publication_id,
        candidate_digest="f" * 64,
        challenge=uuid4(),
        units=(unit(),),
    )


def require(conn: psycopg.Connection) -> None:
    require_current_publication(
        conn, unit(), selector_artifact_digest="b" * 64, selector_manifest_digest="c" * 64
    )


def closure(conn: psycopg.Connection, proposal: PendingPublication) -> ManagedWriterCollection:
    row = conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    return ManagedWriterCollection(
        operation=proposal.operation,
        candidate_digest=proposal.candidate_digest,
        challenge=proposal.challenge,
        collected_at=row[0],
        valid_until=row[0] + timedelta(seconds=30),
        units=tuple(
            ManagedUnitClosure(
                unit=ManagedUnit(
                    machine=entry.machine, home=entry.home, inventory_digest=entry.inventory_digest
                ),
                boot_id=uuid4(),
                observer_instance=uuid4(),
                observation_digest="9" * 64,
                outcome="old_writers_absent_relaunchers_fenced",
            )
            for entry in proposal.units
        ),
    )


def test_adopted_stop_evidence_is_not_current_permission(
    publication_db: psycopg.Connection,
) -> None:
    current = seed_current(publication_db)
    proposal = pending(publication_db, current)
    begin_pending_publication(publication_db, proposal)
    collected = closure(publication_db, proposal)
    adopt_pending_collection(publication_db, collected)
    row = publication_db.execute("SELECT managed_writer_evidence FROM deployment_state").fetchone()
    assert row is not None
    assert row[0]["current"] == current.model_dump(mode="json")
    assert row[0]["pending"]["collection"] == collected.model_dump(mode="json")
    with pytest.raises(ManagedWriterBarrierError, match="transitioning"):
        require(publication_db)


@pytest.mark.parametrize("change", ["challenge", "expired", "inventory"])
def test_adoption_rejects_replay_and_drift(publication_db: psycopg.Connection, change: str) -> None:
    current = seed_current(publication_db)
    proposal = pending(publication_db, current)
    begin_pending_publication(publication_db, proposal)
    collected = closure(publication_db, proposal)
    if change == "challenge":
        collected = collected.model_copy(update={"challenge": uuid4()})
    elif change == "expired":
        collected = collected.model_copy(update={"valid_until": collected.collected_at})
    else:
        publication_db.execute(
            "INSERT INTO machine_units(machine_name,home) VALUES('runner','/new')"
        )
    with pytest.raises(ManagedWriterBarrierError):
        adopt_pending_collection(publication_db, collected)
    row = publication_db.execute("SELECT managed_writer_evidence FROM deployment_state").fetchone()
    assert row is not None and row[0]["pending"]["collection"] is None


def test_recovery_requires_new_closure_and_preserves_current(
    publication_db: psycopg.Connection,
) -> None:
    current = seed_current(publication_db)
    abandoned = pending(publication_db, current)
    begin_pending_publication(publication_db, abandoned)
    old_collection = closure(publication_db, abandoned)
    adopt_pending_collection(publication_db, old_collection)
    successor = pending(publication_db, current)
    with pytest.raises(ManagedWriterBarrierError):
        recover_pending_publication(publication_db, abandoned.operation, successor, old_collection)
    collected = closure(publication_db, successor)
    recover_pending_publication(publication_db, abandoned.operation, successor, collected)
    row = publication_db.execute("SELECT managed_writer_evidence FROM deployment_state").fetchone()
    assert row is not None
    assert row[0]["current"] == current.model_dump(mode="json")
    assert row[0]["pending"]["operation"] == successor.operation.model_dump(mode="json")
    with pytest.raises(ManagedWriterBarrierError, match="no longer matches"):
        recover_pending_publication(publication_db, abandoned.operation, successor, collected)


def test_settled_publication_outlives_lease(publication_db: psycopg.Connection) -> None:
    current = seed_current(publication_db)
    assert (
        require_current_publication(
            publication_db,
            unit(),
            selector_artifact_digest="b" * 64,
            selector_manifest_digest="c" * 64,
        )
        == current.publication_id
    )


def test_pending_preserves_current_and_crash_does_not_unfreeze(
    publication_db: psycopg.Connection,
) -> None:
    current = seed_current(publication_db)
    proposal = pending(publication_db, current)
    begin_pending_publication(publication_db, proposal)
    publication_db.commit()
    row = publication_db.execute("SELECT managed_writer_evidence FROM deployment_state").fetchone()
    assert row is not None
    assert row[0]["current"] == current.model_dump(mode="json")
    publication_db.execute(
        "UPDATE deployment_state SET phase='stable',holder=NULL,acquired_at=NULL,expires_at=NULL"
    )
    with pytest.raises(ManagedWriterBarrierError, match="transitioning"):
        require(publication_db)


@pytest.mark.parametrize("change", ["expired", "holder", "partial", "predecessor"])
def test_invalid_begin_leaves_previous_publication(
    publication_db: psycopg.Connection,
    change: str,
) -> None:
    current = seed_current(publication_db)
    proposal = pending(publication_db, current)
    if change == "expired":
        publication_db.execute("UPDATE deployment_state SET expires_at=clock_timestamp()")
    elif change == "holder":
        publication_db.execute("UPDATE deployment_state SET holder='other'")
    elif change == "partial":
        publication_db.execute(
            "INSERT INTO machine_units(machine_name,home) VALUES('runner','/second')"
        )
    else:
        proposal = proposal.model_copy(update={"predecessor": uuid4()})
    with pytest.raises((ManagedWriterBarrierError, ValidationError)):
        begin_pending_publication(publication_db, proposal)
    row = publication_db.execute("SELECT managed_writer_evidence FROM deployment_state").fetchone()
    assert row is not None
    assert row[0] == WriterPublication(current=current).model_dump(mode="json")


@pytest.mark.parametrize("change", ["new_unit", "selector", "bare_operation", "legacy"])
def test_unproven_admission_refuses(publication_db: psycopg.Connection, change: str) -> None:
    current = seed_current(publication_db)
    if change == "new_unit":
        publication_db.execute(
            "INSERT INTO machine_units(machine_name,home) VALUES('runner','/new')"
        )
    elif change == "bare_operation":
        pending(publication_db, current)
    elif change == "legacy":
        publication_db.execute("UPDATE deployment_state SET managed_writer_evidence='{}'::jsonb")
    with pytest.raises((ManagedWriterBarrierError, ValidationError)):
        require_current_publication(
            publication_db,
            unit(),
            selector_artifact_digest="0" * 64 if change == "selector" else "b" * 64,
            selector_manifest_digest="c" * 64,
        )


def test_pending_waits_for_admission_transaction(db_conn: psycopg.Connection) -> None:
    """The rollout cannot freeze/replace publication during an admitted birth."""
    schema = "publication_" + uuid4().hex
    db_conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    for table in ("deployment_state", "machines", "machine_units"):
        db_conn.execute(
            sql.SQL("CREATE TABLE {}.{} (LIKE public.{} INCLUDING ALL)").format(
                sql.Identifier(schema), sql.Identifier(table), sql.Identifier(table)
            )
        )
    db_conn.execute("SELECT set_config('search_path',%s,false)", (schema + ",public",))
    db_conn.execute("INSERT INTO deployment_state(id) VALUES(1)")
    db_conn.execute("INSERT INTO machines(name) VALUES('runner')")
    db_conn.execute("INSERT INTO machine_units(machine_name,home) VALUES('runner','/ava')")
    current = seed_current(db_conn)
    db_conn.commit()
    try:
        with psycopg.connect(db_conn.info.dsn) as worker:
            worker.execute("SELECT set_config('search_path',%s,false)", (schema + ",public",))
            worker.commit()
            db_conn.execute("BEGIN")
            require(db_conn)
            with ThreadPoolExecutor(max_workers=1) as executor:

                def begin() -> None:
                    with worker.transaction():
                        proposal = pending(worker, current)
                        begin_pending_publication(worker, proposal)

                future = executor.submit(begin)
                try:
                    deadline = time.monotonic() + 5
                    while db_conn.execute(
                        "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=%s AND NOT granted)",
                        (worker.info.backend_pid,),
                    ).fetchone() != (True,):
                        if time.monotonic() >= deadline:
                            raise AssertionError("rollout did not wait for admission's lock")
                        time.sleep(0.01)
                    assert not future.done()
                    require(db_conn)
                    db_conn.commit()
                    future.result(timeout=5)
                finally:
                    db_conn.rollback()
            db_conn.execute("BEGIN")
            with pytest.raises(ManagedWriterBarrierError, match="transitioning"):
                require(db_conn)
    finally:
        db_conn.rollback()
        db_conn.execute("SELECT set_config('search_path','public',false)")
        db_conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        db_conn.commit()
