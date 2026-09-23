"""`ops.publication_recovery` — the checked operator seat for a stranded pending publication.

Real PostgreSQL: the recovery is a journal compare-and-set on the `deployment_state`
row (same reasoning as `tests/shared/test_cluster_lock.py` — mocking the CAS tests
nothing), and the replacement closure is validated against the registered unit
inventory. Every liveness input (`holder_process_gone`, the updater handoff, the
orchestration session, the host updater lease) is stubbed at this module's own
seams, as `tests/ops/test_cluster_recover_op.py` does, so each refusal is pinned
to exactly the fact under test.

The seat has two postures: `pending_publication_recovery_op` performs every
non-mutating proof and then refuses BEFORE any mutation while the trusted closure
producer is not connected; `run_pending_publication_recovery` is the full
sequence that producer connects to (claim -> collect -> complete). Both audit
events are captured through the module's `insert_event_log` seam.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

import ops.publication_recovery as _pr
from ops import cluster_rpc
from ops.cluster import ClusterUpdateInProgress
from shared import updater_handoff as _handoff
from shared.cluster_lock import self_holder
from shared.managed_writer_barrier import (
    ManagedUnit,
    ManagedUnitClosure,
    ManagedWriterCollection,
    RolloutIdentity,
)
from shared.managed_writer_observation import ExpectedProcess
from shared.managed_writer_publication import (
    CommittedPublication,
    NormalService,
    NormalServiceReadback,
    PendingMigrationReceipt,
    PendingPublication,
    PublishedUnit,
    SelectorReadback,
    UnitActivationReadback,
    WriterPublication,
)

TARGET_SHA = "e" * 40
CANDIDATE_DIGEST = "f" * 64


def _unit() -> PublishedUnit:
    return PublishedUnit(
        machine="runner",
        home="/ava",
        inventory_digest="a" * 64,
        prepared_receipt_digest="d" * 64,
        artifact_digest="b" * 64,
        manifest_digest="c" * 64,
    )


def _reset_deploy_row(db_conn: psycopg.Connection) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE deployment_state SET holder=NULL, acquired_at=NULL, expires_at=NULL, "
            "settle_hosts=NULL, settle_note=NULL, settle_started_at=NULL, "
            "phase='stable', kind=NULL, target_sha=NULL, managed_writer_evidence=NULL "
            "WHERE id=1"
        )
        cur.execute("DELETE FROM machine_units")
        cur.execute("DELETE FROM machines")
    db_conn.commit()


@pytest.fixture
def recovery_db(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> Iterator[psycopg.Connection]:
    """The registered unit + a reset deploy row; every liveness seam stubbed calm."""
    monkeypatch.setattr(_pr, "holder_process_gone", _holder_gone)
    monkeypatch.setattr(_pr, "updater_lease_live", lambda: False)
    monkeypatch.setattr(_pr.cluster_session, "live_orchestration_session", lambda: None)
    monkeypatch.setattr(
        _handoff, "read", lambda: _handoff.UpdaterHandoffSnapshot(status="inactive")
    )
    monkeypatch.setattr(_handoff, "allows_generic_recovery", _allows_generic_recovery)

    _reset_deploy_row(db_conn)
    db_conn.execute("INSERT INTO machines(name) VALUES('runner')")
    db_conn.execute("INSERT INTO machine_units(machine_name, home) VALUES('runner', '/ava')")
    db_conn.commit()
    try:
        yield db_conn
    finally:
        _reset_deploy_row(db_conn)


def _abandoned_rollout(
    db_conn: psycopg.Connection,
    *,
    holder: str = "m1:pid9001",
    with_current: bool = False,
    expired: bool = True,
    valid_until: datetime | None = None,
    plan_digest: str | None = None,
) -> WriterPublication:
    """An executing-rollout row carrying a durable pending publication."""
    row = db_conn.execute(
        "UPDATE deployment_state SET phase='updating', kind='rollout', holder=%s, "
        "acquired_at=clock_timestamp() - interval '2 minutes', "
        "expires_at=clock_timestamp() + make_interval(secs => %s), target_sha=%s "
        "WHERE id=1 RETURNING acquired_at",
        (holder, -60.0 if expired else 600.0, TARGET_SHA),
    ).fetchone()
    assert row is not None
    acquired_at = row[0]
    operation = RolloutIdentity(holder=holder, acquired_at=acquired_at, target_sha=TARGET_SHA)
    current = (
        CommittedPublication(
            publication_id=uuid4(),
            operation=RolloutIdentity(
                holder="completed-holder",
                acquired_at=acquired_at - timedelta(hours=1),
                target_sha="1" * 40,
            ),
            committed_at=acquired_at - timedelta(minutes=30),
            units=(_unit(),),
        )
        if with_current
        else None
    )
    publication = WriterPublication(
        current=current,
        pending=PendingPublication(
            operation=operation,
            predecessor=current.publication_id if current is not None else None,
            candidate_digest=CANDIDATE_DIGEST,
            challenge=uuid4(),
            units=(_unit(),),
            valid_until=valid_until,
            plan_digest=plan_digest,
        ),
    )
    db_conn.execute(
        "UPDATE deployment_state SET managed_writer_evidence=%s WHERE id=1",
        (Jsonb(publication.model_dump(mode="json")),),
    )
    db_conn.commit()
    return publication


def _journal_snapshot(db_conn: psycopg.Connection) -> tuple[object, ...]:
    row = db_conn.execute(
        "SELECT holder, target_sha, phase, kind, settle_note, expires_at, "
        "managed_writer_evidence::text FROM deployment_state WHERE id=1"
    ).fetchone()
    assert row is not None
    return tuple(row)


def _collector(claim: _pr.PendingRecoveryClaim) -> ManagedWriterCollection:
    """A fresh complete closure for the replacement operation (the producer's job)."""
    return ManagedWriterCollection(
        operation=claim.operation,
        candidate_digest=claim.candidate_digest,
        challenge=claim.challenge,
        collected_at=claim.operation.acquired_at,
        valid_until=claim.operation.acquired_at + timedelta(seconds=120),
        units=tuple(
            ManagedUnitClosure(
                unit=ManagedUnit(
                    machine=unit.machine,
                    home=unit.home,
                    inventory_digest=unit.prepared_receipt_digest,
                ),
                boot_id=uuid4(),
                observer_instance=uuid4(),
                observation_digest="9" * 64,
                outcome="old_writers_absent_relaunchers_fenced",
            )
            for unit in claim.abandoned_units
        ),
    )


def _never_collect(_claim: _pr.PendingRecoveryClaim) -> ManagedWriterCollection:
    raise AssertionError("the closure producer must not run when there is nothing to recover")


def _holder_gone(_holder: str, **_kw: object) -> bool:
    return True


def _holder_alive(_holder: str, **_kw: object) -> bool:
    return False


def _allows_generic_recovery(_snapshot: object) -> bool:
    return True


def _capture_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []

    def _event(
        *,
        event_type: str,
        agent_id: int | None = None,
        source: str = "",
        payload: dict[str, Any] | None = None,
        **_kw: object,
    ) -> None:
        del agent_id, source
        events.append((event_type, payload or {}))

    monkeypatch.setattr(_pr, "insert_event_log", _event)
    return events


# ─── nothing to recover ───────────────────────────────────────────────────────


def test_nothing_pending_reports_no_recovery(recovery_db: psycopg.Connection) -> None:
    assert _pr.pending_publication_recovery_op() == {
        "recovered": False,
        "detail": "no pending publication is journaled",
    }
    assert _pr.run_pending_publication_recovery(collect=_never_collect) == {
        "recovered": False,
        "detail": "no pending publication is journaled",
    }


@pytest.mark.parametrize(
    "evidence",
    [
        '{"version":2,"current":null,"pending":{"request":"retained"}}',
        '{"version":1,"current":null,"pending":null}',
    ],
)
def test_unreadable_evidence_refuses_fail_closed(
    recovery_db: psycopg.Connection, evidence: str
) -> None:
    recovery_db.execute(
        "UPDATE deployment_state SET managed_writer_evidence=%s::jsonb WHERE id=1", (evidence,)
    )
    recovery_db.commit()

    with pytest.raises(ClusterUpdateInProgress, match="unreadable"):
        _pr.read_pending_recovery_state()
    with pytest.raises(ClusterUpdateInProgress, match="unreadable"):
        _pr.pending_publication_recovery_op()


# ─── the seat refuses before mutation until the producer connects ─────────────


def test_the_seat_refuses_before_mutation_until_the_producer_connects(
    recovery_db: psycopg.Connection,
) -> None:
    _abandoned_rollout(recovery_db)
    before = _journal_snapshot(recovery_db)

    with pytest.raises(ClusterUpdateInProgress, match="not connected"):
        _pr.pending_publication_recovery_op()

    assert _journal_snapshot(recovery_db) == before


def test_a_live_deploy_still_owns_the_cluster(
    recovery_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _abandoned_rollout(recovery_db)
    before = _journal_snapshot(recovery_db)
    monkeypatch.setattr(_pr.cluster_session, "live_orchestration_session", lambda: "ava-rollout")

    with pytest.raises(ClusterUpdateInProgress, match="ava-rollout"):
        _pr.pending_publication_recovery_op()

    assert _journal_snapshot(recovery_db) == before


def test_a_live_holder_process_refuses(
    recovery_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _abandoned_rollout(recovery_db, expired=False)
    before = _journal_snapshot(recovery_db)
    monkeypatch.setattr(_pr, "holder_process_gone", _holder_alive)

    with pytest.raises(ClusterUpdateInProgress, match="live process"):
        _pr.run_pending_publication_recovery(collect=_never_collect)

    assert _journal_snapshot(recovery_db) == before


def test_an_unprovable_recorded_holder_refuses(
    recovery_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _abandoned_rollout(recovery_db, holder="elsewhere:pid1")
    before = _journal_snapshot(recovery_db)
    monkeypatch.setattr(_pr, "holder_process_gone", _holder_alive)

    with pytest.raises(ClusterUpdateInProgress, match="cannot be proven gone"):
        _pr.pending_publication_recovery_op()

    assert _journal_snapshot(recovery_db) == before


def test_a_pending_updater_handoff_refuses(
    recovery_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _abandoned_rollout(recovery_db)
    monkeypatch.setattr(
        _handoff,
        "read",
        lambda: _handoff.UpdaterHandoffSnapshot(status="pending", expired=False),
    )

    with pytest.raises(ClusterUpdateInProgress, match="protected startup window"):
        _pr.pending_publication_recovery_op()


def test_a_live_host_updater_lease_refuses(
    recovery_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _abandoned_rollout(recovery_db)
    monkeypatch.setattr(_pr, "updater_lease_live", lambda: True)

    with pytest.raises(ClusterUpdateInProgress, match="updater lease"):
        _pr.pending_publication_recovery_op()


# ─── the full sequence (the seat the trusted producer connects to) ────────────


def test_run_replaces_the_abandoned_operation_under_a_fresh_closure(
    recovery_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    publication = _abandoned_rollout(recovery_db, with_current=True)
    events = _capture_events(monkeypatch)

    result = _pr.run_pending_publication_recovery(collect=_collector)

    new_holder = self_holder()
    assert result["recovered"] is True
    assert result["abandoned_holder"] == "m1:pid9001"
    assert result["new_holder"] == new_holder
    assert result["units"] == 1

    row = recovery_db.execute(
        "SELECT holder, target_sha, phase, kind, settle_note, expires_at > clock_timestamp(), "
        "managed_writer_evidence->'pending'->'operation', "
        "managed_writer_evidence->'pending'->'challenge', "
        "managed_writer_evidence->'pending'->'collection'->'challenge', "
        "managed_writer_evidence->'current' FROM deployment_state WHERE id=1"
    ).fetchone()
    assert row is not None
    (
        holder,
        target_sha,
        phase,
        kind,
        settle_note,
        lease_live,
        operation,
        challenge,
        collection,
        current,
    ) = row
    assert holder == new_holder
    assert target_sha == TARGET_SHA  # the replacement resumes the same rollout target
    assert (phase, kind, settle_note) == ("updating", "rollout", None)
    assert lease_live is True
    assert operation["holder"] == new_holder
    assert operation["target_sha"] == TARGET_SHA
    assert operation["acquired_at"]  # a fresh server-side lease identity
    assert str(challenge) == result["challenge"]
    assert str(collection) == result["challenge"]
    assert publication.current is not None
    assert current == publication.current.model_dump(mode="json")  # current preserved

    claimed = [payload for kind_, payload in events if kind_ == "managed_writer_recovery_claimed"]
    completed = [
        payload for kind_, payload in events if kind_ == "managed_writer_recovery_completed"
    ]
    assert [kind_ for kind_, _ in events] == [
        "managed_writer_recovery_claimed",
        "managed_writer_recovery_completed",
    ]
    assert claimed[0]["abandoned_holder"] == "m1:pid9001"
    assert claimed[0]["previous_holder"] == "m1:pid9001"
    assert claimed[0]["challenge"] == result["challenge"]
    assert completed[0]["new_holder"] == new_holder
    assert completed[0]["challenge"] == result["challenge"]
    assert completed[0]["units"] == 1


def test_recovery_does_not_carry_the_abandoned_plan_registration(
    recovery_db: psycopg.Connection,
) -> None:
    """A replacement's premise is its fresh closure, never the abandoned sealed plan."""
    _abandoned_rollout(
        recovery_db, valid_until=datetime(2026, 9, 21, tzinfo=UTC), plan_digest="f" * 64
    )

    result = _pr.run_pending_publication_recovery(collect=_collector)

    assert result["recovered"] is True
    row = recovery_db.execute(
        "SELECT managed_writer_evidence->'pending'->'valid_until', "
        "managed_writer_evidence->'pending'->'plan_digest' FROM deployment_state WHERE id=1"
    ).fetchone()
    assert row == (None, None)


def test_a_dead_holder_with_an_unexpired_lease_is_still_replaced(
    recovery_db: psycopg.Connection,
) -> None:
    """TTL expiry is not the exit evidence; the proven-dead local holder is."""
    _abandoned_rollout(recovery_db, expired=False)

    result = _pr.run_pending_publication_recovery(collect=_collector)

    assert result["recovered"] is True
    row = recovery_db.execute(
        "SELECT holder, target_sha FROM deployment_state WHERE id=1"
    ).fetchone()
    assert row == (self_holder(), TARGET_SHA)


# ─── claim/completion steps refuse on a changed journal or bad closure ───────


def test_the_claim_loses_when_the_journaled_operation_changed(
    recovery_db: psycopg.Connection,
) -> None:
    publication = _abandoned_rollout(recovery_db)
    state = _pr.read_pending_recovery_state()
    pending = publication.pending
    assert pending is not None
    moved = publication.model_copy(
        update={
            "pending": pending.model_copy(
                update={"operation": pending.operation.model_copy(update={"target_sha": "2" * 40})}
            )
        }
    )
    recovery_db.execute(
        "UPDATE deployment_state SET managed_writer_evidence=%s WHERE id=1",
        (Jsonb(moved.model_dump(mode="json")),),
    )
    recovery_db.commit()

    with pytest.raises(ClusterUpdateInProgress, match="changed"):
        _pr.claim_abandoned_pending_lease(state, observed=None)


def test_completion_refuses_a_claim_the_journal_moved_past(
    recovery_db: psycopg.Connection,
) -> None:
    _abandoned_rollout(recovery_db)
    state = _pr.read_pending_recovery_state()
    claim = _pr.claim_abandoned_pending_lease(state, observed=None)
    recovery_db.execute(
        "UPDATE deployment_state SET managed_writer_evidence=jsonb_set("
        "managed_writer_evidence, '{pending,operation,target_sha}', to_jsonb(%s::text)) "
        "WHERE id=1",
        ("3" * 40,),
    )
    recovery_db.commit()
    before = _journal_snapshot(recovery_db)

    with pytest.raises(ClusterUpdateInProgress, match="no longer the journal's pending record"):
        _pr.complete_pending_publication_recovery(claim, _collector(claim))

    assert _journal_snapshot(recovery_db) == before


def test_completion_refuses_a_replayed_or_incomplete_closure(
    recovery_db: psycopg.Connection,
) -> None:
    _abandoned_rollout(recovery_db)
    state = _pr.read_pending_recovery_state()
    claim = _pr.claim_abandoned_pending_lease(state, observed=None)
    wrong_challenge = _collector(claim).model_copy(update={"challenge": uuid4()})
    before = _journal_snapshot(recovery_db)

    with pytest.raises(ClusterUpdateInProgress, match="recovery evidence was refused"):
        _pr.complete_pending_publication_recovery(claim, wrong_challenge)

    assert _journal_snapshot(recovery_db) == before

    recovery_db.execute("INSERT INTO machine_units(machine_name, home) VALUES('runner', '/other')")
    recovery_db.commit()
    with pytest.raises(ClusterUpdateInProgress, match="recovery evidence was refused"):
        _pr.complete_pending_publication_recovery(claim, _collector(claim))


# ─── the exact pre-stop abort (task #4129 C-4) ────────────────────────────────


_ReadAnswer = dict[str, object] | BaseException | Callable[[], dict[str, object] | BaseException]


def _clean_read(machine: str = "runner", home: str = "/ava") -> dict[str, object]:
    return {"machine": machine, "home": home, "journal_present": False, "journal_stage": None}


def _stub_unit_reads(
    monkeypatch: pytest.MonkeyPatch, answers: list[_ReadAnswer]
) -> list[tuple[str, dict[str, object]]]:
    """Stub the per-unit journal probe (`ops.cluster_rpc.dispatch_to_machine`).

    Each answer is a result dict to return, an exception to raise, or a
    zero-arg callable performing a side effect and returning either.
    """
    calls: list[tuple[str, dict[str, object]]] = []

    async def _read(
        *, target_machine: str, kind: str, payload: dict[str, object], ops_url: str | None = None
    ) -> dict[str, object]:
        calls.append((target_machine, {"kind": kind, "payload": payload, "ops_url": ops_url}))
        entry = answers.pop(0)
        answer = entry() if callable(entry) else entry
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _read)
    return calls


def _store_pending(
    db_conn: psycopg.Connection, publication: WriterPublication, **updates: object
) -> None:
    pending = publication.pending
    assert pending is not None
    moved = publication.model_copy(update={"pending": pending.model_copy(update=updates)})
    db_conn.execute(
        "UPDATE deployment_state SET managed_writer_evidence=%s WHERE id=1",
        (Jsonb(moved.model_dump(mode="json")),),
    )
    db_conn.commit()


def _collection_for(publication: WriterPublication) -> ManagedWriterCollection:
    pending = publication.pending
    assert pending is not None
    return ManagedWriterCollection(
        operation=pending.operation,
        candidate_digest=pending.candidate_digest,
        challenge=pending.challenge,
        collected_at=pending.operation.acquired_at,
        valid_until=pending.operation.acquired_at + timedelta(seconds=120),
        units=tuple(
            ManagedUnitClosure(
                unit=ManagedUnit(
                    machine=unit.machine,
                    home=unit.home,
                    inventory_digest=unit.prepared_receipt_digest,
                ),
                boot_id=uuid4(),
                observer_instance=uuid4(),
                observation_digest="9" * 64,
                outcome="old_writers_absent_relaunchers_fenced",
            )
            for unit in pending.units
        ),
    )


def _migration_for(publication: WriterPublication) -> PendingMigrationReceipt:
    pending = publication.pending
    assert pending is not None
    return PendingMigrationReceipt(
        operation=pending.operation,
        challenge=pending.challenge,
        schema_digest="a" * 64,
        applied_names=(),
        verified_at=datetime.now(UTC),
    )


def _readback_for(publication: WriterPublication) -> UnitActivationReadback:
    pending = publication.pending
    assert pending is not None
    now = datetime.now(UTC)
    executable = f"/ava/releases/{'b' * 64}/venv/bin/python"
    service = NormalService(
        session="ava-ops",
        module=None,
        executable=executable,
        entrypoint=executable,
        command_digest="a" * 64,
    )
    return UnitActivationReadback(
        selector=SelectorReadback(
            unit=_unit(),
            challenge=pending.challenge,
            previous_digest=None,
            current_digest="c" * 64,
            observed_at=now,
            valid_until=now + timedelta(seconds=30),
        ),
        services=(
            NormalServiceReadback(
                service=service,
                supervisor=ExpectedProcess(pid=11, create_time=1.0),
                child=ExpectedProcess(pid=12, create_time=2.0),
                loaded_module=None,
                executable=executable,
                entrypoint=executable,
                artifact_digest="b" * 64,
                manifest_digest="c" * 64,
                readiness="normal",
                challenge=pending.challenge,
                observed_at=now,
                valid_until=now + timedelta(seconds=30),
                observation_digest="3" * 64,
            ),
        ),
    )


def test_pre_stop_abort_reports_nothing_to_abort(recovery_db: psycopg.Connection) -> None:
    assert _pr.pre_stop_abort_pending_publication_op() == {
        "aborted": False,
        "detail": "no pending publication is journaled",
    }


def test_pre_stop_abort_clears_a_never_effective_window(
    recovery_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The expired-lease arm: every journaled unit answers clean, the row clears."""
    publication = _abandoned_rollout(recovery_db, with_current=True)
    events = _capture_events(monkeypatch)
    calls = _stub_unit_reads(monkeypatch, [_clean_read()])

    result = _pr.pre_stop_abort_pending_publication_op()

    assert result == {
        "aborted": True,
        "abandoned_holder": "m1:pid9001",
        "target_sha": TARGET_SHA,
        "units": 1,
    }
    assert calls == [
        ("runner", {"kind": "cluster_bootstrap_recovery_read", "payload": {}, "ops_url": None})
    ]

    row = recovery_db.execute(
        "SELECT holder, acquired_at, expires_at, settle_note, phase, kind, target_sha, "
        "managed_writer_evidence->'pending', managed_writer_evidence->'current' "
        "FROM deployment_state WHERE id=1"
    ).fetchone()
    assert row is not None
    holder, acquired_at, expires_at, settle_note, phase, kind, target_sha, pending, current = row
    assert (holder, acquired_at, expires_at, settle_note) == (None, None, None, None)
    assert (phase, kind) == ("stable", None)
    # The target field is not this write's to clear; the release statement keeps it.
    assert target_sha == TARGET_SHA
    assert pending is None
    assert publication.current is not None
    assert current == publication.current.model_dump(mode="json")

    assert [event_type for event_type, _ in events] == ["managed_writer_pre_stop_aborted"]
    payload = events[0][1]
    assert payload["abandoned_holder"] == "m1:pid9001"
    assert payload["abandoned_target_sha"] == TARGET_SHA
    assert payload["units"] == 1
    assert payload["candidate_digest"] == CANDIDATE_DIGEST


def test_pre_stop_abort_pins_an_unexpired_dead_holder(
    recovery_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TTL expiry is not the exit evidence; the proven-dead lease identity is pinned."""
    _abandoned_rollout(recovery_db, expired=False)
    _capture_events(monkeypatch)
    _stub_unit_reads(monkeypatch, [_clean_read()])

    result = _pr.pre_stop_abort_pending_publication_op()

    assert result["aborted"] is True
    row = recovery_db.execute(
        "SELECT holder, phase, kind, managed_writer_evidence->'pending' "
        "FROM deployment_state WHERE id=1"
    ).fetchone()
    assert row == (None, "stable", None, None)


def test_abandon_refuses_a_settled_row_shape(recovery_db: psycopg.Connection) -> None:
    """The guarded write's row-shape clause has independent teeth (QA #3053 N2):
    a settled row carrying a pending journal is not the abort's to clear, even
    with the exact operation and lease pin."""
    publication = _abandoned_rollout(recovery_db)
    pending = publication.pending
    assert pending is not None
    recovery_db.execute("UPDATE deployment_state SET phase='stable' WHERE id=1")
    recovery_db.commit()
    before = _journal_snapshot(recovery_db)
    observed = (pending.operation.holder, pending.operation.acquired_at)

    with recovery_db.transaction():
        cleared = _pr.abandon_pending_publication_lease(
            recovery_db,
            expected_operation=pending.operation.model_dump(mode="json"),
            observed=observed,
        )

    assert cleared is False
    assert _journal_snapshot(recovery_db) == before


def test_pre_stop_abort_refuses_a_live_holder_process(
    recovery_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _abandoned_rollout(recovery_db, expired=False)
    before = _journal_snapshot(recovery_db)
    monkeypatch.setattr(_pr, "holder_process_gone", _holder_alive)

    with pytest.raises(ClusterUpdateInProgress, match="live process"):
        _pr.pre_stop_abort_pending_publication_op()

    assert _journal_snapshot(recovery_db) == before


def test_pre_stop_abort_refuses_a_unit_that_still_reports_a_journal(
    recovery_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _abandoned_rollout(recovery_db)
    before = _journal_snapshot(recovery_db)
    _stub_unit_reads(
        monkeypatch,
        [
            {
                "machine": "runner",
                "home": "/ava",
                "journal_present": True,
                "journal_stage": "prepared",
            }
        ],
    )

    with pytest.raises(ClusterUpdateInProgress, match="reports a bootstrap recovery journal"):
        _pr.pre_stop_abort_pending_publication_op()

    assert _journal_snapshot(recovery_db) == before


def test_pre_stop_abort_refuses_an_unreachable_unit(
    recovery_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _abandoned_rollout(recovery_db)
    before = _journal_snapshot(recovery_db)
    _stub_unit_reads(monkeypatch, [cluster_rpc.ClusterOpUnreachable("connect timed out")])

    with pytest.raises(
        ClusterUpdateInProgress, match="could not prove a journaled unit effect-free"
    ):
        _pr.pre_stop_abort_pending_publication_op()

    assert _journal_snapshot(recovery_db) == before


def test_pre_stop_abort_refuses_a_failed_read(
    recovery_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _abandoned_rollout(recovery_db)
    before = _journal_snapshot(recovery_db)
    _stub_unit_reads(monkeypatch, [cluster_rpc.ClusterOpFailed({"error": "route refused"})])

    with pytest.raises(ClusterUpdateInProgress, match="a bootstrap-recovery read failed"):
        _pr.pre_stop_abort_pending_publication_op()

    assert _journal_snapshot(recovery_db) == before


def test_pre_stop_abort_refuses_an_answer_from_another_unit(
    recovery_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _abandoned_rollout(recovery_db)
    before = _journal_snapshot(recovery_db)
    _stub_unit_reads(monkeypatch, [_clean_read(machine="elsewhere")])

    with pytest.raises(ClusterUpdateInProgress, match="belongs to another unit"):
        _pr.pre_stop_abort_pending_publication_op()

    assert _journal_snapshot(recovery_db) == before


def test_pre_stop_abort_refuses_when_the_journal_moved_mid_proof(
    recovery_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _abandoned_rollout(recovery_db)

    def _move_operation() -> dict[str, object]:
        recovery_db.execute(
            "UPDATE deployment_state SET managed_writer_evidence=jsonb_set("
            "managed_writer_evidence, '{pending,operation,target_sha}', to_jsonb(%s::text)) "
            "WHERE id=1",
            ("2" * 40,),
        )
        recovery_db.commit()
        return _clean_read()

    _stub_unit_reads(monkeypatch, [_move_operation])

    with pytest.raises(ClusterUpdateInProgress, match="pending operation changed"):
        _pr.pre_stop_abort_pending_publication_op()

    row = recovery_db.execute(
        "SELECT holder, phase, kind, "
        "managed_writer_evidence->'pending'->'operation'->>'target_sha' "
        "FROM deployment_state WHERE id=1"
    ).fetchone()
    assert row == ("m1:pid9001", "updating", "rollout", "2" * 40)


def test_pre_stop_abort_refuses_when_the_lease_moved_mid_proof(
    recovery_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _abandoned_rollout(recovery_db)

    def _move_lease() -> dict[str, object]:
        recovery_db.execute(
            "UPDATE deployment_state SET holder='elsewhere:pid1', "
            "acquired_at=clock_timestamp(), "
            "expires_at=clock_timestamp()+make_interval(secs=>600) WHERE id=1"
        )
        recovery_db.commit()
        return _clean_read()

    _stub_unit_reads(monkeypatch, [_move_lease])

    with pytest.raises(ClusterUpdateInProgress, match="journal or lease changed"):
        _pr.pre_stop_abort_pending_publication_op()

    row = recovery_db.execute(
        "SELECT holder, expires_at > clock_timestamp(), phase FROM deployment_state WHERE id=1"
    ).fetchone()
    assert row == ("elsewhere:pid1", True, "updating")


@pytest.mark.parametrize("face", ["collection", "migration", "readbacks"])
def test_pre_stop_abort_refuses_a_journal_that_records_effects(
    recovery_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, face: str
) -> None:
    publication = _abandoned_rollout(recovery_db, with_current=True)
    if face == "collection":
        _store_pending(recovery_db, publication, collection=_collection_for(publication))
    elif face == "migration":
        _store_pending(recovery_db, publication, migration=_migration_for(publication))
    else:
        _store_pending(recovery_db, publication, unit_readbacks=(_readback_for(publication),))
    _stub_unit_reads(monkeypatch, [_clean_read()])
    before = _journal_snapshot(recovery_db)

    with pytest.raises(ClusterUpdateInProgress, match="already records effects"):
        _pr.pre_stop_abort_pending_publication_op()

    assert _journal_snapshot(recovery_db) == before


# ─── the emitted audit events exist in the registry ───────────────────────────


def test_recovery_events_are_registered() -> None:
    from shared.events.contract import EVENTS

    assert "managed_writer_recovery_claimed" in EVENTS
    assert "managed_writer_recovery_completed" in EVENTS
    assert "managed_writer_pre_stop_aborted" in EVENTS
