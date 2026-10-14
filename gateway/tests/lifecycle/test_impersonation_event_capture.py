"""Event capture for log-native leases: gate, failures, central attribution, runner privileges."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from typing import Any

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
    capture_local_event,
    close_local_participant_admission,
    emit_recorded_central_event,
    record_central_event,
    seal_local_participant,
)
from base.agents.impersonation.status import pending_reason
from base.agents.impersonation.tests import test_history as history_cases
from base.agents.impersonation.tests.impersonation_support import attested_caller, recorded_tree
from base.agents.sdk import call_policy
from base.agents.sdk.tally import SdkCallTally
from base.agents.tests.capture_inputs import (
    capture_rows,
    capture_state,
    create_capture_owner,
    open_capture,
    sdk_event,
)
from base.agents.tests.capture_inputs import lease as lease
from base.agents.tests.capture_inputs import owner as owner
from base.api_contracts.caller_identity import CallerIdentity
from base.cluster.authority.event_grants import grant_event_log_runner_access
from base.cluster.authority.tests.runner_logins import grant_runner_login
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.config.tests.config_authority import config_authority as config_authority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.telemetry import Event
from base.telemetry.audit_events import audit_event_uid, prepare_event_log

# The capability group the grants target, and the generation-shaped login that
# inherits it (the only identity that logs in).
_RUNNER_GROUP = "ava_runner"
_RUNNER_LOGIN = "ava_g0_runner"


def _send_event(actor_id: int, target_id: int) -> Event:
    return prepare_event_log(
        event_type="send_message",
        agent_id=target_id,
        source=f"agent:{actor_id}",
        target_agent_id=actor_id,
        payload={"content": "event capture"},
    )


def test_central_events_belong_to_the_asserted_actor_not_the_recipient(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    local_machine = machine_identity.name()
    recipient = create_capture_owner(db_conn, local_machine=local_machine)
    recipient_lease = history_cases.start(
        recipient,
        authority=config_authority,
        database_gate=database_gate,
        local_machine=local_machine,
    )
    tagged = record_central_event(db_conn, _send_event(owner.agent_id, recipient.agent_id))
    assert tagged.attributes["impersonation_session"] == f"{owner.agent_id}:0"
    assert capture_rows(db_conn, lease["id"], capture.CENTRAL_SOURCE) == 1
    assert capture_rows(db_conn, recipient_lease["id"], capture.CENTRAL_SOURCE) == 0

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
    assert capture_rows(db_conn, lease["id"], capture.CENTRAL_SOURCE) == 1


def test_a_label_the_agent_sets_while_borrowed_lands_in_its_lease_log(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    machine_identity: MachineIdentity,
) -> None:
    from gateway.app import app
    from tests.fixtures.gateway_config import gateway_test_client

    with gateway_test_client(app, machine=machine_identity) as client:
        by_agent = client.patch(
            f"/api/agents/{owner.agent_id}", json={"label": "borrowed", "source": "self"}
        )
        by_operator = client.patch(f"/api/agents/{owner.agent_id}", json={"label": "by operator"})
    assert (by_agent.status_code, by_operator.status_code) == (204, 204)

    assert capture_rows(db_conn, lease["id"], capture.CENTRAL_SOURCE) == 1
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
    *,
    machine_identity: MachineIdentity,
) -> None:
    local_machine = machine_identity.name()
    recipient = create_capture_owner(db_conn, local_machine=local_machine)
    seen_at_emit: list[tuple[int, int]] = []

    def emit(event: Event) -> None:
        recorded = db_conn.execute(
            "SELECT count(*) FROM audit_events WHERE event_uid=%s",
            (audit_event_uid(event),),
        ).fetchone()
        db_conn.commit()
        assert recorded is not None
        seen_at_emit.append(
            (int(recorded[0]), capture_rows(db_conn, lease["id"], capture.CENTRAL_SOURCE))
        )

    monkeypatch.setattr(telemetry, "emit_prepared", emit)

    emit_recorded_central_event(database, _send_event(owner.agent_id, recipient.agent_id))

    assert seen_at_emit == [(1, 1)]


def test_manual_leases_keep_no_event_log_and_protocol_less_leases_stay_legacy(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    local_machine = machine_identity.name()
    manual = leases.request(
        database,
        event_bus,
        owner.agent_id,
        caller=CallerIdentity(kind="external_agent", subject="manual-capture"),
        relay_provider="codex",
        relay_thread_id="manual-capture-thread",
        process_metadata=recorded_tree(),
        automatic=False,
        authority=config_authority,
        local_machine=local_machine,
    )
    assert manual["id"] is not None
    row = db_conn.execute(
        "SELECT event_delivery_protocol_version FROM agent_impersonations WHERE id=%s",
        (manual["id"],),
    ).fetchone()
    assert row == (None,)
    document = history.build_document(
        history.resolve(
            database, owner.agent_id, manual["session_id"], local_machine=local_machine
        ),
        history.entries(str(manual["id"]), db_conn),
    )
    assert document["statistics"]["event_delivery"]["pending_reason"] == "manual"


def test_a_held_sdk_call_keeps_its_admission_and_seals_its_source_when_it_drains(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    database_gate: ProcessDbGate,
) -> None:
    participant = open_capture(lease, owner.agent_id, "held-call", database_gate=database_gate)
    gate = capture.LocalCaptureGate(participant)
    with capture.admitted_local_sdk_call(gate.admit) as admission:
        # Closing while the admitted call is in flight cannot finish until it releases.
        assert not close_local_participant_admission(gate, timeout=0.01)
        with pytest.raises(RuntimeError, match="still has admitted SDK calls"):
            seal_local_participant(gate)
        assert admission is not None
        assert admission is not None
        admission.capture(sdk_event(owner.agent_id, "held"))
        assert capture_state(db_conn, participant) == ("open",)
    # The last admitted call sealed the drained source on release.
    assert capture_state(db_conn, participant) == ("sealed",)
    assert capture_rows(db_conn, lease["id"], "held-call") == 1


def test_receipt_owner_retains_its_exact_gate_despite_equal_participant_values(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    database_gate: ProcessDbGate,
) -> None:
    participant = open_capture(
        lease, owner.agent_id, "equal-participant", database_gate=database_gate
    )
    equivalent = dataclasses.replace(participant, db=Database.from_settings(gate=database_gate))
    assert equivalent == participant and equivalent is not participant
    gate = capture.LocalCaptureGate(participant)
    with capture.admitted_local_sdk_call(gate.admit) as admission:
        assert not close_local_participant_admission(gate, timeout=0)
        assert admission is not None
        admission.capture(sdk_event(owner.agent_id, "equal-held"))
    assert capture_state(db_conn, participant) == ("sealed",)
    with capture.admitted_local_sdk_call(gate.admit) as admission:
        assert admission is None
    assert capture_rows(db_conn, lease["id"], participant.source_key) == 1


def test_rebinding_does_not_redirect_an_unbound_held_admission(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    database_gate: ProcessDbGate,
) -> None:
    previous = open_capture(lease, owner.agent_id, "previous-gate", database_gate=database_gate)
    current = open_capture(lease, owner.agent_id, "current-gate", database_gate=database_gate)
    previous_gate = capture.LocalCaptureGate(previous)
    current_gate = capture.LocalCaptureGate(current)
    with capture.admitted_local_sdk_call(previous_gate.admit) as admission:
        assert not close_local_participant_admission(previous_gate, timeout=0)
        assert admission is not None
        admission.capture(sdk_event(owner.agent_id, "previous-held"))
    assert capture_state(db_conn, previous) == ("sealed",)
    assert capture_state(db_conn, current) == ("open",)
    with capture.admitted_local_sdk_call(current_gate.admit) as admission:
        assert admission is not None
        assert admission is not None
        admission.capture(sdk_event(owner.agent_id, "current-call"))
    assert close_local_participant_admission(current_gate, timeout=0)
    seal_local_participant(current_gate)
    assert capture_rows(db_conn, lease["id"], previous.source_key) == 1
    assert capture_rows(db_conn, lease["id"], current.source_key) == 1


def test_a_direct_audit_event_after_close_refuses_and_fails_the_source(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    database: Database,
    event_bus: EventBus,
    database_gate: ProcessDbGate,
    *,
    machine_identity: MachineIdentity,
) -> None:
    local_machine = machine_identity.name()
    participant = open_capture(lease, owner.agent_id, "closed-direct", database_gate=database_gate)
    gate = capture.LocalCaptureGate(participant)
    assert close_local_participant_admission(gate, timeout=0)
    with pytest.raises(RuntimeError, match="Impersonation event capture is closed"):
        capture_local_event(_send_event(owner.agent_id, owner.agent_id), gate=gate)
    assert capture_state(db_conn, participant) == ("failed",)
    with pytest.raises(RuntimeError, match="Failed local impersonation event receipt"):
        seal_local_participant(gate)
    with pytest.raises(leases.ImpersonationError, match="Cannot release until every"):
        leases.release(
            database,
            event_bus,
            participant.lease_id,
            attested_caller(lease),
            "Direct audit refused",
            local_machine=local_machine,
        )
    assert (
        history.resolve(database, owner.agent_id, 0, local_machine=local_machine)[
            "events_completed_at"
        ]
        is None
    )
    assert emit_incomplete_event_logs(db_conn) == 1  # the failed source keeps the signal firing


def test_a_transient_failure_stays_sticky_until_the_failed_source_persists(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    database_gate: ProcessDbGate,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """A failed writer cannot turn a lost event into a seal."""
    local_machine = machine_identity.name()
    participant = open_capture(lease, owner.agent_id, "sticky", database_gate=database_gate)
    gate = capture.LocalCaptureGate(participant)
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
    with pytest.raises(OSError, match="write failed"):
        capture_local_event(sdk_event(owner.agent_id, "lost-during-outage"), gate=gate)
    assert capture_state(db_conn, participant) == ("open",)
    with pytest.raises(RuntimeError, match="Failed local impersonation event receipt"):
        seal_local_participant(gate)
    assert capture_state(db_conn, participant) == ("failed",)
    assert (
        pending_reason(history.resolve(database, owner.agent_id, 0, local_machine=local_machine))
        == "capture_failed"
    )


@pytest.fixture
def restricted_source(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    database_gate: ProcessDbGate,
) -> Iterator[LocalParticipant]:
    participant = open_capture(
        lease, owner.agent_id, "restricted-runner", database_gate=database_gate
    )
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
    participant = dataclasses.replace(participant, db=Database.from_settings(gate=database_gate))
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
    gate = capture.LocalCaptureGate(participant)
    event = sdk_event(participant.agent_id, "runner-capture")
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="lock_impersonation"):
        seal_local_participant(gate)
    _restore_door(db_conn)
    gate = capture.LocalCaptureGate(participant)
    capture_local_event(event, gate=gate)
    seal_local_participant(gate)
    assert capture_rows(db_conn, participant.lease_id, participant.source_key) == 1
    assert capture_state(db_conn, participant) == ("sealed",)


def test_a_runner_failure_is_recorded_once_the_narrow_lock_is_available(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    restricted_source: LocalParticipant,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    *,
    machine_identity: MachineIdentity,
) -> None:
    local_machine = machine_identity.name()
    participant = restricted_source
    gate = capture.LocalCaptureGate(participant)

    def failed_write(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("write failed")

    monkeypatch.setattr(capture, "append_source_event", failed_write)
    gate = capture.LocalCaptureGate(participant)
    # Without the lock the failure cannot be persisted, so the source stays open ...
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        capture_local_event(sdk_event(owner.agent_id, "unrecordable"), gate=gate)
    assert capture_state(db_conn, participant) == ("open",)
    _restore_door(db_conn)
    # ... and the retained pending failure is recorded as soon as it can be.
    with pytest.raises(RuntimeError, match="Failed local impersonation event receipt"):
        seal_local_participant(gate)
    assert db_conn.execute(
        "SELECT state,failure_reason FROM agent_impersonation_event_participants "
        "WHERE lease_id=%s AND source_key=%s",
        (participant.lease_id, participant.source_key),
    ).fetchone() == ("failed", "capture_failed")
    assert (
        pending_reason(history.resolve(database, owner.agent_id, 0, local_machine=local_machine))
        == "capture_failed"
    )


def test_a_runner_completes_a_log_whose_source_seals_after_the_lease_ended(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    restricted_source: LocalParticipant,
    database: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
) -> None:
    local_machine = machine_identity.name()
    participant = restricted_source
    gate = capture.LocalCaptureGate(participant)
    _restore_door(db_conn)
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' "
        "WHERE id=%s",
        (participant.lease_id,),
    )
    db_conn.commit()
    assert (
        leases.get(
            database,
            event_bus,
            participant.lease_id,
            attested_caller(lease),
            local_machine=local_machine,
        )["status"]
        == "expired"
    )
    assert (
        history.resolve(database, owner.agent_id, 0, local_machine=local_machine)[
            "events_completed_at"
        ]
        is None
    )
    seal_local_participant(gate)
    assert (
        history.resolve(database, owner.agent_id, 0, local_machine=local_machine)[
            "events_completed_at"
        ]
        is not None
    )


@pytest.mark.parametrize("body_failed", [False, True])
@pytest.mark.parametrize("async_call", [False, True])
async def test_sdk_emit_failure_marks_original_receipt_after_rebind(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    body_failed: bool,
    async_call: bool,
    database_gate: ProcessDbGate,
) -> None:
    from base.agents.sdk import call_policy
    from base.agents.sdk import telemetry as sdk_usage

    previous = open_capture(
        lease, owner.agent_id, "emit-failed-original", database_gate=database_gate
    )
    current = open_capture(lease, owner.agent_id, "emit-new-receipt", database_gate=database_gate)
    primary = ValueError("body failed after commit")
    cause = LookupError("body cause")
    tally = SdkCallTally()

    def _policy_for_test(
        create_capture_owner: call_policy.SamplingPolicyOwner,
    ) -> call_policy.SamplingPolicy:
        return call_policy.SamplingPolicy()

    monkeypatch.setattr(call_policy, "policy", _policy_for_test)
    db_conn.execute("CREATE TEMP TABLE sdk_committed_effects(marker TEXT)")
    db_conn.commit()
    previous_gate = capture.LocalCaptureGate(previous)
    current_gate = capture.LocalCaptureGate(current)

    class Owner:
        def admit_sdk_call(self) -> capture.LocalCaptureAdmission | None:
            return previous_gate.admit()

        def capture_local_event(self, event: Event) -> Event:
            return capture_local_event(event, gate=previous_gate)

    def body() -> None:
        db_conn.execute("INSERT INTO sdk_committed_effects VALUES('committed')")
        db_conn.commit()
        assert not close_local_participant_admission(previous_gate, timeout=0)
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
                    capture_owner=Owner(),
                    sampling_owner=call_policy.SamplingPolicyOwner(),
                )
            else:
                sdk_usage.run_metered(
                    "files.write",
                    body,
                    (),
                    {},
                    identity={"unexpected_identity": owner.agent_id},
                    tally=tally,
                    capture_owner=Owner(),
                    sampling_owner=call_policy.SamplingPolicyOwner(),
                )
        if body_failed:
            assert raised.value is primary and primary.__cause__ is cause
            assert any("unexpected_identity" in note for note in primary.__notes__)
        assert capture_state(db_conn, previous) == ("failed",)
        assert capture_state(db_conn, current) == ("open",)
        assert capture_rows(db_conn, lease["id"], previous.source_key) == 0
        assert capture_rows(db_conn, lease["id"], current.source_key) == 0
        assert db_conn.execute("SELECT count(*) FROM sdk_committed_effects").fetchone() == (1,)
        assert tally.snapshot() == {"files.write": 1}
    finally:
        close_local_participant_admission(current_gate, timeout=0)
        seal_local_participant(current_gate)
