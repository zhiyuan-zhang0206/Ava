"""Relay-bound leases: credentials, inbox routing, heartbeats, failure and abort paths, liveness alerts; split from base/tests/impersonation/test_impersonation.py (task #4922)."""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest

from base.agents import impersonation as leases
from base.agents.messages.caller_identity import CallerIdentity
from base.cluster.machine import machine_name
from base.db import Database, insert_inbound_message
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.tests.impersonation._impersonation_helpers import _active, _agent, _request, _status
from tests.impersonation_support import attested_caller, recorded_tree


def test_request_requires_a_relay_binding(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus
) -> None:
    owner = _agent(db_conn)
    with pytest.raises(ValueError, match="provider"):
        leases.request(
            database,
            event_bus,
            owner.agent_id,
            caller=CallerIdentity(kind="external_agent", subject="codex"),
            relay_provider="nope",
        )
    with pytest.raises(ValueError, match="thread id"):
        leases.request(
            database,
            event_bus,
            owner.agent_id,
            caller=CallerIdentity(kind="external_agent", subject="codex"),
            relay_provider="codex",
        )
    with pytest.raises(ValueError, match="thread id and remote are rejected"):
        leases.request(
            database,
            event_bus,
            owner.agent_id,
            caller=CallerIdentity(kind="external_agent", subject="codex"),
            relay_provider="claude",
            relay_thread_id=str(uuid4()),
        )
    with pytest.raises(ValueError, match="unix:// or ws://"):
        leases.request(
            database,
            event_bus,
            owner.agent_id,
            caller=CallerIdentity(kind="external_agent", subject="codex"),
            relay_provider="codex",
            relay_thread_id=str(uuid4()),
            relay_codex_remote="http://not-a-codex-endpoint",
        )


def test_claude_request_mints_a_scoped_relay_credential(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus
) -> None:
    owner = _agent(db_conn)
    lease = _request(owner, provider="claude", thread=None)
    assert lease["relay_provider"] == "claude"
    assert lease.get("relay_token")
    assert "relay_token_hash" not in lease
    assert (
        leases.relay_get(database, event_bus, lease["id"], lease["relay_token"])["status"]
        == "requested"
    )
    with pytest.raises(leases.ImpersonationError, match="Invalid relay token"):
        leases.relay_get(database, event_bus, lease["id"], "wrong")


def test_request_validates_and_records_the_batch_window(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus
) -> None:
    owner = _agent(db_conn)
    lease = _request(owner, provider="codex")
    assert lease["relay_batch_window_seconds"] == 0  # default: deliver immediately
    for bad in (-1, 301, 1.5, True, "30"):
        with pytest.raises(ValueError, match="relay_batch_window_seconds"):
            leases.request(
                database,
                event_bus,
                owner.agent_id,
                caller=CallerIdentity(kind="external_agent", subject="codex"),
                relay_provider="codex",
                relay_thread_id=str(uuid4()),
                relay_batch_window_seconds=bad,  # type: ignore[arg-type] — the runtime check rejects non-ints
            )
    with pytest.raises(leases.ImpersonationError, match="already has"):
        _request(owner, provider="codex")  # first lease still open
    leases.reject(database, event_bus, lease["id"], owner.agent_id, owner, "window probe done")
    window_off = leases.request(
        database,
        event_bus,
        owner.agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex"),
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
        relay_batch_window_seconds=0,
    )
    assert window_off["relay_batch_window_seconds"] == 0


def test_codex_request_defers_relay_credential_to_activation(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus
) -> None:
    owner = _agent(db_conn)
    lease = _request(owner)
    assert lease["relay_provider"] == "codex"
    assert lease["relay_thread_id"]
    assert "relay_token" not in lease
    with pytest.raises(leases.ImpersonationError, match="Invalid relay token"):
        leases.relay_get(database, event_bus, lease["id"], "any")


def test_accept_without_relay_binding_fails_loudly(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus
) -> None:
    # A legacy-shape lease (created before the relay binding existed) has all
    # relay fields NULL; accepting it must fail loudly, never guess an endpoint.
    owner = _agent(db_conn)
    legacy_id = uuid4()
    db_conn.execute(
        "INSERT INTO agent_impersonations(id,agent_id,source,machine,token_hash,reason,"
        "status,ttl_seconds,expires_at,session_id) VALUES(%s,%s,'external_agent:codex:old',%s,%s,'',"
        "'requested',300,clock_timestamp()+interval '5 minutes',0)",
        (legacy_id, owner.agent_id, machine_name(), "legacy-hash"),
    )
    db_conn.commit()
    with pytest.raises(leases.ImpersonationError, match="no relay binding"):
        leases.accept(database, event_bus, str(legacy_id), owner.agent_id, owner, "Handoff brief")


def test_provision_relay_only_for_the_accepting_incarnation(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus
) -> None:
    owner = _agent(db_conn)
    lease = _request(owner)
    leases.accept(database, event_bus, lease["id"], owner.agent_id, owner, "Handoff brief")
    foreign = RuntimeIncarnation(owner.agent_id, uuid4(), uuid4())
    with pytest.raises(leases.ImpersonationError):
        leases.provision_relay(database, lease["id"], foreign, "relay-credential")
    provisioned = leases.provision_relay(database, lease["id"], owner, "relay-credential")
    assert provisioned is not None
    assert "relay_token_hash" not in provisioned
    assert (
        leases.relay_get(database, event_bus, lease["id"], "relay-credential")["status"]
        == "accepted"
    )


def test_provision_relay_revokes_the_previous_credential(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus
) -> None:
    owner = _agent(db_conn)
    lease = _request(owner)
    leases.accept(database, event_bus, lease["id"], owner.agent_id, owner, "Handoff brief")
    leases.activate(database, event_bus, lease["id"], owner)
    first = leases.provision_relay(database, lease["id"], owner, "first")
    assert first is not None
    second = leases.provision_relay(
        database, lease["id"], owner, "second", expected_generation=first["relay_generation"]
    )
    assert second is not None
    with pytest.raises(leases.ImpersonationError, match="Invalid relay token"):
        leases.relay_get(database, event_bus, lease["id"], "first")
    assert leases.relay_get(database, event_bus, lease["id"], "second")["status"] == "active"


def test_active_lease_binding_inherits_the_replacement_incarnation(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    """Every restart/host turnover mints a fresh incarnation; the active lease's
    accepting binding must follow it so relay supervision can re-provision."""
    owner = _agent(db_conn)
    lease = _active(owner)
    replacement = RuntimeIncarnation(owner.agent_id, uuid4(), uuid4())
    db_conn.execute(
        "UPDATE agents_meta SET runtime_generation=%s,runtime_owner=%s WHERE id=%s",
        (replacement.generation, replacement.owner, owner.agent_id),
    )
    db_conn.commit()
    state = _status(replacement)
    assert state["status"] == "active"
    assert (state["accepted_generation"], state["accepted_owner"]) == (
        str(replacement.generation),
        str(replacement.owner),
    )
    provisioned = leases.provision_relay(
        database,
        lease["id"],
        replacement,
        "relay-credential",
        expected_generation=state["relay_generation"],
    )
    assert provisioned is not None
    assert "relay_token_hash" not in provisioned
    assert (
        leases.relay_get(database, event_bus, lease["id"], "relay-credential")["status"] == "active"
    )
    # The old incarnation cannot mint the relay credential after the transfer.
    with pytest.raises(leases.ImpersonationError):
        leases.provision_relay(database, lease["id"], owner, "old-incarnation-credential")


@pytest.mark.parametrize("kind", ["chat", "heartbeat"])
def test_relay_inbox_uses_the_scoped_credential_only(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus, kind: str
) -> None:
    owner = _agent(db_conn)
    lease = _request(owner, provider="claude", thread=None)
    leases.accept(database, event_bus, lease["id"], owner.agent_id, owner, "Handoff brief")
    leases.activate(database, event_bus, lease["id"], owner)
    insert_inbound_message(
        db_conn, owner.agent_id, "hello", "system", kind=kind, bus=event_bus, database=database
    )
    db_conn.commit()
    with pytest.raises(leases.ImpersonationError, match="Invalid relay token"):
        leases.relay_inbox(database, lease["id"], "wrong")
    rows = leases.relay_inbox(database, lease["id"], lease["relay_token"])
    assert [row["content"] for row in rows] == ["hello"]
    # The relay credential is not a caller attestation: the controller inbox
    # refuses anything that is not the session's recorded process tree.
    with pytest.raises(leases.ImpersonationError, match="caller check failed"):
        leases.inbox(database, lease["id"], lease["relay_token"])


def test_relay_heartbeat_beats_while_open_and_stops_at_terminal(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    owner = _agent(db_conn)
    lease = _request(owner, provider="claude", thread=None)
    leases.relay_heartbeat(database, lease["id"], lease["relay_token"])
    row = leases.relay_get(database, event_bus, lease["id"], lease["relay_token"])
    assert row["relay_heartbeat_at"] is not None
    leases.accept(database, event_bus, lease["id"], owner.agent_id, owner, "Handoff brief")
    leases.activate(database, event_bus, lease["id"], owner)
    leases.relay_heartbeat(database, lease["id"], lease["relay_token"])
    leases.release(database, event_bus, lease["id"], attested_caller(lease), "Done")
    with pytest.raises(leases.ImpersonationError, match="ended"):
        leases.relay_heartbeat(database, lease["id"], lease["relay_token"])


def test_fail_acceptance_rolls_back_with_reason_and_native_note(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    owner = _agent(db_conn)
    lease = _request(owner)
    leases.accept(database, event_bus, lease["id"], owner.agent_id, owner, "Handoff brief")
    result = leases.fail_acceptance(
        database, event_bus, lease["id"], owner, "relay process exited at startup"
    )
    assert result["status"] == "rejected"
    assert result["rejection_reason"] == "relay process exited at startup"
    assert result["relay_last_failure_at"] is not None
    notes = db_conn.execute(
        "SELECT content, payload FROM inbound_messages WHERE agent_id=%s AND kind='system_note'",
        (owner.agent_id,),
    ).fetchall()
    assert any("rolled back" in row[0] and row[1]["note_tag"] == "impersonation" for row in notes)


def test_fail_acceptance_requires_an_accepted_lease(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus
) -> None:
    owner = _agent(db_conn)
    lease = _request(owner)
    with pytest.raises(leases.ImpersonationError):
        leases.fail_acceptance(database, event_bus, lease["id"], owner, "too early")
    leases.accept(database, event_bus, lease["id"], owner.agent_id, owner, "Handoff brief")
    leases.activate(database, event_bus, lease["id"], owner)
    with pytest.raises(leases.ImpersonationError):
        leases.fail_acceptance(database, event_bus, lease["id"], owner, "too late")


def test_record_relay_failure_is_rate_limited(
    db_conn: psycopg.Connection, database: Database
) -> None:
    owner = _agent(db_conn)
    lease = _active(owner)
    assert leases.record_relay_failure(database, lease["id"], owner) is True
    assert leases.record_relay_failure(database, lease["id"], owner) is False


def test_abort_lease_stops_the_takeover_and_keeps_the_request_reason(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    """A core-component death (task #3998) stops the takeover like an expiry:
    the cause lands in rejection_reason prefixed "aborted: ", the request's own
    reason survives, a non-automatic lease gets the legacy end note, and a
    second abort is a no-op."""
    owner = _agent(db_conn)
    lease = _active(owner)
    ended = leases.abort_lease(
        database, event_bus, lease["id"], owner, "the executor process is gone"
    )
    assert ended is not None
    assert ended["status"] == "expired"
    assert ended["rejection_reason"] == "aborted: the executor process is gone"
    assert ended["reason"] == "Handle the next message"
    note = db_conn.execute(
        "SELECT content FROM inbound_messages WHERE id=%s", (ended["summary_inbound_id"],)
    ).fetchone()
    assert note is not None
    assert "stopped — the executor process is gone" in note[0]
    # An abort that loses the race (lease already terminal) is a no-op.
    assert (
        leases.abort_lease(database, event_bus, lease["id"], owner, "the executor process is gone")
        is None
    )


def test_abort_lease_leaves_the_automatic_end_note_to_the_resume_chain(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    """An automatic takeover's end note belongs to the resume chain
    (deliver_handoff), never to the abort transaction itself (task #3998)."""
    owner = _agent(db_conn)
    lease = leases.request(
        database,
        event_bus,
        owner.agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex", instance="test"),
        ttl_seconds=300,
        reason="Handle the next message",
        process_metadata=recorded_tree(),
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
        automatic=True,
        name="Auto takeover",
        executor_name="Codex: test",
    )
    leases.accept(database, event_bus, lease["id"], owner.agent_id, owner, "Handoff brief")
    leases.activate(database, event_bus, lease["id"], owner)
    ended = leases.abort_lease(
        database, event_bus, lease["id"], owner, "the bound relay stopped heartbeating"
    )
    assert ended is not None
    assert ended["status"] == "expired"
    assert ended["rejection_reason"] == "aborted: the bound relay stopped heartbeating"
    assert ended["summary_inbound_id"] is None
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND kind='chat' "
        "AND source='system:impersonation'",
        (owner.agent_id,),
    ).fetchone() == (0,)


def test_relay_liveness_alert_logs_only_for_stale_active_leases(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, database: Database
) -> None:
    owner = _agent(db_conn)
    _active(owner)
    errors: list[tuple[str, object]] = []

    class FakeLogger:
        def error(self, message: str, **fields: object) -> None:
            errors.append((message, fields))

        def exception(self, message: str, **fields: object) -> None:
            errors.append((message, fields))

    monkeypatch.setattr(leases, "logger", FakeLogger())
    leases.relay_liveness_alert(database, owner.agent_id)
    assert len(errors) == 1
    assert "relay heartbeat is stale" in errors[0][0]
    # A fresh heartbeat suppresses the alert.
    db_conn.execute(
        "UPDATE agent_impersonations SET relay_heartbeat_at=clock_timestamp() WHERE agent_id=%s",
        (owner.agent_id,),
    )
    db_conn.commit()
    errors.clear()
    leases.relay_liveness_alert(database, owner.agent_id)
    assert errors == []
    leases.relay_liveness_alert(database, owner.agent_id + 1)
    assert errors == []
