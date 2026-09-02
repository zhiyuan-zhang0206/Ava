"""Structured provenance survives real initiating writes and audit emission."""

from uuid import uuid4

import psycopg
import pytest

from shared.audit_events import insert_event_log, insert_event_log_many
from shared.chat_delivery import insert_chat_inbound_once, reconcile_chat_inbound
from shared.db import create_agent, insert_inbound_message

_SOURCE = "external_agent:codex:run-42"
_CALLER = {"kind": "external_agent", "subject": "codex", "instance": "run-42"}


def test_chat_insert_and_reconcile_persist_same_structured_identity(
    db_conn: psycopg.Connection,
) -> None:
    agent_id = create_agent(db_conn)
    key = str(uuid4())
    receipt = insert_chat_inbound_once(
        db_conn,
        agent_id=agent_id,
        content="hello",
        source=_SOURCE,
        payload={"other": "preserved"},
        client_message_id=key,
    )
    with db_conn.cursor() as cur:
        cur.execute("SELECT payload FROM inbound_messages WHERE id = %s", (receipt.inbound_id,))
        assert cur.fetchone() == ({"other": "preserved", "caller_identity": _CALLER},)
    reconciled = reconcile_chat_inbound(
        db_conn,
        client_message_id=key,
        agent_id=agent_id,
        content="hello",
        source=_SOURCE,
        payload={"other": "preserved"},
    )
    assert reconciled is not None
    assert reconciled.inbound_id == receipt.inbound_id
    assert not reconciled.inserted


def test_lifecycle_insert_persists_structured_identity(db_conn: psycopg.Connection) -> None:
    agent_id = create_agent(db_conn)
    inbound_id = insert_inbound_message(db_conn, agent_id, "", _SOURCE, kind="restart")
    with db_conn.cursor() as cur:
        cur.execute("SELECT payload FROM inbound_messages WHERE id = %s", (inbound_id,))
        assert cur.fetchone() == ({"caller_identity": _CALLER},)


def test_conflicting_caller_rejected_before_insert(db_conn: psycopg.Connection) -> None:
    agent_id = create_agent(db_conn)
    with pytest.raises(ValueError, match="conflicts with source"):
        insert_chat_inbound_once(
            db_conn,
            agent_id=agent_id,
            content="hello",
            source="user",
            payload={"caller_identity": _CALLER},
            client_message_id=str(uuid4()),
        )
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM inbound_messages WHERE agent_id = %s", (agent_id,))
        assert cur.fetchone() == (0,)


def test_single_and_batch_audit_carry_structured_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import Mock

    emit = Mock()
    monkeypatch.setattr("shared.audit_events.telemetry.emit", emit)
    insert_event_log(event_type="restart", agent_id=42, source=_SOURCE, payload={"inbound_id": 9})
    insert_event_log_many(
        event_type="restart", agent_id=42, source=_SOURCE, payloads=[{"inbound_id": 10}]
    )
    assert emit.call_count == 2
    for call in emit.call_args_list:
        assert call.kwargs["attributes"]["caller_identity"] == _CALLER
        assert "auth_principal" not in call.kwargs["attributes"]
