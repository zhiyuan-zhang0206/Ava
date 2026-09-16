"""Regressions found by independent review of permanent impersonation history."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, LiteralString, cast
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg import sql

from ava import _impersonation_events as recorded
from shared import impersonation as leases
from shared import impersonation_history as history
from shared import impersonation_sessions as sessions
from shared.chat_delivery import insert_chat_inbound_once
from shared.db import create_agent
from shared.machine import machine_name
from shared.runtime_incarnation import RuntimeIncarnation
from tests.impersonation_support import attested_caller, recorded_tree


def test_upgrade_captures_unread_backlog_of_already_active_legacy_session(
    db_conn: psycopg.Connection,
) -> None:
    root = Path(__file__).parents[2]
    migration = root / "migrations/20260913T180056_named-impersonation-history.sql"
    rollback = migration.with_suffix(".down.sql")
    agent_id = create_agent(db_conn)
    legacy_id = uuid4()
    db_conn.commit()
    with db_conn.transaction(force_rollback=True):
        db_conn.execute(sql.SQL(cast(LiteralString, rollback.read_text())))
        db_conn.execute(
            "INSERT INTO agent_impersonations(id,agent_id,source,machine,token_hash,status,"
            "ttl_seconds,expires_at,activated_at) VALUES(%s,%s,'external_agent:codex',%s,"
            "'existing-credential-hash','active',3600,now()+interval '1 hour',now())",
            (legacy_id, agent_id, machine_name()),
        )
        row = db_conn.execute(
            "INSERT INTO inbound_messages(agent_id,content,kind,source) "
            "VALUES(%s,'Unread at upgrade','chat','user') RETURNING id",
            (agent_id,),
        ).fetchone()
        assert row is not None
        # The existing relay has not read this message yet, so no delivery receipt exists.
        db_conn.execute(sql.SQL(cast(LiteralString, migration.read_text())))
        messages = [
            entry
            for entry in history.entries(str(legacy_id), db_conn)
            if entry["kind"] == "message"
        ]
        assert [entry["payload"]["inbound_id"] for entry in messages] == [row[0]]


@pytest.fixture
def session(db_conn: psycopg.Connection) -> dict[str, Any]:
    agent_id = create_agent(db_conn)
    owner = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_generation,runtime_owner,"
        "runtime_kind,lease_expires_at) VALUES(%s,'idling',%s,%s,%s,'process',"
        "clock_timestamp()+interval '10 minutes')",
        (agent_id, machine_name(), owner.generation, owner.owner),
    )
    db_conn.commit()
    requested = sessions.request(
        agent_id,
        name="Review regressions",
        executor_name="Codex reviewer",
        provider="codex",
        thread_id=str(uuid4()),
        process_metadata=recorded_tree(),
    )
    lease = history.resolve(agent_id, requested["session_id"])
    leases.accept(str(lease["id"]), agent_id, owner, "Check the history contract")
    leases.activate(str(lease["id"]), owner)
    return history.resolve(agent_id, requested["session_id"])


def test_idempotent_inbound_retry_preserves_one_real_message(
    db_conn: psycopg.Connection, session: dict[str, Any]
) -> None:
    arguments: dict[str, Any] = {
        "agent_id": session["agent_id"],
        "content": "One logical message",
        "source": "user",
        "payload": None,
        "client_message_id": "impersonation-review-retry",
    }
    first = insert_chat_inbound_once(db_conn, **arguments)
    retried = insert_chat_inbound_once(db_conn, **arguments)
    assert first.inserted
    assert not retried.inserted
    assert retried.inbound_id == first.inbound_id
    actual = db_conn.execute(
        "SELECT id FROM inbound_messages WHERE agent_id=%s", (session["agent_id"],)
    ).fetchall()
    assert actual == [(first.inbound_id,)]
    messages = [
        row for row in history.entries(str(session["id"]), db_conn) if row["kind"] == "message"
    ]
    assert [row["payload"]["inbound_id"] for row in messages] == [first.inbound_id]


@pytest.fixture
def peer_events(
    db_conn: psycopg.Connection, session: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> tuple[list[dict[str, Any]], int]:
    """Capture the real chat emitter's row shape, replacing only its event sink."""
    events: list[dict[str, Any]] = []

    def capture(category: str, event_name: str, **fields: Any) -> None:
        if category == "audit" and event_name == "send_message":
            events.append(
                {
                    "id": len(events) + 1,
                    "ts": datetime.now(UTC).isoformat(),
                    "category": category,
                    "event_name": event_name,
                    **fields,
                }
            )

    monkeypatch.setattr("shared.audit_events.telemetry.emit", capture)
    recipient = create_agent(db_conn)
    incoming_sender = create_agent(db_conn)
    db_conn.commit()
    for sender, target, content in (
        (session["agent_id"], recipient, "Outgoing peer update"),
        (incoming_sender, session["agent_id"], "Incoming peer update"),
        (incoming_sender, recipient, "Unrelated peer update"),
    ):
        insert_chat_inbound_once(
            db_conn,
            agent_id=target,
            content=content,
            source=f"agent:{sender}",
            payload=None,
            client_message_id=str(uuid4()),
        )
    assert len(events) == 3
    leases.release(str(session["id"]), attested_caller(session), "Sent the peer update")
    return events, recipient


def test_event_reader_consumes_outgoing_peer_operations(
    db_conn: psycopg.Connection,
    session: dict[str, Any],
    peer_events: tuple[list[dict[str, Any]], int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events, _ = peer_events

    def get(path: str, *, params: dict[str, Any]) -> httpx.Response:
        assert path == "/api/events"
        # Match the existing event API's exact filters; the event sink is the
        # only external dependency, so a backwards agent filter loses the row.
        selected = [
            event
            for event in events
            if all(
                params.get(field) is None or event[field] == params[field]
                for field in ("agent_id", "category", "event_name")
            )
        ]
        offset, limit = params["offset"], params["limit"]
        return httpx.Response(
            200,
            request=httpx.Request("GET", "http://test/api/events"),
            json={
                "items": selected[offset : offset + limit],
                "meta": {"has_more": len(selected) > offset + limit},
            },
        )

    monkeypatch.setattr(recorded, "_get", get)
    recorded.consume_recorded_events(history.resolve(session["agent_id"], session["session_id"]))
    consumed = {
        row["payload"]["id"]
        for row in history.entries(str(session["id"]), db_conn)
        if row["kind"] == "api_event"
    }
    assert events[0]["id"] in consumed
    assert events[2]["id"] not in consumed


def test_recipient_statistics_follow_real_chat_event_direction(
    session: dict[str, Any], peer_events: tuple[list[dict[str, Any]], int]
) -> None:
    events, recipient = peer_events
    # Both incoming and outgoing peer messages belong to the conversation;
    # only the outgoing message is evidence of a recipient of this executor.
    rows: list[dict[str, Any]] = [{"kind": "api_event", "payload": event} for event in events[:2]]
    document = history.build_document(
        history.resolve(session["agent_id"], session["session_id"]), rows
    )
    assert document["statistics"]["message_recipients"] == {str(recipient): 1}


def test_gateway_transport_outage_leaves_accounting_pending_and_delivers_handoff(
    session: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent import impersonation_handoff as handoff
    from ava import _gateway_transport as transport

    leases.release(str(session["id"]), attested_caller(session), "Completed external work")
    lease = history.resolve(session["agent_id"], session["session_id"])

    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Event gateway is unavailable", request=request)

    # Exercise the real SDK transport's exception conversion. Replacing _get
    # with an HTTPError would miss GatewayUnavailable after retry exhaustion.
    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(unavailable)) as client:
        monkeypatch.setattr(transport, "_client_singleton", lambda: client)
        monkeypatch.setattr(transport, "_MAX_RETRIES", 1)
        save_document = Mock(return_value=("Done", "/test/0.json"))
        receipt = Mock()
        monkeypatch.setattr(handoff, "_save_document", save_document)
        monkeypatch.setattr(handoff, "_receipt", receipt)
        graph = SimpleNamespace(
            checkpointer=object(),
            aget_state=AsyncMock(
                return_value=SimpleNamespace(
                    values={
                        "impersonation_handoff_id": f"{session['agent_id']}:{session['session_id']}"
                    }
                )
            ),
        )
        owner = RuntimeIncarnation(session["agent_id"], uuid4(), uuid4())
        asyncio.run(handoff.deliver_handoff(graph, lease, owner))

    save_document.assert_called_once_with(lease, owner)
    receipt.assert_called_once_with(lease, owner)
    assert (
        history.resolve(session["agent_id"], session["session_id"])["events_completed_at"] is None
    )
