"""Audit facts written by `base.db` are recorded in the transaction that makes them."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from base import db
from base.db import create_agent, insert_spawn_prompt_in_transaction
from base.telemetry import Event


def _audit(
    conn: psycopg.Connection, agent_id: int, event_name: str
) -> list[tuple[str, int | None, dict[str, Any]]]:
    rows = conn.execute(
        "SELECT source, target_agent_id, attributes FROM audit_events "
        "WHERE agent_id=%s AND event_name=%s ORDER BY id",
        (agent_id, event_name),
    ).fetchall()
    conn.commit()
    return [(r[0], r[1], r[2]) for r in rows]


def _refuse(_conn: psycopg.Connection, _event: Event) -> Event:
    raise RuntimeError("audit write failed")


def test_an_inbound_and_its_audit_fact_commit_together(db_conn: psycopg.Connection) -> None:
    agent_id = create_agent(db_conn)

    inbound_id = db.insert_inbound_message(db_conn, agent_id, "", source="user", kind="restart")

    assert _audit(db_conn, agent_id, "restart") == [("user", None, {"inbound_id": inbound_id})]


def test_an_inbound_whose_audit_fact_cannot_be_recorded_is_not_written(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id = create_agent(db_conn)
    monkeypatch.setattr("base.telemetry.audit_events.record_audit", _refuse)

    with pytest.raises(RuntimeError, match="audit write failed"):
        db.insert_inbound_message(db_conn, agent_id, "", source="user", kind="restart")
    db_conn.rollback()

    count = db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent_id,)
    ).fetchone()
    assert count == (0,)
    assert _audit(db_conn, agent_id, "restart") == []


def test_a_compact_request_is_recorded_before_it_is_emitted(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id = create_agent(db_conn)
    seen_at_emit: list[int] = []

    def emit(_event: Event | None) -> None:
        seen_at_emit.append(len(_audit(db_conn, agent_id, "compact")))

    monkeypatch.setattr(db, "_emit_prepared_event", emit)

    db.insert_compact_request_inbound(db_conn, agent_id)

    assert seen_at_emit == [1]
    assert _audit(db_conn, agent_id, "compact") == [("user", None, {"compact_kind": "request"})]


def test_an_agent_sourced_spawn_prompt_is_recorded_with_its_inbound(
    db_conn: psycopg.Connection,
) -> None:
    agent_id = create_agent(db_conn)

    with db_conn.cursor() as cur:
        inbound_id, event = insert_spawn_prompt_in_transaction(cur, agent_id, "hi", "agent:4242")
    db_conn.commit()

    assert event is not None
    assert _audit(db_conn, agent_id, "send_message") == [
        ("agent:4242", 4242, {"inbound_id": inbound_id, "content": "hi"})
    ]


def test_a_user_sourced_spawn_prompt_has_no_audit_fact(db_conn: psycopg.Connection) -> None:
    agent_id = create_agent(db_conn)

    with db_conn.cursor() as cur:
        _inbound_id, event = insert_spawn_prompt_in_transaction(cur, agent_id, "hi", "user")
    db_conn.commit()

    assert event is None
    assert _audit(db_conn, agent_id, "send_message") == []
