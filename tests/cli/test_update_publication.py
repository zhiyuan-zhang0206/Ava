"""`cli.commands._update_publication` — the publication seats, against real PostgreSQL.

The seats' only non-pure steps are their compare-and-set transitions on the
real `deployment_state` row (same reasoning as
`tests/shared/test_managed_writer_publication.py`: mocking the CAS tests
nothing). Prepared receipts and candidate plans are constructed directly —
their producers (`_release_inventory`, `_release_services`) are covered by
their own prove scripts — so every refusal is pinned to exactly the fact under
test. One structural test pins the seats' inertness: no production package
imports the module until the rollout wiring lands.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Generator, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from cli.commands._update_publication import (
    PreparedUnitPublication,
    build_pending_publication,
    commit_pending_publication,
    open_pending_publication,
    published_unit,
)
from shared.cluster_lock import release_update_lock
from shared.managed_writer_activation import (
    NormalServiceReadback,
    SelectorReadback,
    UnitActivationReadback,
    record_pending_migration,
    record_pending_unit_readback,
)
from shared.managed_writer_barrier import (
    ManagedUnit,
    ManagedUnitClosure,
    ManagedWriterBarrierError,
    ManagedWriterCollection,
    RolloutIdentity,
)
from shared.managed_writer_observation import ExpectedProcess, ExpectedUnitWriters
from shared.managed_writer_publication import (
    CandidateUnitPlan,
    CommittedPublication,
    NormalService,
    PendingPublication,
    WriterPublication,
    adopt_pending_collection,
    require_current_publication,
)
from shared.runtime_publication_input import PreparationReceipt, PreparedService

MACHINE = "runner"
HOME = "/ava"
STOPPED_HOME = "/ava-two"
TARGET_SHA = "e" * 40


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


CANDIDATE_DIGEST = _digest("candidate")
SCHEMA_DIGEST = _digest("schema")


def _receipt(home: str) -> PreparationReceipt:
    expected = ExpectedUnitWriters(
        machine=MACHINE,
        home=home,
        artifact_digest=_digest(f"artifact:{home}"),
        manifest_digest=_digest(f"manifest:{home}"),
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


def _facts(home: str) -> PreparedUnitPublication:
    receipt = _receipt(home)
    return PreparedUnitPublication(
        receipt=receipt,
        prepared_receipt_digest=_digest(f"receipt:{home}"),
        artifact_digest=receipt.expected.artifact_digest,
        manifest_digest=receipt.expected.manifest_digest,
        candidate=None,
    )


def _candidate(facts: PreparedUnitPublication) -> CandidateUnitPlan:
    unit = published_unit(facts)
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
                command_digest=_digest("command"),
            ),
        ),
        previous_selector_digest=None,
        selector_digest=hashlib.sha256(
            (json.dumps(selector, sort_keys=True, separators=(",", ":")) + "\n").encode()
        ).hexdigest(),
    )


def _with_candidate(facts: PreparedUnitPublication) -> PreparedUnitPublication:
    return replace(facts, candidate=_candidate(facts))


def _rollout_identity() -> RolloutIdentity:
    return RolloutIdentity(
        holder="gateway:pid77", acquired_at=datetime.now(UTC), target_sha=TARGET_SHA
    )


def _reset_deployment(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE deployment_state SET holder=NULL, acquired_at=NULL, expires_at=NULL, "
            "note=NULL, settle_hosts=NULL, settle_note=NULL, settle_started_at=NULL, "
            "phase='stable', kind=NULL, target_sha=NULL, managed_writer_evidence=NULL WHERE id=1"
        )
        cur.execute("DELETE FROM machine_units")
        cur.execute("DELETE FROM machines")
    conn.commit()


@pytest.fixture
def publication_db(db_conn: psycopg.Connection) -> Iterator[psycopg.Connection]:
    """Two registered units (one stopped) on a reset singleton deploy row."""
    _reset_deployment(db_conn)
    db_conn.execute("INSERT INTO machines(name) VALUES(%s)", (MACHINE,))
    db_conn.execute("INSERT INTO machine_units(machine_name, home) VALUES(%s, %s)", (MACHINE, HOME))
    db_conn.execute(
        "INSERT INTO machine_units(machine_name, home, stopped_at) VALUES(%s, %s, now())",
        (MACHINE, STOPPED_HOME),
    )
    db_conn.commit()
    try:
        yield db_conn
    finally:
        _reset_deployment(db_conn)


def _acquire_rollout(
    conn: psycopg.Connection, *, holder: str = "gateway:pid77", ttl_s: float = 300.0
) -> RolloutIdentity:
    row = conn.execute(
        "UPDATE deployment_state SET phase='updating', kind='rollout', holder=%s, "
        "acquired_at=clock_timestamp(), expires_at=clock_timestamp() + make_interval(secs => %s), "
        "note=NULL, target_sha=%s WHERE id=1 RETURNING acquired_at",
        (holder, ttl_s, TARGET_SHA),
    ).fetchone()
    assert row is not None
    return RolloutIdentity(holder=holder, acquired_at=row[0], target_sha=TARGET_SHA)


def _seed_current(conn: psycopg.Connection) -> CommittedPublication:
    row = conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    now = row[0]
    current = CommittedPublication(
        publication_id=uuid4(),
        operation=RolloutIdentity(
            holder="completed-holder", acquired_at=now - timedelta(hours=1), target_sha="1" * 40
        ),
        committed_at=now - timedelta(minutes=30),
        units=(published_unit(_facts(HOME)), published_unit(_facts(STOPPED_HOME))),
    )
    conn.execute(
        "UPDATE deployment_state SET managed_writer_evidence=%s WHERE id=1",
        (Jsonb(WriterPublication(current=current).model_dump(mode="json")),),
    )
    return current


def _evidence(conn: psycopg.Connection) -> object:
    row = conn.execute("SELECT managed_writer_evidence FROM deployment_state WHERE id=1").fetchone()
    assert row is not None
    return row[0]


def _stored_publication(conn: psycopg.Connection) -> WriterPublication:
    return WriterPublication.model_validate_json(json.dumps(_evidence(conn)))


def test_published_unit_binds_receipt_and_observer_digests() -> None:
    facts = _facts(HOME)
    unit = published_unit(facts)
    assert (unit.machine, unit.home) == (MACHINE, HOME)
    assert unit.inventory_digest == facts.receipt.inventory_digest
    assert unit.inventory_digest == facts.receipt.expected.unit().inventory_digest
    assert unit.prepared_receipt_digest == facts.prepared_receipt_digest
    assert (unit.artifact_digest, unit.manifest_digest) == (
        facts.artifact_digest,
        facts.manifest_digest,
    )


def test_open_journals_the_complete_roster_and_preserves_current(
    publication_db: psycopg.Connection,
) -> None:
    conn = publication_db
    current = _seed_current(conn)
    operation = _acquire_rollout(conn)

    pending = open_pending_publication(
        conn,
        (_facts(HOME), _facts(STOPPED_HOME)),
        operation=operation,
        candidate_digest=CANDIDATE_DIGEST,
    )
    conn.commit()

    stored = _stored_publication(conn)
    assert stored.current == current
    assert stored.pending == pending
    assert stored.pending is not None
    assert stored.pending.operation == operation
    assert stored.pending.predecessor == current.publication_id
    assert [(unit.machine, unit.home) for unit in stored.pending.units] == [
        (MACHINE, HOME),
        (MACHINE, STOPPED_HOME),
    ]
    assert stored.pending.normal_start_plan is None
    assert isinstance(stored.pending.challenge, UUID)
    # The seat opens the journal; it does not touch the operation columns.
    row = conn.execute("SELECT phase, kind, holder FROM deployment_state WHERE id=1").fetchone()
    assert row == ("updating", "rollout", "gateway:pid77")


def test_open_without_current_has_no_predecessor(publication_db: psycopg.Connection) -> None:
    conn = publication_db
    operation = _acquire_rollout(conn)

    open_pending_publication(
        conn,
        (_facts(HOME), _facts(STOPPED_HOME)),
        operation=operation,
        candidate_digest=CANDIDATE_DIGEST,
    )
    conn.commit()

    stored = _stored_publication(conn)
    assert stored.current is None
    assert stored.pending is not None
    assert stored.pending.predecessor is None


def test_open_registers_the_begin_execution_v_and_plan_digest(
    publication_db: psycopg.Connection,
) -> None:
    """F1: the journal durably registers V and the sealed plan's digest."""
    conn = publication_db
    operation = _acquire_rollout(conn)
    valid_until = datetime(2026, 9, 21, tzinfo=UTC)
    plan_digest = _digest("sealed-plan")

    pending = open_pending_publication(
        conn,
        (_facts(HOME), _facts(STOPPED_HOME)),
        operation=operation,
        candidate_digest=CANDIDATE_DIGEST,
        valid_until=valid_until,
        plan_digest=plan_digest,
    )
    conn.commit()

    stored = _stored_publication(conn)
    assert stored.pending == pending
    assert stored.pending is not None
    assert stored.pending.valid_until == valid_until
    assert stored.pending.plan_digest == plan_digest


def test_open_journals_the_normal_start_plan_for_every_unit(
    publication_db: psycopg.Connection,
) -> None:
    conn = publication_db
    operation = _acquire_rollout(conn)

    pending = open_pending_publication(
        conn,
        (_with_candidate(_facts(HOME)), _with_candidate(_facts(STOPPED_HOME))),
        operation=operation,
        candidate_digest=CANDIDATE_DIGEST,
        schema_digest=SCHEMA_DIGEST,
        applied_names=("baseline", "second"),
    )
    conn.commit()

    stored = _stored_publication(conn)
    assert stored.pending == pending
    assert stored.pending is not None
    plan = stored.pending.normal_start_plan
    assert plan is not None
    assert plan.schema_digest == SCHEMA_DIGEST
    assert plan.applied_names == ("baseline", "second")
    assert tuple(entry.unit for entry in plan.units) == stored.pending.units


def test_retry_adopts_the_journaled_challenge_byte_stably(
    publication_db: psycopg.Connection,
) -> None:
    conn = publication_db
    _seed_current(conn)
    operation = _acquire_rollout(conn)
    facts = (_facts(HOME), _facts(STOPPED_HOME))

    first = open_pending_publication(
        conn,
        facts,
        operation=operation,
        candidate_digest=CANDIDATE_DIGEST,
        valid_until=datetime(2026, 9, 21, tzinfo=UTC),
        plan_digest=_digest("first-plan"),
    )
    conn.commit()
    before = _evidence(conn)

    second = open_pending_publication(
        conn,
        facts,
        operation=operation,
        candidate_digest=CANDIDATE_DIGEST,
        valid_until=datetime(2026, 9, 22, tzinfo=UTC),
        plan_digest=_digest("second-plan"),
    )
    conn.commit()

    assert second.challenge == first.challenge
    assert second.valid_until == first.valid_until
    assert second.plan_digest == first.plan_digest
    assert _evidence(conn) == before


def test_a_pre_registration_journal_stays_readable(
    publication_db: psycopg.Connection,
) -> None:
    """F1 compatibility: stored v2 journals without the two fields still parse."""
    conn = publication_db
    operation = _acquire_rollout(conn)
    pending = open_pending_publication(
        conn,
        (_facts(HOME), _facts(STOPPED_HOME)),
        operation=operation,
        candidate_digest=CANDIDATE_DIGEST,
        valid_until=datetime(2026, 9, 21, tzinfo=UTC),
        plan_digest=_digest("sealed-plan"),
    )
    conn.commit()
    evidence = WriterPublication(pending=pending).model_dump(mode="json")
    del evidence["pending"]["valid_until"]
    del evidence["pending"]["plan_digest"]
    conn.execute(
        "UPDATE deployment_state SET managed_writer_evidence=%s WHERE id=1",
        (Jsonb(evidence),),
    )
    conn.commit()

    stored = _stored_publication(conn)

    assert stored.pending is not None
    assert stored.pending.valid_until is None
    assert stored.pending.plan_digest is None


def test_open_refuses_a_plan_that_omits_a_registered_unit(
    publication_db: psycopg.Connection,
) -> None:
    conn = publication_db
    operation = _acquire_rollout(conn)

    with pytest.raises(ManagedWriterBarrierError, match="omits registered units"):
        open_pending_publication(
            conn,
            (_facts(HOME),),
            operation=operation,
            candidate_digest=CANDIDATE_DIGEST,
        )
    conn.rollback()
    assert _evidence(conn) is None


def test_open_refuses_a_foreign_pending_publication(publication_db: psycopg.Connection) -> None:
    conn = publication_db
    current = _seed_current(conn)
    row = conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    now = row[0]
    foreign = PendingPublication(
        operation=RolloutIdentity(
            holder="crashed-holder", acquired_at=now - timedelta(hours=1), target_sha="2" * 40
        ),
        predecessor=current.publication_id,
        candidate_digest=_digest("foreign-candidate"),
        challenge=uuid4(),
        units=(published_unit(_facts(HOME)), published_unit(_facts(STOPPED_HOME))),
    )
    conn.execute(
        "UPDATE deployment_state SET managed_writer_evidence=%s WHERE id=1",
        (Jsonb(WriterPublication(current=current, pending=foreign).model_dump(mode="json")),),
    )
    conn.commit()
    operation = _acquire_rollout(conn)
    before = _evidence(conn)

    with pytest.raises(ManagedWriterBarrierError, match="explicit recovery"):
        open_pending_publication(
            conn,
            (_facts(HOME), _facts(STOPPED_HOME)),
            operation=operation,
            candidate_digest=CANDIDATE_DIGEST,
        )
    conn.rollback()
    assert _evidence(conn) == before


@pytest.mark.parametrize("shape", ["stable", "expired"])
def test_open_refuses_when_the_operation_owns_no_live_lease(
    publication_db: psycopg.Connection, shape: str
) -> None:
    conn = publication_db
    if shape == "stable":
        operation = _rollout_identity()
        conn.execute("SELECT 1")  # the caller's transaction is open; no live lease is in it
    else:
        operation = _acquire_rollout(conn, ttl_s=-60.0)

    with pytest.raises(ManagedWriterBarrierError, match="live rollout"):
        open_pending_publication(
            conn,
            (_facts(HOME), _facts(STOPPED_HOME)),
            operation=operation,
            candidate_digest=CANDIDATE_DIGEST,
        )
    conn.rollback()
    assert _evidence(conn) is None


def test_open_requires_a_caller_owned_transaction(
    publication_db: psycopg.Connection,
) -> None:
    conn = publication_db
    conn.commit()  # no transaction open: psycopg starts one with the caller's first statement

    with pytest.raises(ManagedWriterBarrierError, match="caller-owned transaction"):
        open_pending_publication(
            conn,
            (_facts(HOME),),
            operation=_rollout_identity(),
            candidate_digest=CANDIDATE_DIGEST,
        )


def test_builder_refuses_empty_and_duplicate_units() -> None:
    operation = _rollout_identity()
    with pytest.raises(ManagedWriterBarrierError, match="requires a prepared unit"):
        build_pending_publication(
            (),
            operation=operation,
            predecessor=None,
            candidate_digest=CANDIDATE_DIGEST,
            challenge=uuid4(),
        )
    with pytest.raises(ManagedWriterBarrierError, match="unique by machine and home"):
        build_pending_publication(
            (_facts(HOME), _facts(HOME)),
            operation=operation,
            predecessor=None,
            candidate_digest=CANDIDATE_DIGEST,
            challenge=uuid4(),
        )


def test_builder_refuses_plans_covering_only_some_units() -> None:
    mixed = (_with_candidate(_facts(HOME)), _facts(STOPPED_HOME))
    with pytest.raises(ManagedWriterBarrierError, match="cover every prepared unit"):
        build_pending_publication(
            mixed,
            operation=_rollout_identity(),
            predecessor=None,
            candidate_digest=CANDIDATE_DIGEST,
            challenge=uuid4(),
            schema_digest=SCHEMA_DIGEST,
            applied_names=("baseline",),
        )


def test_builder_refuses_plan_inputs_without_candidate_plans() -> None:
    facts = (_facts(HOME),)
    with pytest.raises(ManagedWriterBarrierError, match="require candidate plans"):
        build_pending_publication(
            facts,
            operation=_rollout_identity(),
            predecessor=None,
            candidate_digest=CANDIDATE_DIGEST,
            challenge=uuid4(),
            schema_digest=SCHEMA_DIGEST,
        )
    with pytest.raises(ManagedWriterBarrierError, match="require candidate plans"):
        build_pending_publication(
            facts,
            operation=_rollout_identity(),
            predecessor=None,
            candidate_digest=CANDIDATE_DIGEST,
            challenge=uuid4(),
            applied_names=("baseline",),
        )


def test_builder_requires_the_applied_schema_digest_for_a_plan() -> None:
    with pytest.raises(ManagedWriterBarrierError, match="schema digest"):
        build_pending_publication(
            (_with_candidate(_facts(HOME)),),
            operation=_rollout_identity(),
            predecessor=None,
            candidate_digest=CANDIDATE_DIGEST,
            challenge=uuid4(),
        )


def test_builder_refuses_unsorted_or_duplicated_applied_names() -> None:
    facts = (_with_candidate(_facts(HOME)),)
    for names in (("second", "baseline"), ("baseline", "baseline")):
        with pytest.raises(ManagedWriterBarrierError, match="sorted set"):
            build_pending_publication(
                facts,
                operation=_rollout_identity(),
                predecessor=None,
                candidate_digest=CANDIDATE_DIGEST,
                challenge=uuid4(),
                schema_digest=SCHEMA_DIGEST,
                applied_names=names,
            )


def test_candidate_plan_must_bind_the_prepared_unit() -> None:
    facts = replace(_facts(HOME), candidate=_candidate(_facts(STOPPED_HOME)))
    with pytest.raises(ManagedWriterBarrierError, match="different prepared unit"):
        published_unit(facts)


@pytest.mark.parametrize("field", ["receipt_digest", "manifest"])
def test_published_unit_refuses_inconsistent_prepared_facts(field: str) -> None:
    if field == "receipt_digest":
        facts = replace(
            _facts(HOME),
            receipt=_facts(HOME).receipt.model_copy(update={"inventory_digest": _digest("other")}),
        )
        match = "inventory digest"
    else:
        facts = replace(_facts(HOME), manifest_digest=_digest("other"))
        match = "different images"
    with pytest.raises(ManagedWriterBarrierError, match=match):
        published_unit(facts)


def _closure(conn: psycopg.Connection, pending: PendingPublication) -> ManagedWriterCollection:
    row = conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    now = row[0]
    return ManagedWriterCollection(
        operation=pending.operation,
        candidate_digest=pending.candidate_digest,
        challenge=pending.challenge,
        collected_at=now,
        valid_until=now + timedelta(seconds=30),
        units=tuple(
            ManagedUnitClosure(
                unit=ManagedUnit(
                    machine=entry.machine,
                    home=entry.home,
                    inventory_digest=entry.prepared_receipt_digest,
                ),
                boot_id=uuid4(),
                observer_instance=uuid4(),
                observation_digest="9" * 64,
                outcome="old_writers_absent_relaunchers_fenced",
            )
            for entry in pending.units
        ),
    )


def _unit_readback(
    conn: psycopg.Connection, pending: PendingPublication, expected: CandidateUnitPlan
) -> UnitActivationReadback:
    row = conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    now = row[0]
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


def _commit_ready_pending(
    conn: psycopg.Connection, *, readbacks: tuple[str, ...] = (HOME, STOPPED_HOME)
) -> PendingPublication:
    """Journal, collection, migration receipt and the requested unit readbacks.

    The pre-commit chain each seat test starts from: a live rollout, a complete
    candidate plan, adopted closure, the applied migration SET, and readbacks
    for exactly the named units (a strict subset for the incompleteness tests).
    """
    operation = _acquire_rollout(conn)
    # The test database carries the real applied SET; the receipt must read it.
    applied = tuple(
        row[0] for row in conn.execute("SELECT name FROM schema_migrations ORDER BY name")
    )
    pending = open_pending_publication(
        conn,
        (_with_candidate(_facts(HOME)), _with_candidate(_facts(STOPPED_HOME))),
        operation=operation,
        candidate_digest=CANDIDATE_DIGEST,
        schema_digest=SCHEMA_DIGEST,
        applied_names=applied,
    )
    adopt_pending_collection(conn, _closure(conn, pending))
    record_pending_migration(conn, pending.operation, pending.challenge)
    assert pending.normal_start_plan is not None
    expected = {entry.unit.home: entry for entry in pending.normal_start_plan.units}
    for home in readbacks:
        record_pending_unit_readback(
            conn,
            pending.operation,
            pending.challenge,
            _unit_readback(conn, pending, expected[home]),
        )
    return pending


def test_commit_seat_skips_without_pending(publication_db: psycopg.Connection) -> None:
    conn = publication_db
    _seed_current(conn)
    before = _evidence(conn)

    assert commit_pending_publication(conn) is None
    assert _evidence(conn) == before
    conn.rollback()


def test_commit_seat_publishes_the_complete_chain(publication_db: psycopg.Connection) -> None:
    conn = publication_db
    previous = _seed_current(conn)
    pending = _commit_ready_pending(conn)
    conn.commit()

    conn.execute("SELECT 1")  # the caller's transaction is open
    committed = commit_pending_publication(conn)
    conn.commit()

    assert committed is not None
    stored = _stored_publication(conn)
    assert stored.pending is None
    assert stored.current is not None
    assert stored.current.publication_id == committed
    assert stored.current.publication_id != previous.publication_id
    assert stored.current.operation == pending.operation
    assert stored.current.units == pending.units
    assert stored.current.activation_challenge == pending.challenge
    assert stored.current.activation_digest is not None
    conn.rollback()


def test_commit_seat_refuses_partial_readbacks(publication_db: psycopg.Connection) -> None:
    conn = publication_db
    _seed_current(conn)
    _commit_ready_pending(conn, readbacks=(HOME,))
    conn.commit()
    before = _evidence(conn)

    conn.execute("SELECT 1")  # the caller's transaction is open
    with pytest.raises(ManagedWriterBarrierError, match="not ready to commit"):
        commit_pending_publication(conn)
    conn.rollback()

    assert _evidence(conn) == before
    assert _stored_publication(conn).pending is not None
    conn.rollback()


def test_commit_seat_is_a_noop_after_commit(publication_db: psycopg.Connection) -> None:
    conn = publication_db
    _seed_current(conn)
    _commit_ready_pending(conn)
    conn.commit()

    conn.execute("SELECT 1")
    first = commit_pending_publication(conn)
    conn.commit()
    assert first is not None
    before = _evidence(conn)

    conn.execute("SELECT 1")
    assert commit_pending_publication(conn) is None
    assert _evidence(conn) == before
    conn.rollback()


def test_commit_seat_requires_a_caller_owned_transaction(
    publication_db: psycopg.Connection,
) -> None:
    conn = publication_db
    conn.commit()  # no transaction open: psycopg starts one with the caller's first statement

    with pytest.raises(ManagedWriterBarrierError, match="caller-owned transaction"):
        commit_pending_publication(conn)


def test_commit_seat_leaves_the_release_and_phase_to_the_existing_finalizer(
    publication_db: psycopg.Connection,
) -> None:
    conn = publication_db
    _seed_current(conn)
    _commit_ready_pending(conn)
    conn.commit()

    # A durable pending journal refuses the guarded generic release...
    release_update_lock("gateway:pid77")
    row = conn.execute("SELECT phase, holder FROM deployment_state WHERE id=1").fetchone()
    assert row == ("updating", "gateway:pid77")

    # ...the seat commits the evidence and nothing else...
    conn.execute("SELECT 1")
    committed = commit_pending_publication(conn)
    conn.commit()
    assert committed is not None
    row = conn.execute("SELECT phase, holder FROM deployment_state WHERE id=1").fetchone()
    assert row == ("updating", "gateway:pid77")

    # ...ordinary admission still refuses while the phase is not stable...
    with pytest.raises(ManagedWriterBarrierError, match="not settled"):
        require_current_publication(
            conn,
            published_unit(_facts(HOME)),
            selector_artifact_digest=_facts(HOME).artifact_digest,
            selector_manifest_digest=_facts(HOME).manifest_digest,
        )
    conn.rollback()

    # ...and the release now settles because the commit cleared the pending entry.
    release_update_lock("gateway:pid77")
    row = conn.execute("SELECT phase, holder FROM deployment_state WHERE id=1").fetchone()
    assert row == ("stable", None)
    require_current_publication(
        conn,
        published_unit(_facts(HOME)),
        selector_artifact_digest=_facts(HOME).artifact_digest,
        selector_manifest_digest=_facts(HOME).manifest_digest,
    )
    conn.rollback()


def test_commit_step_publishes_the_complete_chain_through_the_coordinator(
    publication_db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The E2-a post-Phase-B step: an active decision publishes through the seat."""
    from cli.commands import _managed_writer_mode as mode_mod
    from cli.commands import _managed_writer_wiring as wiring

    conn = publication_db
    _seed_current(conn)
    pending = _commit_ready_pending(conn)
    conn.commit()

    @contextlib.contextmanager
    def _txn() -> Generator[psycopg.Connection, None, None]:
        with conn.transaction():
            yield conn

    monkeypatch.setattr("shared.db_transaction.write_transaction", _txn)
    monkeypatch.setattr(
        mode_mod, "managed_writer_mode", lambda: mode_mod.ManagedWriterMode("active")
    )

    assert wiring._commit_managed_writer_publication() == 0
    stored = _stored_publication(conn)
    assert stored.pending is None
    assert stored.current is not None
    assert stored.current.activation_challenge == pending.challenge
    assert "committed" in capsys.readouterr().out
    conn.rollback()


def test_the_seat_has_no_unnamed_production_callsite() -> None:
    """The seats' importers are named here: tests, plus the modules below.

    Slices 1b/1e landed the seats inert; each managed-writer wiring slice
    connects its seat in its own reviewed change and updates this pin
    consciously rather than importing quietly.

    Four conscious exceptions, each added by its own reviewed change:

    - the enable-point gate (`cli/commands/_managed_writer_mode.py`) reads this
      module's `MANAGED_WRITER_WIRING_COMPLETE` completion declaration -- the
      module the declaration proves -- without importing or calling the seats
      (task #4128's managed-writer wiring);
    - the coordinator wiring (`cli/commands/_managed_writer_wiring.py`) imports
      the P5 commit seat from its post-Phase-B step and calls it under the
      enable point's recorded `active` decision (E2-a), and calls the dispatch
      chain below from its begin position (E2-b); its collect position still
      refuses under `active` before reaching any seat import;
    - the dispatch gather (`cli/commands/_managed_writer_gather.py`) imports
      the `PreparedUnitPublication` datum type -- the begin seat's exact
      input -- while verifying each unit's prepared-facts shipment
      (task #4129 I2); it imports no seat function and performs no effect,
      and the dispatch chain calls it from the begin position;
    - the dispatch chain (`cli/commands/_managed_writer_dispatch.py`) imports
      the P1 seat's `open_pending_publication` -- the journal it opens from the
      begin position -- plus the `published_unit` datum alias (task #4129 I3,
      channel B), all under the wiring's `active` decision.

    The declaration stays False until the last wiring slice flips it; the
    collect refusal persists until the collection channel connects it (the
    begin chain is connected by task #4129 I3).
    """
    root = Path(__file__).resolve().parents[2]
    allowed = {
        "cli/commands/_managed_writer_mode.py",
        "cli/commands/_managed_writer_wiring.py",
        "cli/commands/_managed_writer_gather.py",
        "cli/commands/_managed_writer_dispatch.py",
    }
    offenders = sorted(
        str(path.relative_to(root))
        for package in ("ava", "agent", "cli", "gateway", "services", "ops", "shared")
        for path in (root / package).rglob("*.py")
        if path.name != "_update_publication.py"
        and str(path.relative_to(root)) not in allowed
        and "_update_publication" in path.read_text(encoding="utf-8")
    )
    assert offenders == []
