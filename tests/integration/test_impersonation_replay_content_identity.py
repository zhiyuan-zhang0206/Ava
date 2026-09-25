"""Consumed event replay keeps producer content as its durable identity."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from shared.agents.impersonation import impersonation_history as history
from shared.agents.impersonation.impersonation_events import consume_events
from shared.db import create_agent
from shared.machine import machine_name
from shared.runtime_incarnation import RuntimeIncarnation
from tests.shared import test_impersonation_history as history_cases


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


def _event(owner: RuntimeIncarnation) -> dict[str, Any]:
    return {
        "id": "replay-event",
        "ts": datetime.now(UTC).isoformat(),
        "trace_id": None,
        "span_id": None,
        "agent_id": owner.agent_id,
        "machine": machine_name(),
        "process": "test",
        "category": "telemetry",
        "event_name": "sdk_call",
        "level": "info",
        "source": f"agent:{owner.agent_id}",
        "target_agent_id": None,
        "attributes": {"fn": "ava.tasks.create", "duration": 0.0},
    }


@pytest.mark.parametrize("kind", ["sdk_call", "api_event"])
def test_added_reader_field_preserves_original_row(
    db_conn: psycopg.Connection[Any], owner: RuntimeIncarnation, kind: str
) -> None:
    lease = history_cases.start(owner)
    event = _event(owner)
    if kind == "api_event":
        event.update(category="audit", event_name="task_create", attributes={"task_id": "task"})
    key = f"event:{event['id']}"
    original_seq = history.append(db_conn, str(lease["id"]), kind, event, event_key=key)

    replay_seq = history.append(
        db_conn, str(lease["id"]), kind, {**event, "line_sha256": "new"}, event_key=key
    )

    assert replay_seq == original_seq
    rows = db_conn.execute(
        "SELECT seq,payload FROM agent_impersonation_entries WHERE lease_id=%s AND event_key=%s",
        (lease["id"], key),
    ).fetchall()
    assert rows == [(original_seq, event)]


def test_changed_reader_value_preserves_original_sequence(
    db_conn: psycopg.Connection[Any], owner: RuntimeIncarnation
) -> None:
    lease = history_cases.start(owner)
    event = _event(owner)
    key = f"event:{event['id']}"
    original_seq = history.append(
        db_conn, str(lease["id"]), "sdk_call", {**event, "line_sha256": "old"}, event_key=key
    )

    replay_seq = history.append(
        db_conn, str(lease["id"]), "sdk_call", {**event, "line_sha256": "new"}, event_key=key
    )

    assert replay_seq == original_seq


def test_producer_content_drift_is_rejected(
    db_conn: psycopg.Connection[Any], owner: RuntimeIncarnation
) -> None:
    lease = history_cases.start(owner)
    event = _event(owner)
    key = f"event:{event['id']}"
    history.append(db_conn, str(lease["id"]), "sdk_call", event, event_key=key)

    with pytest.raises(ValueError, match="different content"):
        history.append(
            db_conn,
            str(lease["id"]),
            "sdk_call",
            {**event, "attributes": {"fn": "ava.tasks.delete", "duration": 0.0}},
            event_key=key,
        )


def test_locally_authored_message_keeps_full_payload_comparison(
    db_conn: psycopg.Connection[Any], owner: RuntimeIncarnation
) -> None:
    lease = history_cases.start(owner)
    payload = {"direction": "out", "content": "first"}
    history.append(db_conn, str(lease["id"]), "message", payload, event_key="reply:t1")

    with pytest.raises(ValueError, match="different content"):
        history.append(
            db_conn,
            str(lease["id"]),
            "message",
            {**payload, "content": "changed"},
            event_key="reply:t1",
        )


def test_consume_events_replays_pre_projection_row(
    db_conn: psycopg.Connection[Any], owner: RuntimeIncarnation
) -> None:
    lease = history_cases.start(owner)
    event = _event(owner)
    key = f"event:{event['id']}"
    history.append(db_conn, str(lease["id"]), "sdk_call", event, event_key=key)
    db_conn.commit()

    assert (
        consume_events(owner.agent_id, lease["session_id"], [{**event, "line_sha256": "new"}]) == 0
    )
    rows = db_conn.execute(
        "SELECT payload FROM agent_impersonation_entries WHERE lease_id=%s AND event_key=%s",
        (lease["id"], key),
    ).fetchall()
    assert rows == [(event,)]
