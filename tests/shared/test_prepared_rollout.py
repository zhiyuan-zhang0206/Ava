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

from shared.managed_writer_barrier import ManagedWriterBarrierError
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
    record_prepared_preflight,
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
