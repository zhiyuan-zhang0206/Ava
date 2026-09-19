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

from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

import ops.publication_recovery as _pr
from ops.cluster import ClusterUpdateInProgress
from shared import updater_handoff as _handoff
from shared.cluster_lock import self_holder
from shared.managed_writer_barrier import (
    ManagedUnit,
    ManagedUnitClosure,
    ManagedWriterCollection,
    RolloutIdentity,
)
from shared.managed_writer_publication import (
    CommittedPublication,
    PendingPublication,
    PublishedUnit,
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
            "note=NULL, settle_hosts=NULL, settle_note=NULL, settle_started_at=NULL, "
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
        "SELECT holder, target_sha, phase, kind, note, expires_at, "
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
        "SELECT holder, target_sha, phase, kind, note, expires_at > clock_timestamp(), "
        "managed_writer_evidence->'pending'->'operation', "
        "managed_writer_evidence->'pending'->'challenge', "
        "managed_writer_evidence->'pending'->'collection'->'challenge', "
        "managed_writer_evidence->'current' FROM deployment_state WHERE id=1"
    ).fetchone()
    assert row is not None
    holder, target_sha, phase, kind, note, lease_live, operation, challenge, collection, current = (
        row
    )
    assert holder == new_holder
    assert target_sha == TARGET_SHA  # the replacement resumes the same rollout target
    assert (phase, kind, note) == ("updating", "rollout", None)
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


# ─── the emitted audit events exist in the registry ───────────────────────────


def test_recovery_events_are_registered() -> None:
    from shared.events.contract import EVENTS

    assert "managed_writer_recovery_claimed" in EVENTS
    assert "managed_writer_recovery_completed" in EVENTS
