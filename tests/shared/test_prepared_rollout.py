"""Real PG dispatch binding; no fixture acknowledgement proves OS closure."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from shared.managed_writer_barrier import (
    ManagedUnit,
    ManagedUnitClosure,
    ManagedWriterBarrierError,
    ManagedWriterCollection,
    RolloutIdentity,
)
from shared.managed_writer_publication import (
    CandidateUnitPlan,
    NormalService,
    NormalStartPlan,
    PreparedDispatch,
    PreparedUnitPreflight,
    PublishedUnit,
)
from shared.prepared_rollout import (
    bind_prepared_participant,
    create_prepared_operation,
    read_prepared_blockage,
    record_prepared_preflight,
    recover_prepared_operation,
    require_all_prepared_preflights,
    require_prepared_dispatch,
)


@pytest.fixture
def dispatch_db(db_conn: psycopg.Connection) -> Iterator[psycopg.Connection]:
    for table in ("deployment_state", "machines", "machine_units"):
        db_conn.execute(
            sql.SQL("CREATE TEMP TABLE {} (LIKE public.{} INCLUDING ALL)").format(
                sql.Identifier(table), sql.Identifier(table)
            )
        )
    db_conn.execute("INSERT INTO deployment_state(id) VALUES(1)")
    db_conn.execute("INSERT INTO machines(name) VALUES('gateway')")
    db_conn.execute(
        "INSERT INTO machine_units(machine_name,home,serve_gateway) VALUES('gateway','/ava',true)"
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


def proposal(conn: psycopg.Connection) -> tuple[PreparedDispatch, NormalStartPlan]:
    unit = PublishedUnit(
        machine="gateway",
        home="/ava",
        inventory_digest="a" * 64,
        artifact_digest="b" * 64,
        manifest_digest="c" * 64,
    )
    selector = {
        "version": 2,
        "artifact_digest": unit.artifact_digest,
        "manifest_digest": unit.manifest_digest,
        "inventory_receipt_digest": unit.inventory_digest,
    }
    encoded = (json.dumps(selector, sort_keys=True, separators=(",", ":")) + "\n").encode()
    root = "/ava/releases/" + unit.artifact_digest
    plan = NormalStartPlan(
        schema_digest="d" * 64,
        applied_names=("baseline",),
        units=(
            CandidateUnitPlan(
                unit=unit,
                previous_selector_digest=None,
                selector_digest=hashlib.sha256(encoded).hexdigest(),
                services=(
                    NormalService(
                        session="ava-ops",
                        module="services.agent_ops.daemon",
                        executable=root + "/venv/bin/python",
                        entrypoint=root + "/venv/ops.py",
                        command_digest="e" * 64,
                    ),
                ),
            ),
        ),
    )
    now = conn.execute("SELECT clock_timestamp()").fetchone()
    assert now is not None
    return PreparedDispatch(
        request_id=uuid4(),
        request_digest="f" * 64,
        coordinator=unit,
        valid_until=now[0] + timedelta(seconds=60),
    ), plan


def recovery_collection(
    conn: psycopg.Connection,
    dispatch: PreparedDispatch,
    plan: NormalStartPlan,
    *,
    target_sha: str,
) -> ManagedWriterCollection:
    row = conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    operation = RolloutIdentity(
        holder="prepared:gateway:recovery:1", acquired_at=row[0], target_sha=target_sha
    )
    return ManagedWriterCollection(
        operation=operation,
        candidate_digest=dispatch.request_digest,
        challenge=dispatch.request_id,
        collected_at=row[0],
        valid_until=dispatch.valid_until,
        units=tuple(
            ManagedUnitClosure(
                unit=ManagedUnit(
                    machine=entry.unit.machine,
                    home=entry.unit.home,
                    inventory_digest=entry.unit.inventory_digest,
                ),
                boot_id=uuid4(),
                observer_instance=uuid4(),
                observation_digest="9" * 64,
                outcome="old_writers_absent_relaunchers_fenced",
            )
            for entry in plan.units
        ),
    )


def test_one_operation_and_exact_participant_binding(dispatch_db: psycopg.Connection) -> None:
    conn = dispatch_db
    with conn.transaction():
        dispatch, plan = proposal(conn)
        operation = create_prepared_operation(
            conn, dispatch=dispatch, plan=plan, target_sha="1" * 40, holder="gateway:pid123"
        )
    with conn.transaction():
        bound = bind_prepared_participant(
            conn,
            request_id=dispatch.request_id,
            request_digest=dispatch.request_digest,
            valid_until=dispatch.valid_until,
            unit=dispatch.coordinator,
        )
        assert bound == operation
    with pytest.raises(ManagedWriterBarrierError), conn.transaction():
        create_prepared_operation(
            conn, dispatch=dispatch, plan=plan, target_sha="1" * 40, holder="gateway:pid456"
        )
    with pytest.raises(ManagedWriterBarrierError), conn.transaction():
        bind_prepared_participant(
            conn,
            request_id=uuid4(),
            request_digest=dispatch.request_digest,
            valid_until=dispatch.valid_until,
            unit=dispatch.coordinator,
        )
    with pytest.raises(ManagedWriterBarrierError), conn.transaction():
        require_all_prepared_preflights(
            conn, operation, dispatch.request_id, dispatch.request_digest, dispatch.coordinator
        )
    with conn.transaction():
        now = conn.execute("SELECT clock_timestamp()").fetchone()
        assert now is not None
        evidence = PreparedUnitPreflight(
            unit=dispatch.coordinator,
            request_digest=dispatch.request_digest,
            evidence_digest="2" * 64,
            observed_at=now[0],
        )
        record_prepared_preflight(conn, operation, dispatch.request_id, evidence)
        record_prepared_preflight(conn, operation, dispatch.request_id, evidence)
        require_all_prepared_preflights(
            conn, operation, dispatch.request_id, dispatch.request_digest, dispatch.coordinator
        )
    with pytest.raises(ManagedWriterBarrierError), conn.transaction():
        record_prepared_preflight(
            conn,
            operation,
            dispatch.request_id,
            evidence.model_copy(update={"evidence_digest": "3" * 64}),
        )
    with conn.transaction():
        conn.execute("UPDATE deployment_state SET holder='gateway:pid456' WHERE id=1")
    with pytest.raises(ManagedWriterBarrierError), conn.transaction():
        require_prepared_dispatch(
            conn, operation, dispatch.request_id, dispatch.request_digest, dispatch.coordinator
        )


@pytest.mark.parametrize("mutation", ["gateway", "unit", "expired", "orphan"])
def test_pre_effect_rejection(dispatch_db: psycopg.Connection, mutation: str) -> None:
    conn = dispatch_db
    with conn.transaction():
        dispatch, plan = proposal(conn)
        if mutation == "gateway":
            conn.execute("UPDATE machine_units SET serve_gateway=false")
        elif mutation == "unit":
            conn.execute("INSERT INTO machine_units(machine_name,home) VALUES('gateway','/other')")
        elif mutation == "expired":
            dispatch = dispatch.model_copy(
                update={"valid_until": dispatch.valid_until - timedelta(days=1)}
            )
        else:
            conn.execute(
                "UPDATE deployment_state SET holder='dead:pid1',expires_at=now()-interval '1 day'"
            )
    with pytest.raises(ManagedWriterBarrierError), conn.transaction():
        create_prepared_operation(
            conn, dispatch=dispatch, plan=plan, target_sha="1" * 40, holder="gateway:pid123"
        )
    row = conn.execute("SELECT managed_writer_evidence FROM deployment_state WHERE id=1").fetchone()
    assert row is not None and row[0] is None


def test_blockage_read_reports_exact_abandoned_operation(dispatch_db: psycopg.Connection) -> None:
    dispatch, plan = proposal(dispatch_db)
    with dispatch_db.transaction():
        operation = create_prepared_operation(
            dispatch_db,
            dispatch=dispatch,
            plan=plan,
            target_sha="1" * 40,
            holder="gateway:pid123",
        )
    with dispatch_db.transaction():
        blockage = read_prepared_blockage(dispatch_db)
    assert blockage.operation == operation
    assert blockage.predecessor is None
    assert blockage.phase == "updating"
    assert blockage.holder == operation.holder
    assert blockage.acquired_at == operation.acquired_at
    assert blockage.target_sha == operation.target_sha


def test_recovery_cas_replaces_exact_predecessor(dispatch_db: psycopg.Connection) -> None:
    dispatch, plan = proposal(dispatch_db)
    with dispatch_db.transaction():
        abandoned = create_prepared_operation(
            dispatch_db,
            dispatch=dispatch,
            plan=plan,
            target_sha="1" * 40,
            holder="gateway:pid123",
        )
    replacement_dispatch = dispatch.model_copy(
        update={"request_id": uuid4(), "request_digest": "7" * 64}
    )
    collection = recovery_collection(dispatch_db, replacement_dispatch, plan, target_sha="2" * 40)
    wrong_collection = collection.model_copy(
        update={"operation": collection.operation.model_copy(update={"target_sha": "3" * 40})}
    )
    with pytest.raises(ManagedWriterBarrierError, match="target"), dispatch_db.transaction():
        recover_prepared_operation(
            dispatch_db,
            abandoned=abandoned,
            dispatch=replacement_dispatch,
            plan=plan,
            target_sha="2" * 40,
            fresh_collection=wrong_collection,
        )
    with dispatch_db.transaction():
        recovered = recover_prepared_operation(
            dispatch_db,
            abandoned=abandoned,
            dispatch=replacement_dispatch,
            plan=plan,
            target_sha="2" * 40,
            fresh_collection=collection,
        )
        row = dispatch_db.execute(
            "SELECT holder, acquired_at, target_sha, managed_writer_evidence "
            "FROM deployment_state WHERE id=1"
        ).fetchone()
    assert recovered == collection.operation
    assert row is not None
    assert row[:3] == (recovered.holder, recovered.acquired_at, recovered.target_sha)
    assert row[3]["pending"]["operation"] == recovered.model_dump(mode="json")
    assert row[3]["pending"]["predecessor"] is None

    with (
        pytest.raises(ManagedWriterBarrierError, match="no longer matches"),
        dispatch_db.transaction(),
    ):
        recover_prepared_operation(
            dispatch_db,
            abandoned=abandoned,
            dispatch=replacement_dispatch,
            plan=plan,
            target_sha="2" * 40,
            fresh_collection=collection,
        )


def test_recovery_cas_refuses_tampered_deployment_holder(
    dispatch_db: psycopg.Connection,
) -> None:
    dispatch, plan = proposal(dispatch_db)
    with dispatch_db.transaction():
        abandoned = create_prepared_operation(
            dispatch_db,
            dispatch=dispatch,
            plan=plan,
            target_sha="1" * 40,
            holder="gateway:pid123",
        )
        dispatch_db.execute("UPDATE deployment_state SET holder='tampered:pid456' WHERE id=1")
    replacement_dispatch = dispatch.model_copy(
        update={"request_id": uuid4(), "request_digest": "7" * 64}
    )
    collection = recovery_collection(dispatch_db, replacement_dispatch, plan, target_sha="2" * 40)
    with (
        pytest.raises(ManagedWriterBarrierError, match="predecessor CAS failed"),
        dispatch_db.transaction(),
    ):
        recover_prepared_operation(
            dispatch_db,
            abandoned=abandoned,
            dispatch=replacement_dispatch,
            plan=plan,
            target_sha="2" * 40,
            fresh_collection=collection,
        )
    row = dispatch_db.execute("SELECT holder FROM deployment_state WHERE id=1").fetchone()
    assert row == ("tampered:pid456",)
