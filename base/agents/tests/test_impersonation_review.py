"""Regressions found by independent review of permanent impersonation history."""

from collections.abc import Callable
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from base.agents import impersonation as leases
from base.agents.impersonation import history as history
from base.agents.impersonation import sessions as sessions
from base.agents.messages.chat_delivery import insert_chat_inbound_once
from base.cluster.machine import machine_name
from base.config.service_read import ConfigAuthority
from base.db import Database, create_agent
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
from tests.impersonation_support import attested_caller, recorded_tree


@pytest.fixture
def session(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
) -> dict[str, Any]:
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
        database,
        event_bus,
        agent_id,
        name="Review regressions",
        executor_name="Codex reviewer",
        provider="codex",
        thread_id=str(uuid4()),
        process_metadata=recorded_tree(),
        authority=config_authority,
    )
    lease = history.resolve(database, agent_id, requested["session_id"])
    leases.accept(
        database, event_bus, str(lease["id"]), agent_id, owner, "Check the history contract"
    )
    leases.activate(database, event_bus, str(lease["id"]), owner)
    return history.resolve(database, agent_id, requested["session_id"])


def test_idempotent_inbound_retry_preserves_one_real_message(
    db_conn: psycopg.Connection, session: dict[str, Any], publish_wake: Callable[[int, str], bool]
) -> None:
    arguments: dict[str, Any] = {
        "agent_id": session["agent_id"],
        "content": "One logical message",
        "source": "user",
        "payload": None,
        "client_message_id": "impersonation-review-retry",
    }
    first = insert_chat_inbound_once(db_conn, **arguments, publish_wake=publish_wake)
    retried = insert_chat_inbound_once(db_conn, **arguments, publish_wake=publish_wake)
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
def peer_chats(
    db_conn: psycopg.Connection,
    session: dict[str, Any],
    database: Database,
    event_bus: EventBus,
    publish_wake: Callable[[int, str], bool],
) -> int:
    """Send real chats through the chat emitter; return the executor's recipient."""
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
            publish_wake=publish_wake,
        )
    leases.release(
        database, event_bus, str(session["id"]), attested_caller(session), "Sent the peer update"
    )
    return recipient


def test_only_the_executors_outgoing_peer_operations_enter_the_lease_log(
    db_conn: psycopg.Connection, session: dict[str, Any], peer_chats: int
) -> None:
    api = [
        row["payload"]
        for row in history.entries(str(session["id"]), db_conn)
        if row["kind"] == "api_event"
    ]
    assert [(event["event_name"], event["source"]) for event in api] == [
        ("send_message", f"agent:{session['agent_id']}")
    ]
    assert api[0]["agent_id"] == peer_chats


def test_recipient_statistics_follow_real_chat_event_direction(
    db_conn: psycopg.Connection, session: dict[str, Any], peer_chats: int, database: Database
) -> None:
    # Both incoming and outgoing peer messages belong to the conversation;
    # only the outgoing message is evidence of a recipient of this executor.
    document = history.build_document(
        history.resolve(database, session["agent_id"], session["session_id"]),
        history.entries(str(session["id"]), db_conn),
    )
    assert document["statistics"]["message_recipients"] == {str(peer_chats): 1}
