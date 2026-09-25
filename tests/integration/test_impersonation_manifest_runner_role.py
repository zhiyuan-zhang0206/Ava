"""Exercise manifest receipt operations as a restricted database runner."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

import shared.agents.impersonation_manifest as manifest
from shared.agents import impersonation as leases
from shared.agents.impersonation import impersonation_history as history
from shared.agents.impersonation_manifest import (
    LocalParticipant,
    bind_local_participant,
    capture_local_event,
    open_local_participant,
    seal_local_participant,
    unbind_local_participant,
)
from shared.agents.impersonation_manifest_grants import grant_manifest_runner_access
from shared.cluster import ensure_runner_role
from shared.config import settings
from shared.db import create_agent
from shared.machine import machine_name
from shared.runtime_incarnation import RuntimeIncarnation
from shared.telemetry import Event
from shared.url_secret import url_with_userinfo
from tests.impersonation_support import attested_caller
from tests.shared import test_impersonation_history as history_cases

_RECEIPT_RUNNER = "ava_runner"
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


def _eligible_sdk_event(agent_id: int) -> Event:
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
        attributes={"fn": "runner-capture", "duration": 0.1},
    )


def _central_send_event(agent_id: int) -> Event:
    from shared.audit_events import prepare_event_log

    return prepare_event_log(
        event_type="send_message",
        agent_id=agent_id,
        source=f"agent:{agent_id}",
        target_agent_id=agent_id,
        payload={"content": "runner capture after admission closure"},
    )


@pytest.fixture
def restricted_receipt(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    v1_lease: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[LocalParticipant]:
    participant = LocalParticipant(str(v1_lease["id"]), owner.agent_id, 0, "restricted-runner")
    assert open_local_participant(
        participant.lease_id, agent_id=owner.agent_id, source_key=participant.source_key
    )
    owner_url = settings.data_plane.db_url
    ensure_runner_role(
        "ava_citest",
        base_admin_url=owner_url.rsplit("/", 1)[0] + "/postgres",
        runner_password="test-runner-password",  # noqa: S106 -- throwaway role credential
    )
    db_conn.execute(
        sql.SQL(
            "REVOKE EXECUTE ON FUNCTION public.lock_impersonation_event_participant(UUID,TEXT) "
            "FROM {}"
        ).format(sql.Identifier(_RECEIPT_RUNNER))
    )
    db_conn.commit()
    runner_url = url_with_userinfo(owner_url, _RECEIPT_RUNNER, "test-runner-password")
    with psycopg.connect(runner_url) as conn:
        assert conn.execute("SELECT current_user").fetchone() == (_RECEIPT_RUNNER,)
        assert conn.execute(
            "SELECT has_table_privilege(current_user, "
            "'agent_impersonation_event_participants', 'UPDATE')"
        ).fetchone() == (False,)
    monkeypatch.setattr(settings.data_plane, "db_url", runner_url)
    try:
        yield participant
    finally:
        monkeypatch.setattr(settings.data_plane, "db_url", owner_url)
        db_conn.rollback()
        grant_manifest_runner_access(db_conn, _RECEIPT_RUNNER)
        db_conn.commit()


def _restore_receipt_door(db_conn: psycopg.Connection[Any]) -> None:
    grant_manifest_runner_access(db_conn, _RECEIPT_RUNNER)
    db_conn.commit()


def test_runner_receipt_capture_uses_narrow_lock(
    db_conn: psycopg.Connection[Any], restricted_receipt: LocalParticipant
) -> None:
    participant = restricted_receipt
    event = _eligible_sdk_event(participant.agent_id)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="lock_impersonation"):
        manifest._insert_local_item(participant, event)
    _restore_receipt_door(db_conn)
    bind_local_participant(participant)
    try:
        capture_local_event(event)
    finally:
        unbind_local_participant(participant)
    assert db_conn.execute(
        "SELECT count(*) FROM agent_impersonation_event_participant_items "
        "WHERE lease_id=%s AND source_key=%s",
        (participant.lease_id, participant.source_key),
    ).fetchone() == (1,)


def test_runner_receipt_failure_is_durable(
    db_conn: psycopg.Connection[Any], restricted_receipt: LocalParticipant
) -> None:
    participant = restricted_receipt
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="lock_impersonation"):
        manifest._persist_capture_failure(participant)
    _restore_receipt_door(db_conn)
    manifest._persist_capture_failure(participant)
    assert db_conn.execute(
        "SELECT state,failure_reason FROM agent_impersonation_event_participants "
        "WHERE lease_id=%s AND source_key=%s",
        (participant.lease_id, participant.source_key),
    ).fetchone() == ("failed", "capture_failed")
    assert (
        history.resolve(participant.agent_id, participant.session_id)[
            "event_delivery_pending_reason"
        ]
        == "capture_failed"
    )


def test_runner_receipt_seal_uses_narrow_lock(
    db_conn: psycopg.Connection[Any], restricted_receipt: LocalParticipant
) -> None:
    participant = restricted_receipt
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="lock_impersonation"):
        seal_local_participant(participant)
    _restore_receipt_door(db_conn)
    seal_local_participant(participant)
    assert db_conn.execute(
        "SELECT state FROM agent_impersonation_event_participants "
        "WHERE lease_id=%s AND source_key=%s",
        (participant.lease_id, participant.source_key),
    ).fetchone() == ("sealed",)


def test_runner_receipt_release_freezes_after_seal(
    db_conn: psycopg.Connection[Any],
    v1_lease: dict[str, Any],
    restricted_receipt: LocalParticipant,
) -> None:
    participant = restricted_receipt
    _restore_receipt_door(db_conn)
    second = LocalParticipant(participant.lease_id, participant.agent_id, 0, "restricted-second")
    assert open_local_participant(
        second.lease_id, agent_id=second.agent_id, source_key=second.source_key
    )
    seal_local_participant(participant)
    seal_local_participant(second)
    db_conn.execute(
        sql.SQL(
            "REVOKE EXECUTE ON FUNCTION public.lock_impersonation_event_participant(UUID,TEXT) "
            "FROM {}"
        ).format(sql.Identifier(_RECEIPT_RUNNER))
    )
    db_conn.commit()
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="lock_impersonation"):
        leases.release(participant.lease_id, attested_caller(v1_lease), "Restricted release")
    _restore_receipt_door(db_conn)
    leases.release(participant.lease_id, attested_caller(v1_lease), "Restricted release")
    assert (
        history.resolve(participant.agent_id, participant.session_id)["manifest_frozen_at"]
        is not None
    )
    assert db_conn.execute(
        "SELECT count(*) FROM agent_impersonation_event_participants "
        "WHERE lease_id=%s AND state='sealed'",
        (participant.lease_id,),
    ).fetchone() == (2,)


def test_runner_late_seal_after_admission_close_and_late_capture_refusal(
    db_conn: psycopg.Connection[Any], restricted_receipt: LocalParticipant
) -> None:
    participant = restricted_receipt
    db_conn.execute(
        "SELECT close_impersonation_event_manifest_admission(%s)", (participant.lease_id,)
    )
    db_conn.commit()
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="lock_impersonation"):
        seal_local_participant(participant)
    _restore_receipt_door(db_conn)
    seal_local_participant(participant)
    bind_local_participant(participant)
    try:
        assert manifest.close_local_participant_admission(participant, timeout=0)
        with pytest.raises(RuntimeError, match="Impersonation event capture is closed"):
            capture_local_event(_central_send_event(participant.agent_id))
    finally:
        unbind_local_participant(participant)
    assert db_conn.execute(
        "SELECT state FROM agent_impersonation_event_participants "
        "WHERE lease_id=%s AND source_key=%s",
        (participant.lease_id, participant.source_key),
    ).fetchone() == ("sealed",)
