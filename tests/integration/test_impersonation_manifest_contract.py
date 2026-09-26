"""Protocol-v1 manifest regression contracts.

These cases hold the producer/consumer boundaries rather than treating an
empty event page or a successful telemetry flush as delivery proof.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql

from gateway.routers import alerts as alerts_router
from shared.agents import impersonation as leases
from shared.agents.impersonation import impersonation_history as history
from shared.agents.impersonation.impersonation_events import _validate_event
from shared.agents.impersonation_manifest import (
    LocalParticipant,
    alert_if_participant_still_open,
    bind_local_participant,
    capture_local_event,
    certify,
    monitor_manifest_health,
    open_local_participant,
    pending_reason,
    retention_loss_panel,
    seal_local_participant,
    stage_central_expected_event,
    unbind_local_participant,
)
from shared.audit_events import prepare_event_log
from shared.caller_identity import CallerIdentity
from shared.config import settings
from shared.db import create_agent
from shared.machine import machine_name
from shared.runtime_incarnation import RuntimeIncarnation
from shared.telemetry import Event
from tests.impersonation_support import attested_caller, recorded_tree
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


def start(owner: RuntimeIncarnation, *, active: bool = True) -> dict[str, Any]:
    return history_cases.start(owner, active=active)


@pytest.fixture
def v1_lease(owner: RuntimeIncarnation, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(settings.general, "impersonation_event_manifest_enabled", True)
    monkeypatch.setattr(
        settings.general,
        "impersonation_event_manifest_certification_secret",
        _CERTIFICATION_SECRET,
    )
    return start(owner)


def _central_send_event(actor_id: int, target_id: int) -> Event:
    return prepare_event_log(
        event_type="send_message",
        agent_id=target_id,
        source=f"agent:{actor_id}",
        target_agent_id=actor_id,
        payload={"content": "manifest contract"},
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


def test_central_rollback_has_no_expected_item_and_post_commit_loss_stays_pending(
    db_conn: psycopg.Connection[Any], owner: RuntimeIncarnation, v1_lease: dict[str, Any]
) -> None:
    target_id = owner.agent_id + 100_000
    db_conn.execute("INSERT INTO agents(id,label) VALUES(%s,'manifest-target')", (target_id,))
    event = _central_send_event(owner.agent_id, target_id)
    with db_conn.transaction(force_rollback=True):
        tagged = stage_central_expected_event(
            db_conn, event, origin_kind="contract_rollback", origin_id=1
        )
        assert tagged.attributes["impersonation_session"] == f"{owner.agent_id}:0"
    assert (
        db_conn.execute(
            "SELECT 1 FROM agent_impersonation_event_expected_items WHERE lease_id=%s",
            (v1_lease["id"],),
        ).fetchone()
        is None
    )

    with db_conn.transaction():
        stage_central_expected_event(
            db_conn, event, origin_kind="contract_post_commit_loss", origin_id=2
        )
    db_conn.commit()
    leases.release(str(v1_lease["id"]), attested_caller(v1_lease), "Telemetry enqueue was lost")
    lease = history.resolve(owner.agent_id, 0)
    assert lease["events_completed_at"] is None
    assert lease["event_delivery_pending_reason"] == "awaiting_indexed_ids"


def test_central_staging_uses_asserted_actor_not_recipient_and_skips_non_agents(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    v1_lease: dict[str, Any],
) -> None:
    """A central audit belongs to its asserted actor, never its recipient or system source."""
    recipient_id = create_agent(db_conn)
    recipient = RuntimeIncarnation(recipient_id, uuid4(), uuid4())
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_generation,runtime_owner,"
        "runtime_kind,lease_expires_at) VALUES(%s,'idling',%s,%s,%s,'process',"
        "clock_timestamp()+interval '10 minutes')",
        (recipient_id, machine_name(), recipient.generation, recipient.owner),
    )
    db_conn.commit()
    recipient_lease = start(recipient)
    actor_event = _central_send_event(owner.agent_id, recipient_id)
    tagged = stage_central_expected_event(
        db_conn, actor_event, origin_kind="actor-recipient-contract", origin_id=1
    )
    assert tagged.attributes["impersonation_session"] == f"{owner.agent_id}:0"
    assert db_conn.execute(
        "SELECT count(*) FROM agent_impersonation_event_expected_items WHERE lease_id=%s",
        (v1_lease["id"],),
    ).fetchone() == (1,)
    assert db_conn.execute(
        "SELECT count(*) FROM agent_impersonation_event_expected_items WHERE lease_id=%s",
        (recipient_lease["id"],),
    ).fetchone() == (0,)

    for origin_id, source in enumerate(
        ("user", "system", "external_client:codex", "agent:nope"), 2
    ):
        untagged = stage_central_expected_event(
            db_conn,
            prepare_event_log(
                event_type="send_message",
                agent_id=recipient_id,
                source=source,
                payload={"content": "not a borrowed actor"},
            ),
            origin_kind="non-agent-contract",
            origin_id=origin_id,
        )
        assert "impersonation_session" not in untagged.attributes
    assert db_conn.execute(
        "SELECT count(*) FROM agent_impersonation_event_expected_items WHERE lease_id=%s",
        (v1_lease["id"],),
    ).fetchone() == (1,)


def test_skew_accepts_both_margins_and_rejects_outside_each(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = 30
    monkeypatch.setattr(settings.general, "impersonation_event_clock_skew_guard_seconds", guard)
    activated = datetime(2026, 1, 1, tzinfo=UTC)
    ended = activated + timedelta(minutes=10)
    lease = {"activated_at": activated, "ended_at": ended}

    def event_at(moment: datetime) -> dict[str, Any]:
        return {
            "event_name": "sdk_call",
            "attributes": {"fn": "ava.agents.send_message", "duration": 0.1},
            "ts": moment,
        }

    assert _validate_event(event_at(activated - timedelta(seconds=guard)), lease) is not None
    assert _validate_event(event_at(ended + timedelta(seconds=guard)), lease) is not None
    with pytest.raises(ValueError, match="predates"):
        _validate_event(event_at(activated - timedelta(seconds=guard + 1)), lease)
    with pytest.raises(ValueError, match="after"):
        _validate_event(event_at(ended + timedelta(seconds=guard + 1)), lease)


def test_manual_is_pending_manual_while_nonempty_legacy_never_certifies(
    db_conn: psycopg.Connection[Any], owner: RuntimeIncarnation
) -> None:
    manual = leases.request(
        owner.agent_id,
        caller=CallerIdentity(kind="external_agent", subject="manual-contract"),
        relay_provider="codex",
        relay_thread_id="manual-contract-thread",
        process_metadata=recorded_tree(),
        automatic=False,
    )
    document = history.build_document(
        history.resolve(owner.agent_id, manual["session_id"]),
        history.entries(str(manual["id"]), db_conn),
    )
    assert document["statistics"]["event_delivery"]["pending_reason"] == "manual"
    leases.accept(str(manual["id"]), owner.agent_id, owner, "Manual work")
    leases.activate(str(manual["id"]), owner)
    leases.release(str(manual["id"]), attested_caller(manual), "Manual work completed")

    legacy = start(owner)
    event = {
        "id": "legacy-nonempty",
        "ts": datetime.now(UTC).isoformat(),
        "agent_id": owner.agent_id,
        "event_name": "sdk_call",
        "category": "telemetry",
        "source": f"agent:{owner.agent_id}",
        "attributes": {"fn": "ava.agents.send_message", "duration": 0.1},
    }
    from shared.agents.impersonation.impersonation_events import consume_events

    assert consume_events(owner.agent_id, 1, [event]) == 1
    leases.release(str(legacy["id"]), attested_caller(legacy), "Legacy work completed")
    legacy = history.resolve(owner.agent_id, 1)
    assert legacy["events_completed_at"] is None
    assert pending_reason(legacy) == "legacy"


def test_gate_drain_cap_and_expiry_never_invent_an_empty_receipt(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    v1_lease: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    participant = LocalParticipant(str(v1_lease["id"]), owner.agent_id, 0, "contract-drain")
    assert open_local_participant(
        participant.lease_id, agent_id=owner.agent_id, source_key=participant.source_key
    )
    with pytest.raises(leases.ImpersonationError, match="participant seals"):
        leases.release(participant.lease_id, attested_caller(v1_lease), "Held SDK finally")
    gate = db_conn.execute(
        "SELECT manifest_admission_closed_at,status FROM agent_impersonations WHERE id=%s",
        (v1_lease["id"],),
    ).fetchone()
    assert gate is not None and gate[0] is not None and gate[1] == "active"

    monkeypatch.setattr(settings.general, "impersonation_event_manifest_max_items", 1)
    bind_local_participant(participant)
    try:
        capture_local_event(_eligible_sdk_event(owner.agent_id, marker="one"))
        capture_local_event(_eligible_sdk_event(owner.agent_id, marker="two"))
    finally:
        unbind_local_participant(participant)
    state = db_conn.execute(
        "SELECT state FROM agent_impersonation_event_participants WHERE lease_id=%s AND source_key=%s",
        (participant.lease_id, participant.source_key),
    ).fetchone()
    assert state == ("failed",)
    assert db_conn.execute(
        "SELECT alertname FROM alerts WHERE labels->>'lease_id'=%s "
        "AND alertname='ImpersonationManifestCaptureFailed'",
        (participant.lease_id,),
    ).fetchone() == ("ImpersonationManifestCaptureFailed",)


def test_delayed_live_seal_and_expiry_keep_open_receipts_honest(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    v1_lease: dict[str, Any],
) -> None:
    participant = LocalParticipant(str(v1_lease["id"]), owner.agent_id, 0, "contract-slow")
    assert open_local_participant(
        participant.lease_id, agent_id=owner.agent_id, source_key=participant.source_key
    )
    # This is the detach timer's callback: it is an alert only, never a
    # state transition. A held SDK finally can still finish normally.
    alert_if_participant_still_open(participant)
    assert db_conn.execute(
        "SELECT state FROM agent_impersonation_event_participants WHERE lease_id=%s AND source_key=%s",
        (participant.lease_id, participant.source_key),
    ).fetchone() == ("open",)
    assert db_conn.execute(
        "SELECT alertname FROM alerts WHERE labels->>'lease_id'=%s "
        "AND alertname='ImpersonationManifestSealSlow'",
        (participant.lease_id,),
    ).fetchone() == ("ImpersonationManifestSealSlow",)
    seal_local_participant(participant)
    assert db_conn.execute(
        "SELECT state FROM agent_impersonation_event_participants WHERE lease_id=%s AND source_key=%s",
        (participant.lease_id, participant.source_key),
    ).fetchone() == ("sealed",)

    other_agent = create_agent(db_conn)
    other_owner = RuntimeIncarnation(other_agent, uuid4(), uuid4())
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_generation,runtime_owner,"
        "runtime_kind,lease_expires_at) VALUES(%s,'idling',%s,%s,%s,'process',"
        "clock_timestamp()+interval '10 minutes')",
        (other_agent, machine_name(), other_owner.generation, other_owner.owner),
    )
    db_conn.commit()
    expired = start(other_owner)
    participant = LocalParticipant(str(expired["id"]), other_agent, 0, "contract-expiry")
    assert open_local_participant(
        participant.lease_id, agent_id=other_agent, source_key=participant.source_key
    )
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
        (participant.lease_id,),
    )
    db_conn.commit()
    expired_state = leases.get(participant.lease_id, attested_caller(expired))
    assert expired_state["status"] == "expired"
    assert db_conn.execute(
        "SELECT state FROM agent_impersonation_event_participants WHERE lease_id=%s AND source_key=%s",
        (participant.lease_id, participant.source_key),
    ).fetchone() == ("open",)


def test_retention_loss_has_an_alerted_operator_panel_and_no_local_override(
    db_conn: psycopg.Connection[Any], owner: RuntimeIncarnation, v1_lease: dict[str, Any]
) -> None:
    leases.release(str(v1_lease["id"]), attested_caller(v1_lease), "No emitted events")
    db_conn.execute(
        "UPDATE agent_impersonations SET manifest_envelope_floor_at=clock_timestamp()-interval '7 days' "
        "WHERE id=%s",
        (v1_lease["id"],),
    )
    db_conn.commit()
    monitor_manifest_health(machine=machine_name())
    lease = history.resolve(owner.agent_id, 0)
    assert lease["event_delivery_pending_reason"] == "retention_loss"
    panel = retention_loss_panel(machine=machine_name())
    assert panel == [
        {
            "lease_id": str(v1_lease["id"]),
            "agent_id": owner.agent_id,
            "session_id": 0,
            "envelope_floor_at": lease["manifest_envelope_floor_at"],
            "retention_horizon_at": lease["event_delivery_retention_horizon_at"],
            "created_at": lease["created_at"],
            "missing_item_count": 0,
        }
    ]
    assert db_conn.execute(
        "SELECT alertname FROM alerts WHERE labels->>'lease_id'=%s",
        (str(v1_lease["id"]),),
    ).fetchone() == ("ImpersonationEventRetentionLoss",)


def test_retention_loss_panel_surfaces_machine_scoped_manifest_evidence(
    gateway_unit: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator can inspect floor/horizon evidence behind the retention alert."""
    now = datetime(2026, 9, 24, tzinfo=UTC)

    def panel(*, machine: str) -> list[dict[str, Any]]:
        return (
            [
                {
                    "lease_id": "retention-lease",
                    "agent_id": 7,
                    "session_id": 3,
                    "envelope_floor_at": now - timedelta(days=4),
                    "retention_horizon_at": now - timedelta(days=3),
                    "created_at": now - timedelta(days=5),
                    "missing_item_count": 2,
                }
            ]
            if machine == "runner-a"
            else []
        )

    monkeypatch.setattr(alerts_router, "retention_loss_panel", panel)
    response = gateway_unit.get("/api/alerts/impersonation-event-retention?machine=runner-a")
    assert response.status_code == 200
    assert response.json() == [
        {
            "lease_id": "retention-lease",
            "agent_id": 7,
            "session_id": 3,
            "envelope_floor_at": "2026-09-20T00:00:00Z",
            "retention_horizon_at": "2026-09-21T00:00:00Z",
            "created_at": "2026-09-19T00:00:00Z",
            "missing_item_count": 2,
        }
    ]


def test_pending_age_emits_an_alert_without_promoting_the_manifest(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    v1_lease: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The finite alert age makes a stuck delivery visible but never completes it."""
    monkeypatch.setattr(settings.general, "impersonation_event_delivery_alert_age_seconds", 60)
    leases.release(str(v1_lease["id"]), attested_caller(v1_lease), "Indexing still pending")
    db_conn.execute(
        "UPDATE agent_impersonations SET ended_at=clock_timestamp()-interval '61 seconds' WHERE id=%s",
        (v1_lease["id"],),
    )
    db_conn.commit()
    monitor_manifest_health(machine=machine_name())
    lease = history.resolve(owner.agent_id, 0)
    assert lease["events_completed_at"] is None
    assert db_conn.execute(
        "SELECT alertname FROM alerts WHERE labels->>'lease_id'=%s "
        "AND alertname='ImpersonationEventDeliveryPending'",
        (str(v1_lease["id"]),),
    ).fetchone() == ("ImpersonationEventDeliveryPending",)


def test_extra_before_final_read_refuses_and_late_extra_alerts_without_demoting(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    v1_lease: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ava import impersonation_replay as reader
    from services.agent_host.impersonation_events import _watch_completed_manifest_integrity

    leases.release(str(v1_lease["id"]), attested_caller(v1_lease), "No expected events")
    lease = history.resolve(owner.agent_id, 0)
    extra = {
        "id": "unexpected-row",
        "line_sha256": "b" * 64,
        "event_name": "send_message",
        "category": "audit",
        "attributes": {"impersonation_session": f"{owner.agent_id}:0"},
    }
    visible = [extra]

    def get(_path: str, *, params: dict[str, Any]) -> Any:
        import httpx

        rows = [] if params.get("event_name") == "sdk_call" else visible
        return httpx.Response(
            200,
            request=httpx.Request("GET", "http://manifest.test/api/events"),
            json={"items": rows, "meta": {"has_more": False}},
        )

    monkeypatch.setattr(reader, "_get", get)
    assert not reader._indexed_manifest_matches(lease)
    assert history.resolve(owner.agent_id, 0)["events_completed_at"] is None

    visible.clear()
    assert reader._indexed_manifest_matches(lease)
    assert certify(str(v1_lease["id"]))
    completed = history.resolve(owner.agent_id, 0)
    assert completed["events_completed_at"] is not None

    visible.append(extra)
    assert reader.post_completion_integrity_breach(completed)
    _watch_completed_manifest_integrity(machine_name())
    completed = history.resolve(owner.agent_id, 0)
    assert completed["events_completed_at"] is not None
    assert completed["event_delivery_pending_reason"] is None
    assert completed["event_delivery_integrity_alerted_at"] is not None
    assert db_conn.execute(
        "SELECT alertname FROM alerts WHERE labels->>'lease_id'=%s",
        (str(v1_lease["id"]),),
    ).fetchone() == ("ImpersonationEventDeliveryIntegrity",)


def test_target_native_acceptance_binds_the_certification_proof(
    db_conn: psycopg.Connection[Any], owner: RuntimeIncarnation, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request cannot bind another host's proof before target-native consent."""
    monkeypatch.setattr(settings.general, "impersonation_event_manifest_enabled", True)
    monkeypatch.setattr(
        settings.general,
        "impersonation_event_manifest_certification_secret",
        _CERTIFICATION_SECRET,
    )
    requested = start(owner, active=False)
    assert (
        db_conn.execute(
            "SELECT 1 FROM agent_impersonation_event_certifiers WHERE lease_id=%s",
            (requested["id"],),
        ).fetchone()
        is None
    )
    with (
        pytest.raises(psycopg.errors.RaiseException, match="accepted automatic"),
        db_conn.transaction(),
    ):
        db_conn.execute(
            "SELECT admit_impersonation_event_certifier(%s,%s)",
            (requested["id"], _CERTIFICATION_SECRET),
        )
    leases.accept(str(requested["id"]), owner.agent_id, owner, "Target native consent")
    with (
        pytest.raises(psycopg.errors.RaiseException, match="does not belong"),
        db_conn.transaction(),
    ):
        db_conn.execute(
            "SELECT admit_impersonation_event_certifier(%s,%s)",
            (requested["id"], "other-machine-secret-cannot-replace-the-admitted-proof"),
        )
    assert db_conn.execute(
        "SELECT machine,certification_secret FROM agent_impersonation_event_certifiers WHERE lease_id=%s",
        (requested["id"],),
    ).fetchone() == (machine_name(), _CERTIFICATION_SECRET)


def test_runner_cannot_rewrite_ledgers_or_stamp_without_certification_procedure(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    v1_lease: dict[str, Any],
) -> None:
    from shared.agents.impersonation_manifest_grants import grant_manifest_runner_access

    runner = "manifest_contract_runner"
    db_conn.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(runner)))
    try:
        grant_manifest_runner_access(db_conn, runner)
        db_conn.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(sql.Identifier(runner)))
        leases.release(str(v1_lease["id"]), attested_caller(v1_lease), "No expected events")
        with db_conn.transaction():
            db_conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(runner)))
            db_conn.execute("SET LOCAL ava.impersonation_machine='forged-machine'")
            with pytest.raises(psycopg.errors.InsufficientPrivilege), db_conn.transaction():
                db_conn.execute(
                    "UPDATE agent_impersonations SET events_completed_at=clock_timestamp() WHERE id=%s",
                    (v1_lease["id"],),
                )
            with pytest.raises(psycopg.errors.InsufficientPrivilege), db_conn.transaction():
                db_conn.execute(
                    "UPDATE agent_impersonation_event_participant_items SET line_sha256='0' "
                    "WHERE lease_id=%s",
                    (v1_lease["id"],),
                )
            assert db_conn.execute(
                "SELECT count(*) FROM agent_impersonation_event_expected_items WHERE lease_id=%s",
                (v1_lease["id"],),
            ).fetchone() == (0,)
            with pytest.raises(psycopg.errors.InsufficientPrivilege), db_conn.transaction():
                db_conn.execute(
                    "INSERT INTO agent_impersonation_event_expected_receipts(lease_id,origin_kind,origin_id) "
                    "VALUES(%s,'forbidden-runner-ledger-write',1)",
                    (v1_lease["id"],),
                )
            with pytest.raises(psycopg.errors.InsufficientPrivilege), db_conn.transaction():
                db_conn.execute(
                    "SELECT certification_secret FROM agent_impersonation_event_certifiers WHERE lease_id=%s",
                    (v1_lease["id"],),
                )
            with (
                pytest.raises(psycopg.errors.RaiseException, match="accepted automatic"),
                db_conn.transaction(),
            ):
                db_conn.execute(
                    "SELECT admit_impersonation_event_certifier(%s,%s)",
                    (v1_lease["id"], "other-machine-secret-cannot-replace-the-admitted-proof"),
                )
            with (
                pytest.raises(psycopg.errors.RaiseException, match="does not match"),
                db_conn.transaction(),
            ):
                db_conn.execute(
                    "SELECT certify_impersonation_event_delivery(%s,%s)",
                    (v1_lease["id"], "wrong-proof-that-is-long-enough-to-reach-the-check"),
                )
            stamped = db_conn.execute(
                "SELECT certify_impersonation_event_delivery(%s,%s)",
                (v1_lease["id"], _CERTIFICATION_SECRET),
            ).fetchone()
            assert stamped == (True,)
        db_conn.commit()
        assert history.resolve(owner.agent_id, 0)["events_completed_at"] is not None
    finally:
        db_conn.execute("RESET ROLE")
        db_conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(runner)))
        db_conn.execute(sql.SQL("REVOKE {} FROM CURRENT_USER").format(sql.Identifier(runner)))
        db_conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(runner)))


def test_cli_impersonate_send_outbox_retry_certifies_exactly_once(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    v1_lease: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """A failed ``impersonate send`` replays one central receipt, never two inbounds."""
    import argparse
    from contextlib import nullcontext

    import httpx

    from ava import impersonation_replay as reader
    from cli.commands.impersonation import _send
    from services.agent_host.impersonation_events import reconcile_one
    from shared.agents.messages import delivery_outbox as outbox

    class SingleConnectionPool:
        def connection(self, *, timeout: float | None = None) -> Any:
            return nullcontext(db_conn)

    target_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,lease_expires_at) "
        "VALUES(%s,'idling',%s,clock_timestamp()+interval '10 minutes')",
        (target_id, machine_name()),
    )
    db_conn.commit()
    snapshot = outbox.DeliveryOutboxLimits(
        enabled=True,
        retry_backoff_steps=(0.0,),
        budget_seconds=3600.0,
        abandoned_retention_days=30,
        dedup_window_seconds=900.0,
        flush_interval_seconds=1.0,
        max_entries=8,
    )
    monkeypatch.setattr(settings.general, "ava_home", tmp_path)
    monkeypatch.setattr(outbox, "limits", lambda: snapshot)
    outbox._reset_caches_for_tests()
    monkeypatch.setattr("shared.proc_tree.process_metadata", lambda: attested_caller(v1_lease))

    def gateway_down(*_args: Any, **_kwargs: Any) -> Any:
        raise httpx.ConnectError("gateway unavailable")

    monkeypatch.setattr("shared.http_dial.post", gateway_down)
    args = argparse.Namespace(
        agent_id=owner.agent_id,
        session_id=0,
        target_agent_id=target_id,
        content="CLI manifest delivery",
    )
    with pytest.raises(httpx.TransportError):
        _send(args)
    records = [outbox._read(path) for path in outbox.journal_dir().glob("*.json")]
    entries = [entry for entry in records if entry is not None]
    assert len(entries) == 1
    entry = entries[0]

    pool = SingleConnectionPool()
    assert outbox.flush(pool, now=datetime.now(UTC) + timedelta(seconds=1)).delivered == 1
    # A stale retry after the first response was lost carries the same key and
    # must only recover the committed receipt, not insert a second message.
    outbox.record_failed_send(
        agent_id=target_id,
        source=f"agent:{owner.agent_id}",
        content="CLI manifest delivery",
        client_message_id=entry.client_message_id,
    )
    assert outbox.flush(pool, now=datetime.now(UTC) + timedelta(seconds=1)).delivered == 1
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE client_message_id=%s",
        (entry.client_message_id,),
    ).fetchone() == (1,)
    assert db_conn.execute(
        "SELECT count(*) FROM agent_impersonation_event_expected_receipts WHERE lease_id=%s",
        (v1_lease["id"],),
    ).fetchone() == (1,)

    leases.release(str(v1_lease["id"]), attested_caller(v1_lease), "CLI delivery replayed")
    expected = db_conn.execute(
        "SELECT event_key,line_sha256,event_at FROM "
        "agent_impersonation_event_expected_items WHERE lease_id=%s",
        (v1_lease["id"],),
    ).fetchone()
    assert expected is not None

    def get(_path: str, *, params: dict[str, Any]) -> httpx.Response:
        if params.get("event_name") == "sdk_call":
            items: list[dict[str, Any]] = []
        else:
            key, digest, event_at = expected
            items = [
                {
                    "id": key.removeprefix("event:"),
                    "line_sha256": digest,
                    "event_name": "send_message",
                    "category": "audit",
                    "ts": event_at.isoformat(),
                    "agent_id": target_id,
                    "source": f"agent:{owner.agent_id}",
                    "attributes": {"impersonation_session": f"{owner.agent_id}:0"},
                }
            ]
        return httpx.Response(
            200,
            request=httpx.Request("GET", "http://manifest.test/api/events"),
            json={"items": items, "meta": {"has_more": False}},
        )

    monkeypatch.setattr(reader, "_get", get)
    reconcile_one()
    assert history.resolve(owner.agent_id, 0)["events_completed_at"] is not None
