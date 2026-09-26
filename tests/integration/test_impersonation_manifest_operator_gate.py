"""Operator-gate regression contracts for event-manifest certification."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event as ThreadEvent
from threading import Thread
from typing import Any
from uuid import uuid4

import httpx
import psycopg
import pytest

from ava import _impersonation_events as reader
from cli.commands import _release_services as release_services
from ops.spec import ServiceSpec
from shared.agents import impersonation as leases
from shared.agents.impersonation import impersonation_history as history
from shared.agents.impersonation_manifest import (
    LocalParticipant,
    alert_if_participant_still_open,
    bind_local_participant,
    capture_local_event,
    certify,
    close_local_participant_admission,
    monitor_manifest_health,
    open_local_participant,
    seal_local_participant,
    stage_central_expected_event,
    unbind_local_participant,
)
from shared.alerts import upsert_alert
from shared.audit_events import prepare_event_log
from shared.config import settings
from shared.db import create_agent
from shared.env_registry import MANIFEST_CERTIFICATION_SECRET_ENV
from shared.loki_index_labels import EVENT_STREAM_RETENTION
from shared.machine import machine_name
from shared.runtime_incarnation import RuntimeIncarnation
from shared.runtime_release import VerifiedRelease
from shared.telemetry import Event
from tests.impersonation_support import attested_caller
from tests.shared import test_impersonation_history as history_cases

_CERTIFICATION_SECRET = "test-manifest-certification-secret-000001"  # noqa: S105 -- test proof


@pytest.fixture
def owner(db_conn: psycopg.Connection[Any]) -> RuntimeIncarnation:
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
def v1_lease(owner: RuntimeIncarnation, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(settings.general, "impersonation_event_manifest_enabled", True)
    monkeypatch.setattr(
        settings.general,
        "impersonation_event_manifest_certification_secret",
        _CERTIFICATION_SECRET,
    )
    return history_cases.start(owner)


def test_slow_manifest_receipt_reuses_one_episode_from_detach_and_monitor(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    v1_lease: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings.general, "impersonation_event_manifest_seal_wait_seconds", 0)
    lease_id = str(v1_lease["id"])
    first = LocalParticipant(lease_id, owner.agent_id, 0, "slow-first")
    second = LocalParticipant(lease_id, owner.agent_id, 0, "slow-second")
    for participant in (first, second):
        assert open_local_participant(
            lease_id, agent_id=owner.agent_id, source_key=participant.source_key
        )
    oldest_row = db_conn.execute(
        "SELECT min(opened_at) FROM agent_impersonation_event_participants "
        "WHERE lease_id=%s AND state='open'",
        (lease_id,),
    ).fetchone()
    assert oldest_row is not None
    oldest = oldest_row[0]

    alert_if_participant_still_open(first)
    monitor_manifest_health(machine=machine_name())
    monitor_manifest_health(machine=machine_name())

    rows = db_conn.execute(
        "SELECT starts_at,status FROM alerts WHERE labels->>'lease_id'=%s "
        "AND alertname='ImpersonationManifestSealSlow' ORDER BY starts_at",
        (lease_id,),
    ).fetchall()
    assert rows == [(oldest, "unresolved")]


def test_slow_manifest_episode_resolves_after_seal_only_on_its_machine(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    v1_lease: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings.general, "impersonation_event_manifest_seal_wait_seconds", 0)
    lease_id = str(v1_lease["id"])
    participant = LocalParticipant(lease_id, owner.agent_id, 0, "slow-to-sealed")
    assert open_local_participant(
        lease_id, agent_id=owner.agent_id, source_key=participant.source_key
    )
    monitor_manifest_health(machine=machine_name())
    with db_conn.transaction():
        upsert_alert(
            db_conn,
            {
                "status": "firing",
                "labels": {
                    "alertname": "ImpersonationManifestSealSlow",
                    "severity": "warning",
                    "machine": "another-machine",
                    "lease_id": lease_id,
                },
                "annotations": {"sentinel": "other-machine"},
                "starts_at": datetime.now(UTC).isoformat(),
            },
            source="machine-probe",
        )
    seal_local_participant(participant)
    monitor_manifest_health(machine=machine_name())

    rows = db_conn.execute(
        "SELECT labels->>'machine',status,ends_at FROM alerts "
        "WHERE labels->>'lease_id'=%s AND alertname='ImpersonationManifestSealSlow'",
        (lease_id,),
    ).fetchall()
    assert len(rows) == 2
    by_machine = {machine: (status, ends_at) for machine, status, ends_at in rows}
    assert by_machine[machine_name()][0] == "resolved"
    assert by_machine[machine_name()][1] is not None
    assert by_machine["another-machine"] == ("unresolved", None)


def test_old_slow_manifest_episode_resolves_when_newer_open_receipt_takes_over(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    v1_lease: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings.general, "impersonation_event_manifest_seal_wait_seconds", 0)
    lease_id = str(v1_lease["id"])
    oldest = LocalParticipant(lease_id, owner.agent_id, 0, "superseded-oldest")
    newer = LocalParticipant(lease_id, owner.agent_id, 0, "superseding-newer")
    for participant in (oldest, newer):
        assert open_local_participant(
            lease_id, agent_id=owner.agent_id, source_key=participant.source_key
        )
    opened = dict(
        db_conn.execute(
            "SELECT source_key,opened_at FROM agent_impersonation_event_participants "
            "WHERE lease_id=%s",
            (lease_id,),
        ).fetchall()
    )
    monitor_manifest_health(machine=machine_name())
    seal_local_participant(oldest)
    monitor_manifest_health(machine=machine_name())

    rows = db_conn.execute(
        "SELECT starts_at,status,ends_at FROM alerts WHERE labels->>'lease_id'=%s "
        "AND alertname='ImpersonationManifestSealSlow' ORDER BY starts_at",
        (lease_id,),
    ).fetchall()
    assert len(rows) == 2
    assert rows[0][0] == opened[oldest.source_key]
    assert rows[0][1] == "resolved"
    assert rows[0][2] is not None
    assert rows[1] == (opened[newer.source_key], "unresolved", None)


def test_delivery_pending_episode_resolves_when_manifest_completes(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    v1_lease: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings.general, "impersonation_event_delivery_alert_age_seconds", 60)
    lease_id = str(v1_lease["id"])
    leases.release(lease_id, attested_caller(v1_lease), "Empty manifest")
    db_conn.execute(
        "UPDATE agent_impersonations SET ended_at=clock_timestamp()-interval '61 seconds' "
        "WHERE id=%s",
        (lease_id,),
    )
    db_conn.commit()
    ended_at = history.resolve(owner.agent_id, 0)["ended_at"]
    monitor_manifest_health(machine=machine_name())
    assert certify(lease_id)
    monitor_manifest_health(machine=machine_name())

    rows = db_conn.execute(
        "SELECT starts_at,status,ends_at FROM alerts WHERE labels->>'lease_id'=%s "
        "AND alertname='ImpersonationEventDeliveryPending'",
        (lease_id,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == ended_at + timedelta(seconds=60)
    assert rows[0][1] == "resolved"
    assert rows[0][2] is not None


def test_retention_loss_episode_starts_when_the_frozen_floor_ages_out(
    db_conn: psycopg.Connection[Any],
    v1_lease: dict[str, Any],
) -> None:
    lease_id = str(v1_lease["id"])
    leases.release(lease_id, attested_caller(v1_lease), "Empty manifest")
    db_conn.execute(
        "UPDATE agent_impersonations SET manifest_envelope_floor_at="
        "clock_timestamp()-interval '7 days' WHERE id=%s",
        (lease_id,),
    )
    db_conn.commit()
    floor_row = db_conn.execute(
        "SELECT manifest_envelope_floor_at FROM agent_impersonations WHERE id=%s",
        (lease_id,),
    ).fetchone()
    assert floor_row is not None
    floor = floor_row[0]
    monitor_manifest_health(machine=machine_name())
    monitor_manifest_health(machine=machine_name())

    rows = db_conn.execute(
        "SELECT starts_at,status FROM alerts WHERE labels->>'lease_id'=%s "
        "AND alertname='ImpersonationEventRetentionLoss'",
        (lease_id,),
    ).fetchall()
    assert rows == [(floor + EVENT_STREAM_RETENTION, "unresolved")]


def test_orphan_manifest_alert_resolves_from_stored_lease_absence(
    db_conn: psycopg.Connection[Any],
) -> None:
    orphan_id = str(uuid4())
    with db_conn.transaction():
        upsert_alert(
            db_conn,
            {
                "status": "firing",
                "labels": {
                    "alertname": "ImpersonationManifestCaptureFailed",
                    "severity": "warning",
                    "machine": machine_name(),
                    "lease_id": orphan_id,
                },
                "annotations": {"sentinel": "orphan"},
                "starts_at": datetime.now(UTC).isoformat(),
            },
            source="machine-probe",
        )
    monitor_manifest_health(machine=machine_name())
    row = db_conn.execute(
        "SELECT status,annotations,ends_at FROM alerts WHERE labels->>'lease_id'=%s",
        (orphan_id,),
    ).fetchone()
    assert row is not None
    assert row[:2] == ("resolved", {"sentinel": "orphan"})
    assert row[2] is not None


def _central_send_event(actor_id: int, target_id: int) -> Event:
    return prepare_event_log(
        event_type="send_message",
        agent_id=target_id,
        source=f"agent:{actor_id}",
        target_agent_id=actor_id,
        payload={"content": "manifest operator-gate contract"},
    )


def _eligible_sdk_event(agent_id: int, *, marker: str) -> Event:
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


def _supervisor_sdk_events(agent_id: int) -> list[dict[str, Any]]:
    """The untagged same-agent rows observed in the #4668 session-7 smoke."""
    return [
        {
            "id": 11986666203222120154,
            "ts": "2026-09-24T04:40:28.243000+00:00",
            "agent_id": agent_id,
            "source": f"agent:{agent_id}",
            "event_name": "sdk_call",
            "category": "telemetry",
            "attributes": {"fn": "ava.agents.get_status", "sample_rate": 1, "duration": 0.0},
        },
        {
            "id": 2134537340000307691,
            "ts": "2026-09-24T04:41:28.490000+00:00",
            "agent_id": agent_id,
            "source": f"agent:{agent_id}",
            "event_name": "sdk_call",
            "category": "telemetry",
            "attributes": {"fn": "ava.agents.get_status", "sample_rate": 1, "duration": 0.0},
        },
    ]


def test_v1_reader_excludes_same_agent_supervisor_sdk_events_from_receipt(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    v1_lease: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#4668: untagged supervisor calls never become the borrowed lease's SDK facts."""
    supervisor_events = _supervisor_sdk_events(owner.agent_id)
    reads: list[dict[str, Any]] = []

    def get(path: str, *, params: dict[str, Any]) -> httpx.Response:
        assert path == "/api/events"
        reads.append(params)
        return httpx.Response(
            200,
            request=httpx.Request("GET", "http://manifest.test/api/events"),
            json={
                "items": supervisor_events if params.get("event_name") == "sdk_call" else [],
                "meta": {"has_more": False},
            },
        )

    leases.release(str(v1_lease["id"]), attested_caller(v1_lease), "Executor finished")
    monkeypatch.setattr(reader, "_get", get)
    reader.consume_recorded_events(history.resolve(owner.agent_id, 0))

    assert all(request["impersonation_session"] == f"{owner.agent_id}:0" for request in reads)
    assert not [
        entry
        for entry in history.entries(str(v1_lease["id"]), db_conn)
        if entry["kind"] == "sdk_call"
    ]
    assert history.resolve(owner.agent_id, 0)["events_completed_at"] is not None


def test_v1_reader_keeps_tagged_expected_send_and_excludes_same_agent_audit(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    v1_lease: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tagged central send survives replay while an untagged audit peer does not."""
    target_id = create_agent(db_conn)
    tagged = stage_central_expected_event(
        db_conn,
        _central_send_event(owner.agent_id, target_id),
        origin_kind="lease-attribution-send",
        origin_id=1,
    )
    db_conn.commit()
    expected = db_conn.execute(
        "SELECT event_key,line_sha256,event_at FROM agent_impersonation_event_expected_items "
        "WHERE lease_id=%s",
        (v1_lease["id"],),
    ).fetchone()
    assert expected is not None
    event_key, digest, event_at = expected
    session_tag = f"{owner.agent_id}:0"
    assert tagged.attributes["impersonation_session"] == session_tag
    expected_send = {
        "id": event_key.removeprefix("event:"),
        "line_sha256": digest,
        "ts": event_at.isoformat(),
        "agent_id": target_id,
        "source": f"agent:{owner.agent_id}",
        "event_name": "send_message",
        "category": "audit",
        "attributes": {"impersonation_session": session_tag},
    }
    supervisor_audit = {
        **_supervisor_sdk_events(owner.agent_id)[0],
        "event_name": "agent_status",
        "category": "audit",
    }

    def get(path: str, *, params: dict[str, Any]) -> httpx.Response:
        assert path == "/api/events"
        items = [] if params.get("event_name") == "sdk_call" else [expected_send, supervisor_audit]
        return httpx.Response(
            200,
            request=httpx.Request("GET", "http://manifest.test/api/events"),
            json={"items": items, "meta": {"has_more": False}},
        )

    leases.release(str(v1_lease["id"]), attested_caller(v1_lease), "Sent an update")
    monkeypatch.setattr(reader, "_get", get)
    reader.consume_recorded_events(history.resolve(owner.agent_id, 0))

    consumed = [
        entry["payload"]["id"]
        for entry in history.entries(str(v1_lease["id"]), db_conn)
        if entry["kind"] == "api_event"
    ]
    assert consumed == [expected_send["id"]]
    assert history.resolve(owner.agent_id, 0)["events_completed_at"] is not None


def test_finalizer_proof_is_private_in_root_tree_and_release_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The agent-host can receive the proof without exposing it in a launch receipt."""
    from services.ava_root import supervisor

    monkeypatch.setattr(
        supervisor,
        "manifest_certification_secret_env",
        lambda: {
            MANIFEST_CERTIFICATION_SECRET_ENV: "host-finalizer-proof",
            "AVA_MANIFEST_CERTIFICATION_FINALIZER": "1",
        },
    )
    agent_host_env = supervisor._unit_env("agent-host")
    assert agent_host_env[MANIFEST_CERTIFICATION_SECRET_ENV] == "host-finalizer-proof"
    assert agent_host_env["AVA_MANIFEST_CERTIFICATION_FINALIZER"] == "1"
    assert MANIFEST_CERTIFICATION_SECRET_ENV not in supervisor._unit_env("gateway")

    root = tmp_path / "image"
    root.mkdir()
    executable = root / "otelcol"
    executable.write_bytes(b"not executed")
    image = VerifiedRelease("a" * 64, "b" * 64, root, executable, root)
    spec = ServiceSpec(
        session="otel-collector",
        cmd=str(executable),
        capabilities=frozenset({"gateway"}),
        requires_db=False,
        curl_url="http://127.0.0.1:4318/healthz",
    )

    def first_proof(_spec: ServiceSpec) -> dict[str, str]:
        return {MANIFEST_CERTIFICATION_SECRET_ENV: "first-proof"}

    monkeypatch.setattr(
        release_services,
        "_service_extra_env",
        first_proof,
    )
    first = release_services._command(spec, image)

    def second_proof(_spec: ServiceSpec) -> dict[str, str]:
        return {MANIFEST_CERTIFICATION_SECRET_ENV: "second-proof"}

    monkeypatch.setattr(
        release_services,
        "_service_extra_env",
        second_proof,
    )
    second = release_services._command(spec, image)
    assert first.environment[MANIFEST_CERTIFICATION_SECRET_ENV] == "first-proof"
    assert second.environment[MANIFEST_CERTIFICATION_SECRET_ENV] == "second-proof"
    assert first.identity.command_digest == second.identity.command_digest


def test_held_sdk_finally_is_admitted_before_close_and_seals_with_its_receipt(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    v1_lease: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real SDK metering wrapper retains its pre-close admission through ``finally``."""
    from ava import _sdk_metering, agent_identity
    from ava.external import Attachment

    participant = LocalParticipant(str(v1_lease["id"]), owner.agent_id, 0, "held-sdk-finally")
    assert open_local_participant(
        participant.lease_id, agent_id=owner.agent_id, source_key=participant.source_key
    )
    bind_local_participant(participant)
    monkeypatch.setattr(settings.general, "impersonation_event_manifest_seal_wait_seconds", 0.01)
    monkeypatch.setattr(agent_identity, "_external_agent_id", owner.agent_id)
    entered, finish = ThreadEvent(), ThreadEvent()

    def held_send() -> None:
        entered.set()
        assert finish.wait(timeout=2)

    # This is the production SDK recorder shape for ava.agents.send_message,
    # not a direct hand-built telemetry event.
    recorded_send = _sdk_metering._make_recorder(held_send, "agents.send_message")
    worker = Thread(target=recorded_send)
    worker.start()
    assert entered.wait(timeout=2)

    attachment = object.__new__(Attachment)
    attachment._manifest_participant = participant
    attachment._seal_manifest_participant()
    assert db_conn.execute(
        "SELECT state FROM agent_impersonation_event_participants WHERE lease_id=%s AND source_key=%s",
        (participant.lease_id, participant.source_key),
    ).fetchone() == ("open",)

    # Attachment.close() detaches immediately after its bounded wait. The
    # in-flight SDK admission retains the receipt and performs the delayed seal.
    unbind_local_participant(participant)
    finish.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert db_conn.execute(
        "SELECT state FROM agent_impersonation_event_participants WHERE lease_id=%s AND source_key=%s",
        (participant.lease_id, participant.source_key),
    ).fetchone() == ("sealed",)
    assert db_conn.execute(
        "SELECT event_kind FROM agent_impersonation_event_participant_items "
        "WHERE lease_id=%s AND source_key=%s",
        (participant.lease_id, participant.source_key),
    ).fetchone() == ("sdk_call",)


def test_closed_direct_audit_capture_refuses_and_vetoes_manifest_certification(
    db_conn: psycopg.Connection[Any], owner: RuntimeIncarnation, v1_lease: dict[str, Any]
) -> None:
    """A post-close direct audit event cannot escape untagged from a live attachment."""
    participant = LocalParticipant(str(v1_lease["id"]), owner.agent_id, 0, "closed-direct-audit")
    assert open_local_participant(
        participant.lease_id, agent_id=owner.agent_id, source_key=participant.source_key
    )
    bind_local_participant(participant)
    try:
        assert close_local_participant_admission(participant, timeout=0)
        with pytest.raises(RuntimeError, match="Impersonation event capture is closed"):
            capture_local_event(_central_send_event(owner.agent_id, owner.agent_id))
        assert db_conn.execute(
            "SELECT state FROM agent_impersonation_event_participants WHERE lease_id=%s "
            "AND source_key=%s",
            (participant.lease_id, participant.source_key),
        ).fetchone() == ("failed",)
        assert (
            db_conn.execute(
                "SELECT starts_at FROM alerts WHERE labels->>'lease_id'=%s "
                "AND alertname='ImpersonationManifestCaptureFailed'",
                (participant.lease_id,),
            ).fetchone()
            == db_conn.execute(
                "SELECT opened_at FROM agent_impersonation_event_participants "
                "WHERE lease_id=%s AND source_key=%s",
                (participant.lease_id, participant.source_key),
            ).fetchone()
        )
        with pytest.raises(
            RuntimeError, match="Failed local impersonation event receipt cannot seal"
        ):
            seal_local_participant(participant)
        with pytest.raises(leases.ImpersonationError, match="Cannot release until every"):
            leases.release(participant.lease_id, attested_caller(v1_lease), "Direct audit refused")
    finally:
        unbind_local_participant(participant)
    assert history.resolve(owner.agent_id, 0)["events_completed_at"] is None


def test_transient_capture_failure_stays_sticky_until_the_failed_receipt_persists(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    v1_lease: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither a failed writer nor an alert failure can turn a lost event into a seal."""
    import shared.agents.impersonation_manifest as manifest

    participant = LocalParticipant(str(v1_lease["id"]), owner.agent_id, 0, "sticky-capture-failure")
    assert open_local_participant(
        participant.lease_id, agent_id=owner.agent_id, source_key=participant.source_key
    )
    bind_local_participant(participant)
    original_persist = manifest._persist_capture_failure
    attempts = 0

    def transient_persist(item: LocalParticipant) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise psycopg.OperationalError("temporary manifest writer outage")
        original_persist(item)

    def failed_capture_write(_participant: LocalParticipant, _event: Event) -> None:
        raise OSError("cap")

    def failed_alert_write(
        _upsert_alert: Any,
        _conn: psycopg.Connection[Any],
        _lease: dict[str, Any],
        _alertname: str,
    ) -> None:
        raise OSError("alert down")

    monkeypatch.setattr(manifest, "_insert_local_item", failed_capture_write)
    monkeypatch.setattr(manifest, "_persist_capture_failure", transient_persist)
    monkeypatch.setattr(manifest, "_upsert_manifest_alert", failed_alert_write)
    try:
        capture_local_event(_eligible_sdk_event(owner.agent_id, marker="lost-during-outage"))
        assert db_conn.execute(
            "SELECT state FROM agent_impersonation_event_participants WHERE lease_id=%s "
            "AND source_key=%s",
            (participant.lease_id, participant.source_key),
        ).fetchone() == ("open",)
        with pytest.raises(
            RuntimeError, match="Failed local impersonation event receipt cannot seal"
        ):
            seal_local_participant(participant)
    finally:
        unbind_local_participant(participant)
    assert attempts == 2
    assert db_conn.execute(
        "SELECT state FROM agent_impersonation_event_participants WHERE lease_id=%s AND source_key=%s",
        (participant.lease_id, participant.source_key),
    ).fetchone() == ("failed",)
    assert history.resolve(owner.agent_id, 0)["event_delivery_pending_reason"] == "capture_failed"


def test_truncated_retention_window_vetoes_certification_even_when_the_read_matches(
    db_conn: psycopg.Connection[Any], owner: RuntimeIncarnation, v1_lease: dict[str, Any]
) -> None:
    """A matching post-retention read cannot prove absence over the frozen envelope."""
    leases.release(str(v1_lease["id"]), attested_caller(v1_lease), "No emitted events")
    db_conn.execute(
        "UPDATE agent_impersonations SET manifest_envelope_floor_at=clock_timestamp()-interval '7 days' "
        "WHERE id=%s",
        (v1_lease["id"],),
    )
    db_conn.execute(
        "SELECT record_impersonation_event_retention_loss(%s,clock_timestamp()-interval '1 day')",
        (v1_lease["id"],),
    )
    db_conn.commit()
    with pytest.raises(psycopg.errors.RaiseException, match="vetoed by retention loss"):
        certify(str(v1_lease["id"]))
    assert history.resolve(owner.agent_id, 0)["events_completed_at"] is None


def test_certifier_rejects_a_durable_entry_with_the_right_key_but_wrong_digest(
    db_conn: psycopg.Connection[Any], owner: RuntimeIncarnation, v1_lease: dict[str, Any]
) -> None:
    """Final certification compares the durable payload digest, not key/kind alone."""
    event = _central_send_event(owner.agent_id, create_agent(db_conn))
    tagged = stage_central_expected_event(
        db_conn, event, origin_kind="wrong-durable-digest", origin_id=1
    )
    db_conn.commit()
    leases.release(str(v1_lease["id"]), attested_caller(v1_lease), "One delivered event")
    expected = db_conn.execute(
        "SELECT event_key,event_at FROM agent_impersonation_event_expected_items WHERE lease_id=%s",
        (v1_lease["id"],),
    ).fetchone()
    assert expected is not None
    key, event_at = expected
    from shared.agents.impersonation.impersonation_events import consume_events

    assert (
        consume_events(
            owner.agent_id,
            0,
            [
                {
                    "id": key.removeprefix("event:"),
                    "line_sha256": "0" * 64,
                    "ts": event_at.isoformat(),
                    "agent_id": owner.agent_id,
                    "event_name": tagged.event_name,
                    "category": "audit",
                    "source": f"agent:{owner.agent_id}",
                    "attributes": {"impersonation_session": f"{owner.agent_id}:0"},
                }
            ],
        )
        == 1
    )
    with pytest.raises(psycopg.errors.RaiseException, match="differs from durable consumed events"):
        certify(str(v1_lease["id"]))
    assert history.resolve(owner.agent_id, 0)["events_completed_at"] is None


def test_final_envelope_reader_splits_before_the_gateway_offset_ceiling(
    owner: RuntimeIncarnation,
    v1_lease: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A permitted manifest never asks the public reader for offset 11,000."""
    from ava import _impersonation_events as reader

    leases.release(str(v1_lease["id"]), attested_caller(v1_lease), "No emitted events")
    lease = history.resolve(owner.agent_id, 0)
    calls: list[dict[str, Any]] = []
    original_window = (
        lease["manifest_envelope_floor_at"].isoformat(),
        (
            lease["ended_at"]
            + timedelta(seconds=settings.general.impersonation_event_clock_skew_guard_seconds)
        ).isoformat(),
    )

    def get(_path: str, *, params: dict[str, Any]) -> Any:
        import httpx

        calls.append(params)
        original = (params["from"], params["to"])
        has_more = original == original_window and params["offset"] <= 10_000
        return httpx.Response(
            200,
            request=httpx.Request("GET", "http://manifest.test/api/events"),
            json={"items": [], "meta": {"has_more": has_more}},
        )

    monkeypatch.setattr(reader, "_get", get)
    expected, actual, conflicting = reader._indexed_manifest_items(lease)
    assert expected == actual == {}
    assert not conflicting
    assert max(call["offset"] for call in calls) == 10_000
    assert all(call["offset"] <= 10_000 for call in calls)
    assert any((call["from"], call["to"]) != original_window for call in calls)
