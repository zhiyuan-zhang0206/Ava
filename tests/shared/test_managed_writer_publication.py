"""Real PostgreSQL publication transitions; fixture permits are not activation."""

from __future__ import annotations

import hashlib
import json
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
    for table in ("deployment_state", "machines", "machine_units", "schema_migrations"):
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
            "DROP TABLE pg_temp.deployment_state,pg_temp.machine_units,pg_temp.machines,"
            "pg_temp.schema_migrations"
        )
        db_conn.commit()


def activation_plan(conn: psycopg.Connection) -> PendingPublication:
    from shared.managed_writer_publication import CandidateUnitPlan, NormalService, NormalStartPlan

    current = seed_current(conn)
    proposal = pending(conn, current)
    image = "/ava/releases/" + "b" * 64
    selector = {
        "version": 2,
        "artifact_digest": "b" * 64,
        "manifest_digest": "c" * 64,
        "prepared_receipt_digest": "d" * 64,
    }
    digest = hashlib.sha256(
        (json.dumps(selector, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    plan = NormalStartPlan(
        schema_digest="1" * 64,
        applied_names=("baseline",),
        units=(
            CandidateUnitPlan(
                unit=unit(),
                previous_selector_digest=None,
                selector_digest=digest,
                services=(
                    NormalService(
                        session="ava-ops",
                        module="services.agent_ops.daemon",
                        executable=image + "/venv/bin/python",
                        entrypoint=image + "/venv/services/agent_ops/daemon.py",
                        command_digest="2" * 64,
                    ),
                ),
            ),
        ),
    )
    proposal = proposal.model_copy(update={"normal_start_plan": plan})
    begin_pending_publication(conn, proposal)
    return proposal


def activation_readback(conn: psycopg.Connection, proposal: PendingPublication):
    from shared.managed_writer_activation import (
        NormalServiceReadback,
        SelectorReadback,
        UnitActivationReadback,
    )
    from shared.managed_writer_observation import ExpectedProcess

    assert proposal.normal_start_plan is not None
    expected = proposal.normal_start_plan.units[0]
    row = conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    now = row[0]
    # Fixed observation fixtures test DB acceptance, not a live service adapter.
    return UnitActivationReadback(
        selector=SelectorReadback(
            unit=unit(),
            challenge=proposal.challenge,
            previous_digest=None,
            current_digest=expected.selector_digest,
            observed_at=now,
            valid_until=now + timedelta(seconds=30),
        ),
        services=(
            NormalServiceReadback(
                service=expected.services[0],
                supervisor=ExpectedProcess(pid=11, create_time=1.0),
                child=ExpectedProcess(pid=12, create_time=2.0),
                loaded_module=expected.services[0].entrypoint,
                executable=expected.services[0].executable,
                entrypoint=expected.services[0].entrypoint,
                artifact_digest="b" * 64,
                manifest_digest="c" * 64,
                readiness="normal",
                observation_digest="3" * 64,
                challenge=proposal.challenge,
                observed_at=now,
                valid_until=now + timedelta(seconds=30),
            ),
        ),
    )


@pytest.mark.parametrize(
    "session,allowed",
    [
        ("ava-agent-host", True),
        ("ava-agent-1", False),
        ("ava-agent-1-attempt-deadbeef", False),
        ("ava-agent-other", False),
    ],
)
def test_normal_plan_allows_only_known_agent_host_service(
    publication_db: psycopg.Connection, session: str, allowed: bool
) -> None:
    from shared.managed_writer_publication import CandidateUnitPlan

    proposal = activation_plan(publication_db)
    assert proposal.normal_start_plan is not None
    value = proposal.normal_start_plan.units[0].model_dump(mode="json")
    value["services"][0]["session"] = session
    if allowed:
        assert (
            CandidateUnitPlan.model_validate_json(json.dumps(value)).services[0].session == session
        )
    else:
        with pytest.raises(ValidationError, match="agent session"):
            CandidateUnitPlan.model_validate_json(json.dumps(value))


def test_pending_readback_transport_is_immutable_and_consumed(
    publication_db: psycopg.Connection,
) -> None:
    from shared.managed_writer_activation import (
        commit_current,
        read_pending_unit_readbacks,
        record_pending_migration,
        record_pending_unit_readback,
    )

    conn = publication_db
    proposal = activation_plan(conn)
    args = (conn, proposal.operation, proposal.challenge)
    adopt_pending_collection(conn, closure(conn, proposal))
    conn.execute("INSERT INTO schema_migrations(name) VALUES('baseline')")
    record_pending_migration(*args)
    assert read_pending_unit_readbacks(*args) == ()
    readback = activation_readback(conn, proposal)
    record_pending_unit_readback(*args, readback)
    before = conn.execute("SELECT managed_writer_evidence FROM deployment_state").fetchone()
    record_pending_unit_readback(*args, readback)
    begin_pending_publication(conn, proposal)
    assert conn.execute("SELECT managed_writer_evidence FROM deployment_state").fetchone() == before
    changed = readback.model_copy(
        update={
            "services": (readback.services[0].model_copy(update={"observation_digest": "4" * 64}),)
        }
    )
    with pytest.raises(ManagedWriterBarrierError, match="silently replaced"):
        record_pending_unit_readback(*args, changed)
    with pytest.raises(ManagedWriterBarrierError, match="recorded"):
        commit_current(*args, (changed,))
    assert conn.execute("SELECT managed_writer_evidence FROM deployment_state").fetchone() == before
    collected = read_pending_unit_readbacks(*args)
    assert collected == (readback,)
    assert commit_current(*args, collected)


@pytest.mark.parametrize("change", ["challenge", "expiry", "service", "unit"])
def test_invalid_unit_readback_does_not_enter_pending(
    publication_db: psycopg.Connection, change: str
) -> None:
    from shared.managed_writer_activation import (
        record_pending_migration,
        record_pending_unit_readback,
    )

    conn = publication_db
    proposal = activation_plan(conn)
    args = (conn, proposal.operation, proposal.challenge)
    adopt_pending_collection(conn, closure(conn, proposal))
    conn.execute("INSERT INTO schema_migrations(name) VALUES('baseline')")
    record_pending_migration(*args)
    readback = activation_readback(conn, proposal)
    if change == "service":
        readback = readback.model_copy(update={"services": ()})
    else:
        updates = (
            {"challenge": uuid4()}
            if change == "challenge"
            else (
                {"valid_until": readback.selector.observed_at}
                if change == "expiry"
                else {"unit": unit().model_copy(update={"home": "/other"})}
            )
        )
        readback = readback.model_copy(
            update={"selector": readback.selector.model_copy(update=updates)}
        )
    before = conn.execute("SELECT managed_writer_evidence FROM deployment_state").fetchone()
    with pytest.raises(ManagedWriterBarrierError):
        record_pending_unit_readback(*args, readback)
    assert conn.execute("SELECT managed_writer_evidence FROM deployment_state").fetchone() == before


def test_actual_migration_and_service_publication_chain(publication_db: psycopg.Connection) -> None:
    from shared.managed_writer_activation import (
        commit_current,
        pending_stage,
        record_pending_migration,
        require_pending_candidate_start,
        require_pending_migration,
        require_pending_selector_change,
    )

    conn = publication_db
    proposal = activation_plan(conn)
    args = (conn, proposal.operation, proposal.challenge)
    assert pending_stage(*args) == "waiting_collection"
    with pytest.raises(ManagedWriterBarrierError):
        require_pending_migration(*args)
    adopt_pending_collection(conn, closure(conn, proposal))
    assert pending_stage(*args) == "waiting_migration"
    assert require_pending_migration(*args) == proposal.normal_start_plan
    with pytest.raises(ManagedWriterBarrierError, match="actual migrations"):
        record_pending_migration(*args)
    conn.execute("INSERT INTO schema_migrations(name) VALUES('baseline')")
    receipt = record_pending_migration(*args)
    assert record_pending_migration(*args) == receipt
    assert pending_stage(*args) == "selector_allowed"
    plan = require_pending_selector_change(*args, unit())
    readback = activation_readback(conn, proposal)
    assert (
        require_pending_candidate_start(*args, readback.selector, plan.services[0])
        == plan.services[0]
    )
    publication_id = commit_current(*args, (readback,))
    assert pending_stage(*args) == "committed"
    assert commit_current(*args, (readback,)) == publication_id
    with pytest.raises(ManagedWriterBarrierError, match="not settled"):
        require(conn)
    conn.execute(
        "UPDATE deployment_state SET phase='stable',holder=NULL,acquired_at=NULL,expires_at=NULL"
    )
    assert commit_current(*args, (readback,)) == publication_id
    require(conn)


@pytest.mark.parametrize(
    "change",
    ["lease", "challenge", "schema", "selector", "service", "image", "module", "unit", "expiry"],
)
def test_bad_activation_never_clears_pending(
    publication_db: psycopg.Connection, change: str
) -> None:
    from shared.managed_writer_activation import commit_current, record_pending_migration

    conn = publication_db
    proposal = activation_plan(conn)
    adopt_pending_collection(conn, closure(conn, proposal))
    conn.execute("INSERT INTO schema_migrations(name) VALUES('baseline')")
    record_pending_migration(conn, proposal.operation, proposal.challenge)
    readback = activation_readback(conn, proposal)
    challenge = proposal.challenge
    if change == "lease":
        conn.execute("UPDATE deployment_state SET expires_at=clock_timestamp()")
    elif change == "challenge":
        challenge = uuid4()
    elif change == "schema":
        conn.execute("INSERT INTO schema_migrations(name) VALUES('unplanned')")
    elif change == "selector":
        readback = readback.model_copy(
            update={"selector": readback.selector.model_copy(update={"previous_digest": "4" * 64})}
        )
    elif change == "service":
        readback = readback.model_copy(update={"services": ()})
    elif change in {"image", "module"}:
        field, value = (
            ("artifact_digest", "4" * 64)
            if change == "image"
            else ("loaded_module", "/ava/source/daemon.py")
        )
        readback = readback.model_copy(
            update={"services": (readback.services[0].model_copy(update={field: value}),)}
        )
    elif change == "unit":
        conn.execute("INSERT INTO machine_units(machine_name,home) VALUES('runner','/other')")
    else:
        readback = readback.model_copy(
            update={
                "selector": readback.selector.model_copy(
                    update={"valid_until": readback.selector.observed_at}
                )
            }
        )
    before = conn.execute("SELECT managed_writer_evidence FROM deployment_state").fetchone()
    with pytest.raises(ManagedWriterBarrierError):
        commit_current(conn, proposal.operation, challenge, (readback,))
    assert conn.execute("SELECT managed_writer_evidence FROM deployment_state").fetchone() == before


def test_activation_rollback_preserves_pending(publication_db: psycopg.Connection) -> None:
    from shared.managed_writer_activation import commit_current, record_pending_migration

    conn = publication_db
    proposal = activation_plan(conn)
    adopt_pending_collection(conn, closure(conn, proposal))
    conn.execute("INSERT INTO schema_migrations(name) VALUES('baseline')")
    record_pending_migration(conn, proposal.operation, proposal.challenge)
    readback = activation_readback(conn, proposal)
    before = conn.execute("SELECT managed_writer_evidence FROM deployment_state").fetchone()
    with pytest.raises(RuntimeError, match="injected"), conn.transaction():
        commit_current(conn, proposal.operation, proposal.challenge, (readback,))
        raise RuntimeError("injected crash before commit")
    assert conn.execute("SELECT managed_writer_evidence FROM deployment_state").fetchone() == before


def test_start_waiter_rechecks_lease_after_unchanged_row_lock(db_conn: psycopg.Connection) -> None:
    from shared.managed_writer_activation import (
        record_pending_migration,
        require_pending_selector_change,
    )

    schema = "activation_" + uuid4().hex
    db_conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    for table in ("deployment_state", "machines", "machine_units", "schema_migrations"):
        db_conn.execute(
            sql.SQL("CREATE TABLE {}.{} (LIKE public.{} INCLUDING ALL)").format(
                sql.Identifier(schema), sql.Identifier(table), sql.Identifier(table)
            )
        )
    db_conn.execute("SELECT set_config('search_path',%s,false)", (schema + ",public",))
    db_conn.execute("INSERT INTO deployment_state(id) VALUES(1)")
    db_conn.execute("INSERT INTO machines(name) VALUES('runner')")
    db_conn.execute("INSERT INTO machine_units(machine_name,home) VALUES('runner','/ava')")
    proposal = activation_plan(db_conn)
    adopt_pending_collection(db_conn, closure(db_conn, proposal))
    db_conn.execute("INSERT INTO schema_migrations(name) VALUES('baseline')")
    record_pending_migration(db_conn, proposal.operation, proposal.challenge)
    db_conn.execute("UPDATE deployment_state SET expires_at=clock_timestamp()+interval '1 second'")
    db_conn.commit()
    try:
        with psycopg.connect(db_conn.info.dsn) as worker:
            worker.execute("SELECT set_config('search_path',%s,false)", (schema + ",public",))
            worker.commit()
            db_conn.execute("SELECT id FROM deployment_state WHERE id=1 FOR UPDATE")
            with ThreadPoolExecutor(max_workers=1) as executor:

                def attempt() -> None:
                    with worker.transaction():
                        require_pending_selector_change(
                            worker, proposal.operation, proposal.challenge, unit()
                        )

                future = executor.submit(attempt)
                try:
                    deadline = time.monotonic() + 5
                    while db_conn.execute(
                        "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=%s AND NOT granted)",
                        (worker.info.backend_pid,),
                    ).fetchone() != (True,):
                        if time.monotonic() >= deadline:
                            raise AssertionError("start waiter did not acquire the blocked lock")
                        time.sleep(0.01)
                    while db_conn.execute(
                        "SELECT clock_timestamp() >= expires_at FROM deployment_state"
                    ).fetchone() != (True,):
                        if time.monotonic() >= deadline:
                            raise AssertionError("test lease did not expire")
                        time.sleep(0.01)
                    db_conn.commit()
                    with pytest.raises(ManagedWriterBarrierError, match="live rollout"):
                        future.result(timeout=5)
                finally:
                    db_conn.rollback()
    finally:
        db_conn.rollback()
        db_conn.execute("SELECT set_config('search_path','public',false)")
        db_conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        db_conn.commit()


def unit(*, prepared_receipt_digest: str = "d" * 64) -> PublishedUnit:
    return PublishedUnit(
        machine="runner",
        home="/ava",
        inventory_digest="a" * 64,
        prepared_receipt_digest=prepared_receipt_digest,
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


def closure(
    conn: psycopg.Connection,
    proposal: PendingPublication,
    *,
    use_observer_digest: bool = False,
) -> ManagedWriterCollection:
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
                    machine=entry.machine,
                    home=entry.home,
                    inventory_digest=(
                        entry.inventory_digest
                        if use_observer_digest
                        else entry.prepared_receipt_digest
                    ),
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
    begin_pending_publication(publication_db, proposal)
    row = publication_db.execute("SELECT managed_writer_evidence FROM deployment_state").fetchone()
    assert row is not None
    assert row[0]["current"] == current.model_dump(mode="json")
    assert row[0]["pending"]["collection"] == collected.model_dump(mode="json")
    with pytest.raises(ManagedWriterBarrierError, match="transitioning"):
        require(publication_db)


def test_pending_retry_cannot_alias_a_changed_full_prepare_receipt(
    publication_db: psycopg.Connection,
) -> None:
    current = seed_current(publication_db)
    proposal = pending(publication_db, current)
    begin_pending_publication(publication_db, proposal)
    changed_receipt = proposal.model_copy(
        update={"units": (unit(prepared_receipt_digest="0" * 64),)}
    )
    with pytest.raises(ManagedWriterBarrierError, match="another pending publication"):
        begin_pending_publication(publication_db, changed_receipt)


def test_adoption_rejects_observer_digest_in_place_of_full_prepare_receipt(
    publication_db: psycopg.Connection,
) -> None:
    current = seed_current(publication_db)
    proposal = pending(publication_db, current)
    begin_pending_publication(publication_db, proposal)
    with pytest.raises(ManagedWriterBarrierError, match="inventory differs"):
        adopt_pending_collection(
            publication_db,
            closure(publication_db, proposal, use_observer_digest=True),
        )


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


@pytest.mark.parametrize("change", ["new_unit", "selector", "receipt", "bare_operation", "legacy"])
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
            unit(prepared_receipt_digest="0" * 64) if change == "receipt" else unit(),
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
