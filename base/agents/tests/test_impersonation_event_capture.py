"""Event capture for log-native leases: gate, failures, central attribution, runner privileges."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

import ava
from ava.sdk_surface import metering
from base import telemetry
from base.agents import impersonation as leases
from base.agents.context import AvaContext
from base.agents.context.identity import AgentIdentity
from base.agents.impersonation import history as history
from base.agents.impersonation import manifest as capture
from base.agents.impersonation.event_signals import emit_incomplete_event_logs
from base.agents.impersonation.manifest import (
    LocalParticipant,
    capture_local_event,
    close_local_participant_admission,
    emit_recorded_central_event,
    open_local_participant,
    pending_reason,
    record_central_event,
    seal_local_participant,
)
from base.agents.impersonation.tests import test_history as history_cases
from base.agents.messages.caller_identity import CallerIdentity
from base.agents.sdk import call_policy
from base.agents.sdk.tally import SdkCallTally
from base.cluster.authority.event_grants import grant_event_log_runner_access
from base.cluster.machine import machine_name
from base.config import settings
from base.config.service_read import ConfigAuthority
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
def lease(owner: RuntimeIncarnation, *, config_authority: ConfigAuthority) -> dict[str, Any]:
    return history_cases.start(owner, authority=config_authority)


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
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    *,
    config_authority: ConfigAuthority,
) -> None:
    recipient = _owner(db_conn)
    recipient_lease = history_cases.start(recipient, authority=config_authority)
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
    *,
    config_authority: ConfigAuthority,
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
        authority=config_authority,
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
    gate = capture.LocalCaptureGate(participant)
    with capture.admitted_local_sdk_call(gate.admit) as admission:
        # Closing while the admitted call is in flight cannot finish until it releases.
        assert not close_local_participant_admission(gate, timeout=0.01)
        with pytest.raises(RuntimeError, match="still has admitted SDK calls"):
            seal_local_participant(gate)
        assert admission is not None
        assert admission is not None
        admission.capture(_sdk_event(owner.agent_id, "held"))
        assert _state(db_conn, participant) == ("open",)
    # The last admitted call sealed the drained source on release.
    assert _state(db_conn, participant) == ("sealed",)
    assert _rows(db_conn, lease["id"], "held-call") == 1


def test_receipt_owner_retains_its_exact_gate_despite_equal_participant_values(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
) -> None:
    participant = _open(lease, owner.agent_id, "equal-participant")
    equivalent = dataclasses.replace(participant, db=Database.from_settings())
    assert equivalent == participant and equivalent is not participant
    gate = capture.LocalCaptureGate(participant)
    with capture.admitted_local_sdk_call(gate.admit) as admission:
        assert not close_local_participant_admission(gate, timeout=0)
        assert admission is not None
        admission.capture(_sdk_event(owner.agent_id, "equal-held"))
    assert _state(db_conn, participant) == ("sealed",)
    with capture.admitted_local_sdk_call(gate.admit) as admission:
        assert admission is None
    assert _rows(db_conn, lease["id"], participant.source_key) == 1


def test_rebinding_does_not_redirect_an_unbound_held_admission(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
) -> None:
    previous = _open(lease, owner.agent_id, "previous-gate")
    current = _open(lease, owner.agent_id, "current-gate")
    previous_gate = capture.LocalCaptureGate(previous)
    current_gate = capture.LocalCaptureGate(current)
    with capture.admitted_local_sdk_call(previous_gate.admit) as admission:
        assert not close_local_participant_admission(previous_gate, timeout=0)
        assert admission is not None
        admission.capture(_sdk_event(owner.agent_id, "previous-held"))
    assert _state(db_conn, previous) == ("sealed",)
    assert _state(db_conn, current) == ("open",)
    with capture.admitted_local_sdk_call(current_gate.admit) as admission:
        assert admission is not None
        assert admission is not None
        admission.capture(_sdk_event(owner.agent_id, "current-call"))
    assert close_local_participant_admission(current_gate, timeout=0)
    seal_local_participant(current_gate)
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
    gate = capture.LocalCaptureGate(participant)
    assert close_local_participant_admission(gate, timeout=0)
    with pytest.raises(RuntimeError, match="Impersonation event capture is closed"):
        capture_local_event(_send_event(owner.agent_id, owner.agent_id), gate=gate)
    assert _state(db_conn, participant) == ("failed",)
    with pytest.raises(RuntimeError, match="Failed local impersonation event receipt"):
        seal_local_participant(gate)
    with pytest.raises(leases.ImpersonationError, match="Cannot release until every"):
        leases.release(
            database,
            event_bus,
            participant.lease_id,
            attested_caller(lease),
            "Direct audit refused",
        )
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
        capture_local_event(_sdk_event(owner.agent_id, "lost-during-outage"), gate=gate)
    assert _state(db_conn, participant) == ("open",)
    with pytest.raises(RuntimeError, match="Failed local impersonation event receipt"):
        seal_local_participant(gate)
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
    gate = capture.LocalCaptureGate(participant)
    event = _sdk_event(participant.agent_id, "runner-capture")
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="lock_impersonation"):
        seal_local_participant(gate)
    _restore_door(db_conn)
    gate = capture.LocalCaptureGate(participant)
    capture_local_event(event, gate=gate)
    seal_local_participant(gate)
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
    gate = capture.LocalCaptureGate(participant)

    def failed_write(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("write failed")

    monkeypatch.setattr(capture, "append_source_event", failed_write)
    gate = capture.LocalCaptureGate(participant)
    # Without the lock the failure cannot be persisted, so the source stays open ...
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        capture_local_event(_sdk_event(owner.agent_id, "unrecordable"), gate=gate)
    assert _state(db_conn, participant) == ("open",)
    _restore_door(db_conn)
    # ... and the retained pending failure is recorded as soon as it can be.
    with pytest.raises(RuntimeError, match="Failed local impersonation event receipt"):
        seal_local_participant(gate)
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
    gate = capture.LocalCaptureGate(participant)
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
    seal_local_participant(gate)
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
        close_local_participant_admission(current_gate, timeout=0)
        seal_local_participant(current_gate)


class _CaptureOwner:
    def __init__(self, gate: capture.LocalCaptureGate) -> None:
        self.gate = gate

    def admit_sdk_call(self) -> capture.LocalCaptureAdmission | None:
        admission = self.gate.admit()
        if admission is None:
            raise RuntimeError("external attachment is closing")
        return admission

    def capture_local_event(self, event: Event) -> Event:
        return capture_local_event(event, gate=self.gate)


async def test_concurrent_calls_capture_the_original_gate_after_context_rebind(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real recorders retain separate admissions, despite detach and a new context."""
    previous = _open(lease, owner.agent_id, "concurrent-original")
    current = _open(lease, owner.agent_id, "concurrent-replacement")
    previous_gate = capture.LocalCaptureGate(previous)
    current_gate = capture.LocalCaptureGate(current)

    entered = asyncio.Event()
    release = asyncio.Event()
    started = 0

    async def held() -> None:
        nonlocal started
        started += 1
        if started == 2:
            entered.set()
        await release.wait()

    namespace = SimpleNamespace(held=held, __all_for_ava__=["held"])
    monkeypatch.setattr(ava, "receipt_probe", namespace, raising=False)
    monkeypatch.setattr(ava, "__all_for_ava__", [*ava.__all_for_ava__, "receipt_probe"])
    monkeypatch.setattr(call_policy, "policy", call_policy.SamplingPolicy)
    prior = getattr(ava, "context", None)
    tally = SdkCallTally()
    context = AvaContext(
        identity=AgentIdentity(owner.agent_id, True),
        sdk_calls=tally,
        sdk_capture=_CaptureOwner(previous_gate),
    )
    ava.bind_context(context)
    ledger = metering.install()
    calls = [asyncio.create_task(namespace.held()) for _ in range(2)]
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert previous_gate.in_flight == 2
        assert not close_local_participant_admission(previous_gate, timeout=0)
        ava.bind_context(replace(context, sdk_capture=_CaptureOwner(current_gate)))
        release.set()
        await asyncio.gather(*calls)
        assert previous_gate.in_flight == current_gate.in_flight == 0
        assert _state(db_conn, previous) == ("sealed",)
        assert _state(db_conn, current) == ("open",)
        assert _rows(db_conn, lease["id"], previous.source_key) == 2
        assert _rows(db_conn, lease["id"], current.source_key) == 0
        assert tally.snapshot() == {"receipt_probe.held": 2}
    finally:
        release.set()
        await asyncio.gather(*calls, return_exceptions=True)
        metering.uninstall(ledger)
        if prior is None:
            ava.unbind_context()
        else:
            ava.bind_context(prior)
        close_local_participant_admission(current_gate, timeout=0)
        seal_local_participant(current_gate)


def test_sdk_skill_read_captures_borrowed_audit_source_explicitly(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from base.agents.context.identity import ExternalLease

    participant = _open(lease, owner.agent_id, "explicit-skill-audit")
    gate = capture.LocalCaptureGate(participant)
    (tmp_path / "SKILL.md").write_text("# Capture test\n")
    skill = ava.skills.Skill(
        name="capture_test",
        description="test",
        path=str(tmp_path),
        namespace=(),
    )
    monkeypatch.setattr(ava.skills, "names", lambda: [skill])
    prior = getattr(ava, "context", None)
    borrowed = ExternalLease(
        agent_id=owner.agent_id,
        validate=lambda: owner.agent_id,
        config=lambda: None,
    )
    ava.bind_context(
        AvaContext(
            identity=AgentIdentity(None, True, lease=borrowed),
            sdk_capture=_CaptureOwner(gate),
        )
    )
    try:
        assert "# Capture test" in ava.skills.read("capture_test")
        rows = history.entries(participant.lease_id, db_conn)
        audits = [row for row in rows if row["kind"] == "api_event"]
        assert _rows(db_conn, lease["id"], participant.source_key) == 1
        assert len(audits) == 1
        event = audits[0]["payload"]
        assert event["event_name"] == "skill_invoked" and event["source"] == "self"
        assert event["attributes"]["skill"] == "capture_test"
        assert (
            event["attributes"]["impersonation_session"]
            == f"{owner.agent_id}:{lease['session_id']}"
        )
        assert close_local_participant_admission(gate, timeout=0)
        seal_local_participant(gate)
        assert _state(db_conn, participant) == ("sealed",)
    finally:
        if prior is None:
            ava.unbind_context()
        else:
            ava.bind_context(prior)


def test_known_capture_database_outage_retains_the_failed_receipt_recovery(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    participant = _open(lease, owner.agent_id, "known-capture-outage")
    gate = capture.LocalCaptureGate(participant)

    def unavailable(*_args: Any, **_kwargs: Any) -> None:
        raise psycopg.OperationalError("temporary connection loss")

    monkeypatch.setattr(capture, "append_source_event", unavailable)
    with capture.admitted_local_sdk_call(gate.admit) as admission:
        assert admission is not None
        tagged = admission.capture(_sdk_event(owner.agent_id, "known-outage"))
        assert tagged.attributes["impersonation_session"] == f"{owner.agent_id}:0"
        assert not close_local_participant_admission(gate, timeout=0)
    assert gate.in_flight == 0
    assert _state(db_conn, participant) == ("failed",)
    assert _rows(db_conn, lease["id"], participant.source_key) == 0
    assert (
        history.resolve(Database.from_settings(), owner.agent_id, 0)["events_completed_at"] is None
    )
