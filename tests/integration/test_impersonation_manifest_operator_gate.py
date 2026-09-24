"""Operator-gate regression contracts for event-manifest certification."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event as ThreadEvent
from threading import Thread
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from cli.commands import _release_services as release_services
from ops.spec import ServiceSpec
from shared import impersonation as leases
from shared import impersonation_history as history
from shared.agents.impersonation_manifest import (
    LocalParticipant,
    bind_local_participant,
    capture_local_event,
    certify,
    open_local_participant,
    seal_local_participant,
    stage_central_expected_event,
    unbind_local_participant,
)
from shared.audit_events import prepare_event_log
from shared.config import settings
from shared.db import create_agent
from shared.env_registry import MANIFEST_CERTIFICATION_SECRET_ENV
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


def test_finalizer_proof_is_private_in_root_tree_and_release_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The agent-host can receive the proof without exposing it in a launch receipt."""
    from services.ava_root.supervisor import _unit_env

    monkeypatch.setenv(MANIFEST_CERTIFICATION_SECRET_ENV, "host-finalizer-proof")
    assert _unit_env("agent-host")[MANIFEST_CERTIFICATION_SECRET_ENV] == "host-finalizer-proof"
    assert MANIFEST_CERTIFICATION_SECRET_ENV not in _unit_env("gateway")

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
    from ava import _boot, _sdk_metering
    from ava.external import Attachment

    participant = LocalParticipant(str(v1_lease["id"]), owner.agent_id, 0, "held-sdk-finally")
    assert open_local_participant(
        participant.lease_id, agent_id=owner.agent_id, source_key=participant.source_key
    )
    bind_local_participant(participant)
    monkeypatch.setattr(settings.general, "impersonation_event_manifest_seal_wait_seconds", 0.01)
    monkeypatch.setattr(_boot, "_external_agent_id", owner.agent_id)
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
        _now: datetime,
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
    from shared.impersonation_events import consume_events

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
