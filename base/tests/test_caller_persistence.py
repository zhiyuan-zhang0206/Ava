"""Structured provenance survives real initiating writes and audit emission."""

from collections.abc import Callable
from uuid import uuid4

import psycopg
import pytest

from base.agents.messages.chat_delivery import insert_chat_inbound_once, reconcile_chat_inbound
from base.db import Database, create_agent, insert_inbound_message
from base.events.live.bus import EventBus
from base.telemetry.audit_events import prepare_event_log

_SOURCE = "external_agent:codex:run-42"
_CALLER = {"kind": "external_agent", "subject": "codex", "instance": "run-42"}


def _admit_future_protocol(_source: str) -> None:
    """Test-only seam: exercise storage beyond the independently tested fence."""


def _admit_future_chat(_conn: psycopg.Connection, _agent_id: int, _source: str) -> None:
    """Storage-only seam; the real generation gate has separate integration proof."""


def test_chat_insert_and_reconcile_persist_same_structured_identity(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    publish_wake: Callable[[int, str], bool],
) -> None:
    # Exercise future admitted-write storage separately from today's closed
    # rollout fence; the tests below assert the real fence rejects every write.
    monkeypatch.setattr(
        "base.agents.messages.chat_delivery.require_caller_protocol", _admit_future_chat
    )
    agent_id = create_agent(db_conn)
    key = str(uuid4())
    receipt = insert_chat_inbound_once(
        db_conn,
        agent_id=agent_id,
        content="hello",
        source=_SOURCE,
        payload={"other": "preserved"},
        client_message_id=key,
        publish_wake=publish_wake,
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


def test_lifecycle_insert_persists_structured_identity(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
) -> None:
    monkeypatch.setattr(
        "base.agents.messages.envelope.reject_unnegotiated_caller", _admit_future_protocol
    )
    agent_id = create_agent(db_conn)
    inbound_id = insert_inbound_message(
        db_conn, agent_id, "", _SOURCE, kind="restart", bus=event_bus, database=database
    )
    with db_conn.cursor() as cur:
        cur.execute("SELECT payload FROM inbound_messages WHERE id = %s", (inbound_id,))
        assert cur.fetchone() == ({"caller_identity": _CALLER},)


def test_conflicting_caller_rejected_before_insert(
    db_conn: psycopg.Connection, publish_wake: Callable[[int, str], bool]
) -> None:
    agent_id = create_agent(db_conn)
    with pytest.raises(ValueError, match="conflicts with source"):
        insert_chat_inbound_once(
            db_conn,
            agent_id=agent_id,
            content="hello",
            source="user",
            payload={"caller_identity": _CALLER},
            client_message_id=str(uuid4()),
            publish_wake=publish_wake,
        )
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM inbound_messages WHERE agent_id = %s", (agent_id,))
        assert cur.fetchone() == (0,)


def test_audit_events_carry_structured_identity() -> None:
    event = prepare_event_log(
        event_type="restart", agent_id=42, source=_SOURCE, payload={"inbound_id": 9}
    )

    assert event.attributes["caller_identity"] == _CALLER
    assert "auth_principal" not in event.attributes


def test_internal_chat_and_lifecycle_writes_cannot_bypass_rollout_fence(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    publish_wake: Callable[[int, str], bool],
) -> None:
    agent_id = create_agent(db_conn)
    with pytest.raises(ValueError, match="target runtime protocol"):
        insert_chat_inbound_once(
            db_conn,
            agent_id=agent_id,
            content="hello",
            source=_SOURCE,
            payload=None,
            client_message_id=str(uuid4()),
            publish_wake=publish_wake,
        )
    with pytest.raises(ValueError, match="target runtime protocol"):
        insert_inbound_message(
            db_conn, agent_id, "", _SOURCE, kind="restart", bus=event_bus, database=database
        )
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM inbound_messages WHERE agent_id = %s", (agent_id,))
        assert cur.fetchone() == (0,)
