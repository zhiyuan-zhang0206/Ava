"""Log-native leases (protocol v2) record their SDK/audit events at the source."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from base.agents import impersonation as leases
from base.agents.impersonation import event_log
from base.agents.impersonation import history as history
from base.agents.impersonation.event_signals import emit_incomplete_event_logs
from base.agents.impersonation.manifest import (
    LocalCaptureGate,
    LocalParticipant,
    capture_local_event,
    open_local_participant,
    pending_reason,
    record_central_event,
    seal_local_participant,
)
from base.agents.impersonation.tests import test_history as history_cases
from base.cluster.machine import machine_name
from base.config.service_read import ConfigAuthority
from base.db import Database, create_agent
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.telemetry import Event
from base.telemetry.audit_events import prepare_event_log
from tests.impersonation_support import attested_caller


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


def _central_event(db_conn: psycopg.Connection[Any], actor_id: int) -> Event:
    target_id = actor_id + 100_000
    db_conn.execute(
        "INSERT INTO agents(id,label) VALUES(%s,'event-log-target') ON CONFLICT DO NOTHING",
        (target_id,),
    )
    return prepare_event_log(
        event_type="send_message",
        agent_id=target_id,
        source=f"agent:{actor_id}",
        target_agent_id=actor_id,
        payload={"content": "event log"},
    )


def _rows(db_conn: psycopg.Connection[Any], lease_id: object, source_key: str) -> int:
    row = db_conn.execute(
        "SELECT count(*) FROM agent_impersonation_entries WHERE lease_id=%s AND source_key=%s",
        (lease_id, source_key),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _expire(db_conn: psycopg.Connection[Any], lease: dict[str, Any]) -> None:
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' "
        "WHERE id=%s",
        (lease["id"],),
    )
    db_conn.commit()
    assert (
        leases.get(
            Database.from_settings(),
            EventBus.from_settings(),
            str(lease["id"]),
            attested_caller(lease),
        )["status"]
        == "expired"
    )


def _participant(owner: RuntimeIncarnation, lease: dict[str, Any], key: str) -> LocalParticipant:
    participant = LocalParticipant(
        str(lease["id"]), owner.agent_id, lease["session_id"], key, Database.from_settings()
    )
    assert open_local_participant(
        Database.from_settings(), participant.lease_id, agent_id=owner.agent_id, source_key=key
    )
    return participant


def _capture(participant: LocalParticipant, events: list[Event]) -> None:
    gate = LocalCaptureGate(participant)
    for event in events:
        capture_local_event(event, gate=gate)


def test_new_automatic_lease_is_log_native(lease: dict[str, Any]) -> None:
    assert lease["event_delivery_protocol_version"] == 2


def test_central_event_commits_with_its_transaction_and_survives_a_lost_emit(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    database: Database,
    event_bus: EventBus,
) -> None:
    event = _central_event(db_conn, owner.agent_id)
    with db_conn.transaction(force_rollback=True):
        record_central_event(db_conn, event)
    assert _rows(db_conn, lease["id"], event_log.CENTRAL_SOURCE) == 0

    with db_conn.transaction():
        tagged = record_central_event(db_conn, event)
    db_conn.commit()
    # The post-commit emit is never called here: the record needs no second store.
    assert tagged.attributes["impersonation_session"] == f"{owner.agent_id}:0"
    assert _rows(db_conn, lease["id"], event_log.CENTRAL_SOURCE) == 1

    leases.release(
        database, event_bus, str(lease["id"]), attested_caller(lease), "Central work only"
    )
    ended = history.resolve(database, owner.agent_id, 0)
    assert ended["events_completed_at"] is not None
    assert pending_reason(ended) is None
    document = history.build_document(ended, history.entries(str(ended["id"]), db_conn))
    delivery = document["statistics"]["event_delivery"]
    assert delivery["state"] == "complete"
    assert delivery["completion_basis"] == "source_log"
    assert delivery["api_events"]["consumed_event_count"] == 1
    completion = [row for row in document["lifecycle"] if "event_delivery_complete" in str(row)]
    assert completion[0]["payload"]["api_event_count"] == 1


def test_central_entry_references_its_audit_row_and_entries_resolve_the_body(
    db_conn: psycopg.Connection[Any], owner: RuntimeIncarnation, lease: dict[str, Any]
) -> None:
    event = _central_event(db_conn, owner.agent_id)
    with db_conn.transaction():
        tagged = record_central_event(db_conn, event)
    db_conn.commit()

    stored = db_conn.execute(
        "SELECT payload FROM agent_impersonation_entries WHERE lease_id=%s AND source_key=%s",
        (lease["id"], event_log.CENTRAL_SOURCE),
    ).fetchone()
    assert stored is not None
    # The entry holds the reference only, no second copy of the body.
    assert set(stored[0]) == {"event_uid", "id", "line_sha256"}
    row = db_conn.execute(
        "SELECT event_uid,event_name,attributes FROM audit_events WHERE event_uid=%s",
        (stored[0]["event_uid"],),
    ).fetchone()
    assert row is not None
    assert row[1] == "send_message"
    assert row[2]["impersonation_session"] == tagged.attributes["impersonation_session"]

    [api] = [
        entry["payload"]
        for entry in history.entries(str(lease["id"]), db_conn)
        if entry["kind"] == "api_event"
    ]
    assert (api["event_name"], api["source"], api["category"]) == (
        "send_message",
        event.source,
        "audit",
    )
    assert api["attributes"] == tagged.attributes
    assert api["line_sha256"] == stored[0]["line_sha256"]


def test_resolving_a_reference_without_its_audit_row_fails_loudly(
    db_conn: psycopg.Connection[Any], owner: RuntimeIncarnation, lease: dict[str, Any]
) -> None:
    with db_conn.transaction():
        record_central_event(db_conn, _central_event(db_conn, owner.agent_id))
    db_conn.execute("SET LOCAL session_replication_role = replica")
    db_conn.execute("DELETE FROM audit_events")
    with pytest.raises(RuntimeError, match="audit_events has no row"):
        history.entries(str(lease["id"]), db_conn)
    db_conn.rollback()


def test_central_append_stops_once_admission_closes(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    database: Database,
    event_bus: EventBus,
) -> None:
    participant = _participant(owner, lease, "closing")
    with pytest.raises(leases.ImpersonationError, match="participant seals"):
        leases.release(
            database, event_bus, participant.lease_id, attested_caller(lease), "Held SDK finally"
        )
    event = _central_event(db_conn, owner.agent_id)
    with db_conn.transaction():
        untagged = record_central_event(db_conn, event)
    assert untagged is event
    assert _rows(db_conn, lease["id"], event_log.CENTRAL_SOURCE) == 0
    with pytest.raises(psycopg.errors.RaiseException, match="admission is closed"):
        db_conn.execute(
            "INSERT INTO agent_impersonation_entries(lease_id,seq,kind,event_key,payload,source_key) "
            "VALUES(%s,9999,'api_event','event:forced','{}'::jsonb,'central')",
            (lease["id"],),
        )
    db_conn.rollback()


def test_local_events_are_recorded_sealed_and_complete_at_release(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    database: Database,
    event_bus: EventBus,
) -> None:
    participant = _participant(owner, lease, "local-happy")
    first, second = _sdk_event(owner.agent_id, "one"), _sdk_event(owner.agent_id, "two")
    _capture(participant, [first, second, first])
    assert _rows(db_conn, lease["id"], "local-happy") == 2
    seal_local_participant(LocalCaptureGate(participant))
    assert db_conn.execute(
        "SELECT state,item_count FROM agent_impersonation_event_participants "
        "WHERE lease_id=%s AND source_key='local-happy'",
        (lease["id"],),
    ).fetchone() == ("sealed", 2)

    leases.release(database, event_bus, participant.lease_id, attested_caller(lease), "Local work")
    ended = history.resolve(database, owner.agent_id, 0)
    assert ended["events_completed_at"] is not None
    document = history.build_document(ended, history.entries(str(ended["id"]), db_conn))
    assert document["statistics"]["event_delivery"]["sdk_calls"]["consumed_event_count"] == 2
    assert document["statistics"]["sdk_calls"] == {"one": 1, "two": 1}


def test_release_waits_for_an_open_source(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    database: Database,
    event_bus: EventBus,
) -> None:
    participant = _participant(owner, lease, "still-open")
    with pytest.raises(leases.ImpersonationError, match="participant seals"):
        leases.release(
            database, event_bus, participant.lease_id, attested_caller(lease), "Too early"
        )
    assert history.resolve(database, owner.agent_id, 0)["status"] == "active"


def test_expiry_with_an_open_source_stays_pending_until_it_seals(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    database: Database,
) -> None:
    participant = _participant(owner, lease, "late-seal")
    _expire(db_conn, lease)
    ended = history.resolve(database, owner.agent_id, 0)
    assert ended["events_completed_at"] is None
    assert pending_reason(ended) == "awaiting_participant_seal"

    _capture(participant, [_sdk_event(owner.agent_id, "after-expiry")])
    assert _rows(db_conn, lease["id"], "late-seal") == 1
    seal_local_participant(LocalCaptureGate(participant))
    done = history.resolve(database, owner.agent_id, 0)
    assert done["events_completed_at"] is not None
    assert pending_reason(done) is None

    with pytest.raises(psycopg.errors.RaiseException, match="receipt is not open"):
        db_conn.execute(
            "INSERT INTO agent_impersonation_entries(lease_id,seq,kind,event_key,payload,source_key) "
            "VALUES(%s,9999,'sdk_call','event:forced','{}'::jsonb,'late-seal')",
            (lease["id"],),
        )
    db_conn.rollback()


def test_seal_count_must_match_the_recorded_rows(
    db_conn: psycopg.Connection[Any], owner: RuntimeIncarnation, lease: dict[str, Any]
) -> None:
    participant = _participant(owner, lease, "miscount")
    _capture(participant, [_sdk_event(owner.agent_id, "one")])
    with pytest.raises(psycopg.errors.RaiseException, match="count does not match"):
        db_conn.execute(
            "SELECT seal_impersonation_event_participant(%s,'miscount','sealed',NULL,5)",
            (lease["id"],),
        )
    db_conn.rollback()
    assert db_conn.execute(
        "SELECT state FROM agent_impersonation_event_participants WHERE lease_id=%s "
        "AND source_key='miscount'",
        (lease["id"],),
    ).fetchone() == ("open",)


def test_a_capture_failure_keeps_the_lease_pending_for_good(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    loguru_records: list[dict[str, Any]],
) -> None:
    monkeypatch.setattr(event_log, "MAX_LOG_ENTRIES", 1)
    participant = _participant(owner, lease, "capped")
    with pytest.raises(RuntimeError, match="entry cap"):
        _capture(participant, [_sdk_event(owner.agent_id, "over-cap")])
    assert db_conn.execute(
        "SELECT state FROM agent_impersonation_event_participants WHERE lease_id=%s "
        "AND source_key='capped'",
        (lease["id"],),
    ).fetchone() == ("failed",)
    with pytest.raises(leases.ImpersonationError, match="participant seals"):
        leases.release(
            database, event_bus, participant.lease_id, attested_caller(lease), "Capture failed"
        )
    _expire(db_conn, lease)
    ended = history.resolve(database, owner.agent_id, 0)
    assert ended["events_completed_at"] is None
    assert pending_reason(ended) == "capture_failed"
    assert emit_incomplete_event_logs(db_conn) == 1
    failed = _incomplete_events(loguru_records)
    assert [e["condition"] for e in failed] == ["capture_failed"]
    assert failed[0]["pending_reason"] == "capture_failed"


def _incomplete_events(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        r["extra"]
        for r in records
        if r["extra"].get("event") == "impersonation_event_log_incomplete"
    ]


def test_an_ended_lease_with_an_open_source_signals_by_state_until_it_seals(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    loguru_records: list[dict[str, Any]],
) -> None:
    participant = _participant(owner, lease, "stuck")
    assert emit_incomplete_event_logs(db_conn) == 0  # a live lease is not stuck
    _expire(db_conn, lease)
    assert emit_incomplete_event_logs(db_conn) == 1
    assert emit_incomplete_event_logs(db_conn) == 1  # state, not edge: re-emitted while it holds
    events = _incomplete_events(loguru_records)
    assert [e["condition"] for e in events] == ["seal_stuck", "seal_stuck"]
    assert events[0]["lease_id"] == participant.lease_id
    assert events[0]["agent_id"] == owner.agent_id
    loguru_records.clear()
    seal_local_participant(LocalCaptureGate(participant))
    assert emit_incomplete_event_logs(db_conn) == 0
    assert _incomplete_events(loguru_records) == []


def test_agent_termination_completes_a_fully_sealed_lease(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    database: Database,
) -> None:
    participant = _participant(owner, lease, "terminated")
    _capture(participant, [_sdk_event(owner.agent_id, "before-termination")])
    seal_local_participant(LocalCaptureGate(participant))
    # SQL ends the lease, then closes admission: the close must finish the lease too.
    db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (owner.agent_id,))
    db_conn.commit()
    ended = history.resolve(database, owner.agent_id, 0)
    assert ended["status"] == "expired"
    assert ended["events_completed_at"] is not None


def test_the_handoff_lists_events_in_call_order_not_write_order(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    database: Database,
    event_bus: EventBus,
) -> None:
    participant = _participant(owner, lease, "call-order")
    first = datetime.now(UTC)
    calls = [
        replace(_sdk_event(owner.agent_id, fn), ts=first + timedelta(milliseconds=offset))
        for offset, fn in ((0, "agents.list_agents"), (1, "agents.get_status"), (2, "x.third"))
    ]
    _capture(participant, list(reversed(calls)))
    seal_local_participant(LocalCaptureGate(participant))
    leases.release(
        database, event_bus, participant.lease_id, attested_caller(lease), "Three calls in order"
    )
    document = history.build_document(
        history.resolve(database, owner.agent_id, 0), history.entries(participant.lease_id, db_conn)
    )
    assert [row["payload"]["attributes"]["fn"] for row in document["sdk_events"]] == [
        "agents.list_agents",
        "agents.get_status",
        "x.third",
    ]


def test_a_late_seal_rewrites_the_already_delivered_handoff_file(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: Database,
) -> None:
    from psycopg.types.json import Jsonb

    def workspace_for_agent(_agent_id: int) -> Path:
        return tmp_path

    monkeypatch.setattr(history, "workspace_dir", workspace_for_agent)
    participant = _participant(owner, lease, "late-export")
    _expire(db_conn, lease)
    ended = history.resolve(database, owner.agent_id, 0)
    document, path = history.export_handoff(ended, db_conn)
    assert document["statistics"]["event_delivery"]["state"] == "pending"
    db_conn.execute(
        "UPDATE agent_impersonations SET handoff_document=%s,handoff_path=%s,"
        "handoff_applied_at=now() WHERE id=%s",
        (Jsonb(document), path, lease["id"]),
    )
    db_conn.commit()

    _capture(participant, [_sdk_event(owner.agent_id, "after-handoff")])
    seal_local_participant(LocalCaptureGate(participant))
    exported = json.loads(Path(path).read_text())
    delivery = exported["statistics"]["event_delivery"]
    assert delivery["state"] == "complete"
    assert delivery["sdk_calls"]["consumed_event_count"] == 1


def test_the_reaper_pass_signals_a_stuck_source_until_it_seals(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    loguru_records: list[dict[str, Any]],
) -> None:
    from base.agents.impersonation import maintenance
    from base.db import pool

    participant = _participant(owner, lease, "reaper-stuck")
    _expire(db_conn, lease)

    with pool(max_size=2) as reaper_pool:
        assert maintenance.signal_incomplete_event_logs(reaper_pool) == 1
        assert maintenance.signal_incomplete_event_logs(reaper_pool) == 1  # state, not edge
        seal_local_participant(LocalCaptureGate(participant))
        assert maintenance.signal_incomplete_event_logs(reaper_pool) == 0
    assert [e["condition"] for e in _incomplete_events(loguru_records)] == [
        "seal_stuck",
        "seal_stuck",
    ]
