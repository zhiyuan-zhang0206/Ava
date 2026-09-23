"""CI-only process-level managed-writer activation chain proof (task #4128, N3).

Drives the production publication seats end to end against an isolated schema on
the CI native PostgreSQL, inside the source-absent runtime prepare proof:

    open_pending_publication (P1)
      -> assemble_unit_closure + assemble_collection (the real derivation layer)
      -> adopt_pending_collection
      -> record_pending_migration
      -> record_pending_unit_readback (one, then the complete set)
      -> commit_pending_publication (P5)

Asserted: the journal stage transitions (waiting_collection -> waiting_migration
-> selector_allowed -> committed), two fail-closed gates (the migration seat
refuses before adoption; the commit seat refuses an incomplete readback set),
and commit idempotency (the post-commit seat call is a no-op over unchanged
evidence). The per-transition storage detail stays pinned by
tests/shared/test_managed_writer_publication.py; crash/replay shape stays there.

Fixture boundaries (deliberate; ruled on task #4128 slice E2-d):

- Facts are fixture-constructed: the sealed per-unit receipt and its producers
  (release inventory, the candidate image) are out of scope at this layer.
  The digest classes are bound as production binds them (task #4129 Q1): the
  closure unit carries ``prepared_receipt_digest`` — a distinct fixture digest,
  never the observer-tuple digest — while the observer tuple stays the facts
  attribution; substitution is refused by
  tests/shared/test_managed_writer_publication.py::
  test_adoption_rejects_observer_digest_in_place_of_full_prepare_receipt.
- The isolated schema mirrors tests/shared/test_managed_writer_publication.py
  ::publication_db (deployment_state / machines / machine_units /
  schema_migrations); keep the two in sync.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from cli.commands._update_publication import (
    PreparedUnitPublication,
    commit_pending_publication,
    open_pending_publication,
    published_unit,
)
from shared.managed_writer_activation import (
    pending_stage,
    record_pending_migration,
    record_pending_unit_readback,
    require_pending_candidate_start,
    require_pending_migration,
    require_pending_selector_change,
)
from shared.managed_writer_barrier import (
    ManagedUnit,
    ManagedUnitClosure,
    ManagedWriterBarrierError,
    RolloutIdentity,
)
from shared.managed_writer_closure import assemble_collection, assemble_unit_closure
from shared.managed_writer_observation import ExpectedProcess, ExpectedUnitWriters
from shared.managed_writer_publication import (
    CandidateUnitPlan,
    CommittedPublication,
    NormalService,
    NormalServiceReadback,
    PendingPublication,
    SelectorReadback,
    UnitActivationReadback,
    WriterPublication,
    adopt_pending_collection,
)
from shared.migrations import required_migration_set
from shared.runtime_publication_input import PreparationReceipt, PreparedService

MACHINE = "runtime-proof"
COORDINATOR = "managed-writer-chain-proof"
TARGET_SHA = "e" * 40
CANDIDATE_DIGEST = hashlib.sha256(b"managed-writer-chain-candidate").hexdigest()
SCHEMA_DIGEST = hashlib.sha256(b"managed-writer-chain-schema").hexdigest()
PLAN_DIGEST = hashlib.sha256(b"managed-writer-chain-plan").hexdigest()


def require(condition: bool, message: str) -> None:  # noqa: FBT001 — proof predicate.
    if not condition:
        raise AssertionError(message)


def digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def refused(action: Callable[[], object]) -> ManagedWriterBarrierError:
    try:
        action()
    except ManagedWriterBarrierError as exc:
        return exc
    raise AssertionError("expected a managed-writer refusal")


def clock(conn: psycopg.Connection) -> datetime:
    row = conn.execute("SELECT clock_timestamp()").fetchone()
    if row is None:
        raise AssertionError("database clock read failed")
    return row[0]


def evidence(conn: psycopg.Connection) -> object:
    row = conn.execute("SELECT managed_writer_evidence FROM deployment_state WHERE id=1").fetchone()
    if row is None:
        raise AssertionError("deployment state row is missing")
    return row[0]


def unit_receipt(home: str) -> PreparationReceipt:
    expected = ExpectedUnitWriters(
        machine=MACHINE,
        home=home,
        artifact_digest=digest(f"artifact:{home}"),
        manifest_digest=digest(f"manifest:{home}"),
        processes=(),
        sessions=(),
        launchers=(),
    )
    return PreparationReceipt(
        version=1,
        expected=expected,
        services=(PreparedService(session="ava-ops", requires_db=True, gate=None),),
        excluded_registrations=(),
        inventory_digest=expected.unit().inventory_digest,
        closure="unknown",
        unresolved=("non-session managed processes",),
    )


def unit_candidate(unit_facts: PreparedUnitPublication) -> CandidateUnitPlan:
    unit = published_unit(unit_facts)
    image = f"{unit.home}/releases/{unit.artifact_digest}"
    selector = {
        "version": 2,
        "artifact_digest": unit.artifact_digest,
        "manifest_digest": unit.manifest_digest,
        "prepared_receipt_digest": unit.prepared_receipt_digest,
    }
    return CandidateUnitPlan(
        unit=unit,
        services=(
            NormalService(
                session="ava-ops",
                module="services.agent_ops.daemon",
                executable=f"{image}/venv/bin/python",
                entrypoint=f"{image}/venv/services/agent_ops/daemon.py",
                command_digest=digest("command"),
            ),
        ),
        previous_selector_digest=None,
        selector_digest=hashlib.sha256(
            (json.dumps(selector, sort_keys=True, separators=(",", ":")) + "\n").encode()
        ).hexdigest(),
    )


def unit_facts(home: str) -> PreparedUnitPublication:
    sealed = unit_receipt(home)
    facts = PreparedUnitPublication(
        receipt=sealed,
        # Fixture binding (module docstring): production binds the sealed
        # receipt bytes here; this proof binds a distinct digest so the real
        # derivation and adoption digest classes meet through the binding.
        prepared_receipt_digest=hashlib.sha256(f"receipt:{home}".encode()).hexdigest(),
        artifact_digest=sealed.expected.artifact_digest,
        manifest_digest=sealed.expected.manifest_digest,
        candidate=None,
    )
    return replace(facts, candidate=unit_candidate(facts))


def unit_readback(
    conn: psycopg.Connection, pending: PendingPublication, expected: CandidateUnitPlan
) -> UnitActivationReadback:
    now = clock(conn)
    service = expected.services[0]
    return UnitActivationReadback(
        selector=SelectorReadback(
            unit=expected.unit,
            challenge=pending.challenge,
            previous_digest=expected.previous_selector_digest,
            current_digest=expected.selector_digest,
            observed_at=now,
            valid_until=now + timedelta(seconds=30),
        ),
        services=(
            NormalServiceReadback(
                service=service,
                supervisor=ExpectedProcess(pid=11, create_time=1.0),
                child=ExpectedProcess(pid=12, create_time=2.0),
                loaded_module=service.entrypoint,
                executable=service.executable,
                entrypoint=service.entrypoint,
                artifact_digest=expected.unit.artifact_digest,
                manifest_digest=expected.unit.manifest_digest,
                readiness="normal",
                observation_digest="3" * 64,
                challenge=pending.challenge,
                observed_at=now,
                valid_until=now + timedelta(seconds=30),
            ),
        ),
    )


def provision_schema(conn: psycopg.Connection, namespace: str) -> None:
    """Isolated schema on the CI native PG; DDL mirrors ``publication_db``."""
    conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(namespace)))
    conn.execute("SELECT set_config('search_path',%s,false)", (namespace,))
    conn.execute("CREATE TABLE machines(name text PRIMARY KEY)")
    conn.execute("CREATE TABLE machine_units(machine_name text, home text, stopped_at timestamptz)")
    conn.execute(
        "CREATE TABLE schema_migrations(name text PRIMARY KEY, applied_at timestamptz DEFAULT now())"
    )
    conn.execute(
        # The settle columns are real-schema since the initial release; the
        # activation chain's lease fencing references `settle_hosts` (task #4086 b5).
        "CREATE TABLE deployment_state(id integer PRIMARY KEY, managed_writer_evidence jsonb,"
        " phase text, kind text, holder text, acquired_at timestamptz,"
        " expires_at timestamptz, target_sha text, settle_hosts text[],"
        " settle_note text, settle_started_at timestamptz)"
    )
    conn.commit()


def run_chain(  # noqa: PLR0915 — one guarded chain lifetime with ordered gate evidence.
    conn: psycopg.Connection, home: Path, namespace: str
) -> dict[str, object]:
    home_a = str(home)
    home_b = str(home.parent / (home.name + "-two"))
    facts = (unit_facts(home_a), unit_facts(home_b))
    applied_names = tuple(sorted(required_migration_set()))
    require(bool(applied_names), "installed migration set is empty")

    conn.execute("INSERT INTO machines(name) VALUES (%s)", (MACHINE,))
    conn.execute("INSERT INTO machine_units(machine_name, home) VALUES (%s, %s)", (MACHINE, home_a))
    conn.execute(
        "INSERT INTO machine_units(machine_name, home, stopped_at) VALUES (%s, %s, now())",
        (MACHINE, home_b),
    )
    for name in applied_names:
        conn.execute("INSERT INTO schema_migrations(name) VALUES (%s)", (name,))
    conn.execute("INSERT INTO deployment_state(id) VALUES (1)")
    previous = CommittedPublication(
        publication_id=uuid4(),
        operation=RolloutIdentity(
            holder="completed-holder",
            acquired_at=clock(conn) - timedelta(hours=1),
            target_sha="1" * 40,
        ),
        committed_at=clock(conn) - timedelta(minutes=30),
        units=(published_unit(facts[0]), published_unit(facts[1])),
    )
    conn.execute(
        "UPDATE deployment_state SET managed_writer_evidence=%s WHERE id=1",
        (Jsonb(WriterPublication(current=previous).model_dump(mode="json")),),
    )
    row = conn.execute(
        "UPDATE deployment_state SET phase='updating', kind='rollout', holder=%s,"
        " acquired_at=clock_timestamp(), expires_at=clock_timestamp() + interval '600 seconds',"
        " target_sha=%s WHERE id=1 RETURNING acquired_at",
        (COORDINATOR, TARGET_SHA),
    ).fetchone()
    if row is None:
        raise AssertionError("rollout lease update returned no row")
    operation = RolloutIdentity(holder=COORDINATOR, acquired_at=row[0], target_sha=TARGET_SHA)
    conn.commit()

    # P1: open the journal; a migration seat must refuse before adoption.
    with conn.transaction():
        pending = open_pending_publication(
            conn,
            facts,
            operation=operation,
            candidate_digest=CANDIDATE_DIGEST,
            schema_digest=SCHEMA_DIGEST,
            applied_names=applied_names,
            valid_until=clock(conn) + timedelta(seconds=300),
            plan_digest=PLAN_DIGEST,
        )
        require(
            pending.predecessor == previous.publication_id,
            "journal did not bind the previous current publication",
        )
        require(
            pending.plan_digest == PLAN_DIGEST and pending.valid_until is not None,
            "journal did not register the begin execution's V and plan digest",
        )
        require(
            pending_stage(conn, operation, pending.challenge) == "waiting_collection",
            "stage after open is not waiting_collection",
        )
        migration_refusal = refused(
            lambda: record_pending_migration(conn, operation, pending.challenge)
        )
        require(
            "authority is missing" in str(migration_refusal),
            f"pre-adoption migration refusal changed shape: {migration_refusal}",
        )

    # P2: derive the closure through the real assembly layer, then adopt it.
    with conn.transaction():
        closures: list[ManagedUnitClosure] = []
        for entry in facts:
            expected = entry.receipt.expected
            window = clock(conn)
            closure = assemble_unit_closure(
                expected,
                operation=operation,
                operation_challenge=pending.challenge,
                echoed_challenge=pending.challenge,
                boot_id=uuid4(),
                observer_instance=uuid4(),
                observed_unit=expected.unit(),
                prepared_receipt_digest=entry.prepared_receipt_digest,
                observed_at=window,
                valid_until=window + timedelta(seconds=60),
                processes=(),
                sessions=(),
                launchers=(),
                terminals=(),
            )
            if closure is None:
                raise AssertionError(f"closure derivation refused {expected.home}")
            closures.append(closure)
        collected = clock(conn)
        collection = assemble_collection(
            operation=operation,
            candidate_digest=pending.candidate_digest,
            challenge=pending.challenge,
            collected_at=collected,
            valid_until=collected + timedelta(seconds=60),
            expected_units=tuple(
                ManagedUnit(
                    machine=entry.receipt.expected.machine,
                    home=entry.receipt.expected.home,
                    inventory_digest=entry.prepared_receipt_digest,
                )
                for entry in facts
            ),
            closures=tuple(closures),
        )
        if collection is None:
            raise AssertionError("collection assembly refused")
        adopt_pending_collection(conn, collection)
        require(
            pending_stage(conn, operation, pending.challenge) == "waiting_migration",
            "stage after adoption is not waiting_migration",
        )

    # P3: the migration receipt reads the real applied SET; an exact retry is idempotent.
    with conn.transaction():
        migration = record_pending_migration(conn, operation, pending.challenge)
        require(
            record_pending_migration(conn, operation, pending.challenge) == migration,
            "migration receipt is not idempotent",
        )
        require(
            pending_stage(conn, operation, pending.challenge) == "selector_allowed",
            "stage after the migration receipt is not selector_allowed",
        )

    # P4: gates plus one unit's readback; the commit seat must refuse the rest.
    with conn.transaction():
        plan = require_pending_migration(conn, operation, pending.challenge)
        require(
            plan == pending.normal_start_plan, "migration gate did not return the journaled plan"
        )
        candidates = {entry.unit.home: entry for entry in plan.units}
        readbacks = {
            home_a: unit_readback(conn, pending, candidates[home_a]),
            home_b: unit_readback(conn, pending, candidates[home_b]),
        }
        require(
            require_pending_selector_change(
                conn, operation, pending.challenge, candidates[home_a].unit
            )
            == candidates[home_a],
            "selector gate did not return the journaled candidate plan",
        )
        service = candidates[home_a].services[0]
        require(
            require_pending_candidate_start(
                conn, operation, pending.challenge, readbacks[home_a].selector, service
            )
            == service,
            "candidate-start gate did not authorize the prepared service",
        )
        record_pending_unit_readback(conn, operation, pending.challenge, readbacks[home_a])
    with conn.transaction():
        snapshot = evidence(conn)
        readback_refusal = refused(lambda: commit_pending_publication(conn))
        require(
            "not ready to commit" in str(readback_refusal),
            f"incomplete-readback refusal changed shape: {readback_refusal}",
        )
        require(evidence(conn) == snapshot, "refused commit mutated the publication evidence")
        require(
            pending_stage(conn, operation, pending.challenge) == "selector_allowed",
            "stage changed despite the refused incomplete commit",
        )

    # P5: complete the set and publish; the seat is then a no-op.
    with conn.transaction():
        record_pending_unit_readback(conn, operation, pending.challenge, readbacks[home_b])
        publication_id = commit_pending_publication(conn)
        require(publication_id is not None, "commit seat did not publish")
        require(
            pending_stage(conn, operation, pending.challenge) == "committed",
            "stage after the commit seat is not committed",
        )
    with conn.transaction():
        stored = WriterPublication.model_validate_json(json.dumps(evidence(conn)))
        require(stored.pending is None, "pending survived the commit")
        current = stored.current
        if current is None:
            raise AssertionError("current publication is missing after commit")
        require(current.publication_id == publication_id, "committed id mismatch")
        require(current.operation == operation, "committed operation mismatch")
        require(
            current.activation_challenge == pending.challenge,
            "committed activation challenge mismatch",
        )
        require(current.units == pending.units, "committed roster differs from the journal")
        require(current.activation_digest is not None, "activation digest is missing")
        require(
            current.publication_id != previous.publication_id,
            "commit did not replace the previous current publication",
        )
        committed = evidence(conn)
        require(commit_pending_publication(conn) is None, "post-commit seat call was not a no-op")
        require(evidence(conn) == committed, "post-commit seat call mutated the evidence")

    return {
        "managedWriterChain": True,
        "schema": namespace,
        "stageTransitions": [
            "waiting_collection",
            "waiting_migration",
            "selector_allowed",
            "committed",
        ],
        "gateRefusals": [
            "migration seat refused before adoption",
            "commit seat refused the incomplete readback set",
        ],
        "commitIdempotent": True,
        "publicationId": str(publication_id),
    }


def main() -> None:
    home = Path(os.environ["AVA_HOME"]).resolve()
    require(
        sys.platform == "linux"
        and os.environ.get("GITHUB_ACTIONS") == "true"
        and home.is_relative_to(Path(os.environ["RUNNER_TEMP"]).resolve()),
        "managed-writer chain proof requires isolated Linux CI scratch",
    )
    namespace = "managed_writer_chain_" + uuid4().hex
    with psycopg.connect(os.environ["AVA_DB_URL"]) as conn:
        provision_schema(conn, namespace)
        try:
            proof = run_chain(conn, home, namespace)
        finally:
            conn.rollback()
            conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(namespace))
            )
            conn.commit()
    print(json.dumps(proof, sort_keys=True))


if __name__ == "__main__":
    main()
