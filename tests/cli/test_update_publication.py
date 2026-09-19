"""`cli.commands._update_publication` — the P1 journal seat, against real PostgreSQL.

The seat's only non-pure step is the journal compare-and-set on the real
`deployment_state` row (same reasoning as `tests/shared/test_managed_writer_publication.py`:
mocking the CAS tests nothing). Prepared receipts and candidate plans are
constructed directly — their producers (`_release_inventory`, `_release_services`)
are covered by their own prove scripts — so every refusal is pinned to exactly
the fact under test. One structural test pins this slice's inertness: no
production package imports the seat until the rollout wiring lands.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
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
    open_pending_publication,
    published_unit,
)
from shared.managed_writer_barrier import ManagedWriterBarrierError, RolloutIdentity
from shared.managed_writer_observation import ExpectedUnitWriters
from shared.managed_writer_publication import (
    CandidateUnitPlan,
    CommittedPublication,
    NormalService,
    PendingPublication,
    WriterPublication,
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
        conn, facts, operation=operation, candidate_digest=CANDIDATE_DIGEST
    )
    conn.commit()
    before = _evidence(conn)

    second = open_pending_publication(
        conn, facts, operation=operation, candidate_digest=CANDIDATE_DIGEST
    )
    conn.commit()

    assert second.challenge == first.challenge
    assert _evidence(conn) == before


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


def test_the_seat_has_no_production_callsite() -> None:
    """Slice 1b stays inert: only tests import the seat until the wiring lands.

    When the rollout Phase-0 wiring genuinely connects this seat, its own slice
    must update this pin consciously rather than import it quietly.
    """
    root = Path(__file__).resolve().parents[2]
    offenders = sorted(
        str(path.relative_to(root))
        for package in ("ava", "agent", "cli", "gateway", "services", "ops", "shared")
        for path in (root / package).rglob("*.py")
        if path.name != "_update_publication.py"
        and "_update_publication" in path.read_text(encoding="utf-8")
    )
    assert offenders == []
