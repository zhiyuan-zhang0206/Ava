"""Event capture for log-native leases: gate, failures, central attribution, runner privileges."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from base import telemetry
from base.agents import impersonation as leases
from base.agents.impersonation import history as history
from base.agents.impersonation import manifest as capture
from base.agents.impersonation.event_signals import emit_incomplete_event_logs
from base.agents.impersonation.manifest import (
    LocalParticipant,
    bind_local_participant,
    capture_local_event,
    close_local_participant_admission,
    emit_recorded_central_event,
    open_local_participant,
    pending_reason,
    record_central_event,
    seal_local_participant,
    unbind_local_participant,
)
from base.agents.impersonation.tests import test_history as history_cases
from base.agents.messages.caller_identity import CallerIdentity
from base.cluster.authority.event_grants import grant_event_log_runner_access
from base.cluster.machine import machine_name
from base.config import settings
from base.db import Database, create_agent
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.telemetry import Event
from base.telemetry.audit_events import audit_event_uid, prepare_event_log
from tests._containers import grant_runner_login
from tests.impersonation_support import attested_caller, recorded_tree

# The capability group the grants target, and the generation-shaped login that
# inherits it (the only identity that logs in).
_RUNNER_GROUP = "ava_runner"
_RUNNER_LOGIN = "ava_g0_runner"


def _owner(db_conn: psycopg.Connection[Any]) -> RuntimeIncarnation:
    agent_id = create_agent(db_conn)
    incarnation = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_generation,runtime_owner,"
        "runtime_kind,lease_expires_at) VALUES(%s,'idling',%s,%s,%s,'process',"
        "clock_timestamp()+interval '10 minutes')",
        (agent_id, machine_name(), incarnation.generation, incarnation.owner),
    )
    db_conn.commit()
    return incarnation


@pytest.fixture
def owner(db_conn: psycopg.Connection[Any]) -> RuntimeIncarnation:
    return _owner(db_conn)


@pytest.fixture
def lease(owner: RuntimeIncarnation) -> dict[str, Any]:
    return history_cases.start(owner)


def _sdk_event(agent_id: int, marker: str) -> Event:
    return Event(
        ts=datetime.now(UTC),
        trace_id=None,
        span_id=None,
        agent_id=agent_id,
        machine="test-machine",
        cluster="test-cluster",
        process="test",
        category="telemetry",
        event_name="sdk_call",
        level="info",
        source=f"agent:{agent_id}",
        target_agent_id=None,
        attributes={"fn": marker, "duration": 0.1},
    )


def _send_event(actor_id: int, target_id: int) -> Event:
    return prepare_event_log(
        event_type="send_message",
        agent_id=target_id,
        source=f"agent:{actor_id}",
        target_agent_id=actor_id,
        payload={"content": "event capture"},
    )


def _open(lease: dict[str, Any], agent_id: int, key: str) -> LocalParticipant:
    participant = LocalParticipant(
        str(lease["id"]), agent_id, lease["session_id"], key, Database.from_settings()
    )
    assert open_local_participant(
        Database.from_settings(), participant.lease_id, agent_id=agent_id, source_key=key
    )
    return participant


def _state(db_conn: psycopg.Connection[Any], participant: LocalParticipant) -> tuple[Any, ...]:
    row = db_conn.execute(
        "SELECT state FROM agent_impersonation_event_participants "
        "WHERE lease_id=%s AND source_key=%s",
        (participant.lease_id, participant.source_key),
    ).fetchone()
    assert row is not None
    return row


def _rows(db_conn: psycopg.Connection[Any], lease_id: object, source_key: str) -> int:
    row = db_conn.execute(
        "SELECT count(*) FROM agent_impersonation_entries WHERE lease_id=%s AND source_key=%s",
        (lease_id, source_key),
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_central_events_belong_to_the_asserted_actor_not_the_recipient(
    db_conn: psycopg.Connection[Any], owner: RuntimeIncarnation, lease: dict[str, Any]
) -> None:
    recipient = _owner(db_conn)
    recipient_lease = history_cases.start(recipient)
    tagged = record_central_event(db_conn, _send_event(owner.agent_id, recipient.agent_id))
    assert tagged.attributes["impersonation_session"] == f"{owner.agent_id}:0"
    assert _rows(db_conn, lease["id"], capture.CENTRAL_SOURCE) == 1
    assert _rows(db_conn, recipient_lease["id"], capture.CENTRAL_SOURCE) == 0

    for source in ("user", "system", "external_client:codex", "agent:nope"):
        untagged = record_central_event(
            db_conn,
            prepare_event_log(
                event_type="send_message",
                agent_id=recipient.agent_id,
                source=source,
                payload={"content": "not a borrowed actor"},
            ),
        )
        assert "impersonation_session" not in untagged.attributes
    assert _rows(db_conn, lease["id"], capture.CENTRAL_SOURCE) == 1


def test_a_label_the_agent_sets_while_borrowed_lands_in_its_lease_log(
    db_conn: psycopg.Connection[Any], owner: RuntimeIncarnation, lease: dict[str, Any]
) -> None:
    from fastapi.testclient import TestClient

    from gateway.app import app

    with TestClient(app) as client:
        by_agent = client.patch(
            f"/api/agents/{owner.agent_id}", json={"label": "borrowed", "source": "self"}
        )
        by_operator = client.patch(f"/api/agents/{owner.agent_id}", json={"label": "by operator"})
    assert (by_agent.status_code, by_operator.status_code) == (204, 204)

    assert _rows(db_conn, lease["id"], capture.CENTRAL_SOURCE) == 1
    audited = db_conn.execute(
        "SELECT source, attributes FROM audit_events "
        "WHERE agent_id=%s AND event_name='label_change' ORDER BY id",
        (owner.agent_id,),
    ).fetchall()
    assert [source for source, _ in audited] == [f"agent:{owner.agent_id}", "user"]
    assert audited[0][1]["impersonation_session"] == f"{owner.agent_id}:0"
    assert "impersonation_session" not in audited[1][1]


def test_a_service_owned_central_event_is_recorded_in_both_logs_before_it_is_emitted(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    recipient = _owner(db_conn)
    seen_at_emit: list[tuple[int, int]] = []

    def emit(event: Event) -> None:
        recorded = db_conn.execute(
            "SELECT count(*) FROM audit_events WHERE event_uid=%s",
            (audit_event_uid(event),),
        ).fetchone()
        db_conn.commit()
        assert recorded is not None
        seen_at_emit.append((int(recorded[0]), _rows(db_conn, lease["id"], capture.CENTRAL_SOURCE)))

    monkeypatch.setattr(telemetry, "emit_prepared", emit)

    emit_recorded_central_event(database, _send_event(owner.agent_id, recipient.agent_id))

    assert seen_at_emit == [(1, 1)]


def test_manual_leases_keep_no_event_log_and_protocol_less_leases_stay_legacy(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    database: Database,
    event_bus: EventBus,
) -> None:
    manual = leases.request(
        database,
        event_bus,
        owner.agent_id,
        caller=CallerIdentity(kind="external_agent", subject="manual-capture"),
        relay_provider="codex",
        relay_thread_id="manual-capture-thread",
        process_metadata=recorded_tree(),
        automatic=False,
    )
    assert manual["id"] is not None
    row = db_conn.execute(
        "SELECT event_delivery_protocol_version FROM agent_impersonations WHERE id=%s",
        (manual["id"],),
    ).fetchone()
    assert row == (None,)
    document = history.build_document(
        history.resolve(database, owner.agent_id, manual["session_id"]),
        history.entries(str(manual["id"]), db_conn),
    )
    assert document["statistics"]["event_delivery"]["pending_reason"] == "manual"


def test_a_held_sdk_call_keeps_its_admission_and_seals_its_source_when_it_drains(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
) -> None:
    participant = _open(lease, owner.agent_id, "held-call")
    bind_local_participant(participant)
    try:
        with capture.admitted_local_sdk_call():
            # Closing while the admitted call is in flight cannot finish until it releases.
            assert not close_local_participant_admission(participant, timeout=0.01)
            assert capture.local_sdk_call_was_admitted()
            capture_local_event(_sdk_event(owner.agent_id, "held"))
            assert _state(db_conn, participant) == ("open",)
        # The last admitted call sealed the drained source on release.
        assert _state(db_conn, participant) == ("sealed",)
    finally:
        unbind_local_participant(participant)
    assert _rows(db_conn, lease["id"], "held-call") == 1


def test_equal_participant_closes_and_unbinds_the_existing_gate(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
) -> None:
    participant = _open(lease, owner.agent_id, "equal-participant")
    equivalent = dataclasses.replace(participant, db=Database.from_settings())
    assert equivalent == participant and equivalent is not participant
    bind_local_participant(participant)
    try:
        with capture.admitted_local_sdk_call():
            assert not close_local_participant_admission(equivalent, timeout=0)
            unbind_local_participant(equivalent)
            capture_local_event(_sdk_event(owner.agent_id, "equal-held"))
        assert _state(db_conn, participant) == ("sealed",)
        with capture.admitted_local_sdk_call():
            assert not capture.local_sdk_call_was_admitted()
    finally:
        unbind_local_participant(participant)
    assert _rows(db_conn, lease["id"], participant.source_key) == 1


def test_rebinding_does_not_redirect_an_unbound_held_admission(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
) -> None:
    previous = _open(lease, owner.agent_id, "previous-gate")
    current = _open(lease, owner.agent_id, "current-gate")
    bind_local_participant(previous)
    try:
        with capture.admitted_local_sdk_call():
            assert not close_local_participant_admission(previous, timeout=0)
            unbind_local_participant(previous)
            bind_local_participant(current)
            capture_local_event(_sdk_event(owner.agent_id, "previous-held"))
        assert _state(db_conn, previous) == ("sealed",)
        assert _state(db_conn, current) == ("open",)
        with capture.admitted_local_sdk_call():
            assert capture.local_sdk_call_was_admitted()
            capture_local_event(_sdk_event(owner.agent_id, "current-call"))
        assert close_local_participant_admission(current, timeout=0)
        seal_local_participant(current)
    finally:
        unbind_local_participant(previous)
        unbind_local_participant(current)
    assert _rows(db_conn, lease["id"], previous.source_key) == 1
    assert _rows(db_conn, lease["id"], current.source_key) == 1


def test_a_direct_audit_event_after_close_refuses_and_fails_the_source(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    database: Database,
    event_bus: EventBus,
) -> None:
    participant = _open(lease, owner.agent_id, "closed-direct")
    bind_local_participant(participant)
    try:
        assert close_local_participant_admission(participant, timeout=0)
        with pytest.raises(RuntimeError, match="Impersonation event capture is closed"):
            capture_local_event(_send_event(owner.agent_id, owner.agent_id))
        assert _state(db_conn, participant) == ("failed",)
        with pytest.raises(RuntimeError, match="Failed local impersonation event receipt"):
            seal_local_participant(participant)
        with pytest.raises(leases.ImpersonationError, match="Cannot release until every"):
            leases.release(
                database,
                event_bus,
                participant.lease_id,
                attested_caller(lease),
                "Direct audit refused",
            )
    finally:
        unbind_local_participant(participant)
    assert history.resolve(database, owner.agent_id, 0)["events_completed_at"] is None
    assert emit_incomplete_event_logs(db_conn) == 1  # the failed source keeps the signal firing


def test_a_transient_failure_stays_sticky_until_the_failed_source_persists(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    """A failed writer cannot turn a lost event into a seal."""
    participant = _open(lease, owner.agent_id, "sticky")
    bind_local_participant(participant)
    original_lock = capture.locked_receipt_state
    locks = 0

    def lock_with_one_outage(conn: psycopg.Connection[Any], lease_id: str, key: str) -> str | None:
        nonlocal locks
        locks += 1
        # Call 1 is the capture write itself; call 2 is the first failure-persist attempt.
        if locks == 2:
            raise psycopg.OperationalError("temporary writer outage")
        return original_lock(conn, lease_id, key)

    def failed_write(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("write failed")

    monkeypatch.setattr(capture, "append_source_event", failed_write)
    monkeypatch.setattr(capture, "locked_receipt_state", lock_with_one_outage)
    try:
        capture_local_event(_sdk_event(owner.agent_id, "lost-during-outage"))
        assert _state(db_conn, participant) == ("open",)
        with pytest.raises(RuntimeError, match="Failed local impersonation event receipt"):
            seal_local_participant(participant)
    finally:
        unbind_local_participant(participant)
    assert _state(db_conn, participant) == ("failed",)
    assert pending_reason(history.resolve(database, owner.agent_id, 0)) == "capture_failed"


@pytest.fixture
def restricted_source(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[LocalParticipant]:
    participant = _open(lease, owner.agent_id, "restricted-runner")
    owner_url = settings.data_plane.db_url
    runner_url = grant_runner_login(
        owner_url,
        owner="ava_citest",
        login=_RUNNER_LOGIN,
        password="test-runner-password",  # noqa: S106 -- throwaway role credential
    )
    db_conn.execute(
        sql.SQL(
            "REVOKE EXECUTE ON FUNCTION public.lock_impersonation_event_participant(UUID,TEXT) "
            "FROM {}"
        ).format(sql.Identifier(_RUNNER_GROUP))
    )
    db_conn.commit()
    with psycopg.connect(runner_url) as conn:
        assert conn.execute("SELECT current_user").fetchone() == (_RUNNER_LOGIN,)
        assert conn.execute(
            "SELECT has_table_privilege(current_user, "
            "'agent_impersonation_event_participants', 'UPDATE')"
        ).fetchone() == (False,)
        assert conn.execute(
            "SELECT has_column_privilege(current_user, 'agent_impersonations', "
            "'events_completed_at', 'UPDATE')"
        ).fetchone() == (False,)
    monkeypatch.setattr(settings.data_plane, "db_url", runner_url)
    participant = dataclasses.replace(participant, db=Database.from_settings())
    try:
        yield participant
    finally:
        monkeypatch.setattr(settings.data_plane, "db_url", owner_url)
        db_conn.rollback()
        grant_event_log_runner_access(db_conn, _RUNNER_GROUP)
        db_conn.commit()


def _restore_door(db_conn: psycopg.Connection[Any]) -> None:
    grant_event_log_runner_access(db_conn, _RUNNER_GROUP)
    db_conn.commit()


def test_a_runner_records_and_seals_only_through_the_narrow_lock(
    db_conn: psycopg.Connection[Any], restricted_source: LocalParticipant
) -> None:
    participant = restricted_source
    event = _sdk_event(participant.agent_id, "runner-capture")
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="lock_impersonation"):
        seal_local_participant(participant)
    _restore_door(db_conn)
    bind_local_participant(participant)
    try:
        capture_local_event(event)
    finally:
        unbind_local_participant(participant)
    seal_local_participant(participant)
    assert _rows(db_conn, participant.lease_id, participant.source_key) == 1
    assert _state(db_conn, participant) == ("sealed",)


def test_a_runner_failure_is_recorded_once_the_narrow_lock_is_available(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    restricted_source: LocalParticipant,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    participant = restricted_source

    def failed_write(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("write failed")

    monkeypatch.setattr(capture, "append_source_event", failed_write)
    bind_local_participant(participant)
    try:
        # Without the lock the failure cannot be persisted, so the source stays open ...
        capture_local_event(_sdk_event(owner.agent_id, "unrecordable"))
        assert _state(db_conn, participant) == ("open",)
        _restore_door(db_conn)
        # ... and the retained pending failure is recorded as soon as it can be.
        with pytest.raises(RuntimeError, match="Failed local impersonation event receipt"):
            seal_local_participant(participant)
    finally:
        unbind_local_participant(participant)
    assert db_conn.execute(
        "SELECT state,failure_reason FROM agent_impersonation_event_participants "
        "WHERE lease_id=%s AND source_key=%s",
        (participant.lease_id, participant.source_key),
    ).fetchone() == ("failed", "capture_failed")
    assert pending_reason(history.resolve(database, owner.agent_id, 0)) == "capture_failed"


def test_a_runner_completes_a_log_whose_source_seals_after_the_lease_ended(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    restricted_source: LocalParticipant,
    database: Database,
    event_bus: EventBus,
) -> None:
    participant = restricted_source
    _restore_door(db_conn)
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' "
        "WHERE id=%s",
        (participant.lease_id,),
    )
    db_conn.commit()
    assert (
        leases.get(database, event_bus, participant.lease_id, attested_caller(lease))["status"]
        == "expired"
    )
    assert history.resolve(database, owner.agent_id, 0)["events_completed_at"] is None
    seal_local_participant(participant)
    assert history.resolve(database, owner.agent_id, 0)["events_completed_at"] is not None


@pytest.mark.parametrize("body_failed", [False, True])
@pytest.mark.parametrize("async_call", [False, True])
async def test_sdk_emit_failure_marks_original_receipt_after_rebind(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    body_failed: bool,
    async_call: bool,
) -> None:
    from base.agents.sdk import call_policy
    from base.agents.sdk import telemetry as sdk_usage
    from base.agents.sdk.tally import SdkCallTally

    previous = _open(lease, owner.agent_id, "emit-failed-original")
    current = _open(lease, owner.agent_id, "emit-new-receipt")
    primary = ValueError("body failed after commit")
    cause = LookupError("body cause")
    tally = SdkCallTally()
    monkeypatch.setattr(call_policy, "policy", call_policy.SamplingPolicy)
    db_conn.execute("CREATE TEMP TABLE sdk_committed_effects(marker TEXT)")
    db_conn.commit()
    bind_local_participant(previous)

    def body() -> None:
        db_conn.execute("INSERT INTO sdk_committed_effects VALUES('committed')")
        db_conn.commit()
        assert not close_local_participant_admission(previous, timeout=0)
        unbind_local_participant(previous)
        bind_local_participant(current)
        if body_failed:
            raise primary from cause

    async def async_body() -> None:
        body()

    try:
        # Real emitter argument validation fails before the local capture seam.
        expected = ValueError if body_failed else TypeError
        with pytest.raises(expected) as raised:
            if async_call:
                await sdk_usage.run_metered_async(
                    "files.write",
                    async_body,
                    (),
                    {},
                    identity={"unexpected_identity": owner.agent_id},
                    tally=tally,
                )
            else:
                sdk_usage.run_metered(
                    "files.write",
                    body,
                    (),
                    {},
                    identity={"unexpected_identity": owner.agent_id},
                    tally=tally,
                )
        if body_failed:
            assert raised.value is primary and primary.__cause__ is cause
            assert any("unexpected_identity" in note for note in primary.__notes__)
        assert _state(db_conn, previous) == ("failed",)
        assert _state(db_conn, current) == ("open",)
        assert _rows(db_conn, lease["id"], previous.source_key) == 0
        assert _rows(db_conn, lease["id"], current.source_key) == 0
        assert db_conn.execute("SELECT count(*) FROM sdk_committed_effects").fetchone() == (1,)
        assert tally.snapshot() == {"files.write": 1}
    finally:
        unbind_local_participant(previous)
        unbind_local_participant(current)
        close_local_participant_admission(current, timeout=0)
        seal_local_participant(current)
