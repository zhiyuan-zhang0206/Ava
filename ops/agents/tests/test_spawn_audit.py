"""Spawn and fork are recorded in `audit_events` by the birth transaction.

Who spawned whom cannot be derived from any later state, so the audit row commits
with the agent row or neither does.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from base.cluster.machine import machine_name
from base.db import Database
from base.events.live.bus import EventBus
from ops.agents import spawn
from ops.agents.spawn import create_agent_row


def _audit(
    db: psycopg.Connection, agent_id: int, name: str
) -> list[tuple[str, int | None, dict[str, Any]]]:
    rows = db.execute(
        "SELECT source, target_agent_id, attributes FROM audit_events "
        "WHERE agent_id=%s AND event_name=%s ORDER BY id",
        (agent_id, name),
    ).fetchall()
    db.commit()
    return [(r[0], r[1], r[2]) for r in rows]


def _first_checkpoint(db: psycopg.Connection, agent_id: int) -> str:
    db.execute(
        "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, "
        "checkpoint, metadata) VALUES (%s, '', 'ckpt-1', 'x', '{}'::jsonb, '{}'::jsonb)",
        (str(agent_id),),
    )
    db.commit()
    return "ckpt-1"


def test_a_spawn_is_recorded_with_its_machine(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus
) -> None:
    machine = machine_name()

    agent_id, _, _, _ = create_agent_row(database, event_bus, spawner="user", machine=machine)

    [(source, target, attributes)] = _audit(db_conn, agent_id, "spawn")
    assert (source, target) == ("user", None)
    assert attributes["machine"] == machine


def test_a_fork_is_recorded_against_its_source_agent(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus
) -> None:
    parent, _, _, _ = create_agent_row(database, event_bus, spawner="user", machine=machine_name())
    checkpoint = _first_checkpoint(db_conn, parent)

    child, _, _, _ = create_agent_row(
        database,
        event_bus,
        spawner="agent:4242",
        fork_from=parent,
        fork_checkpoint=checkpoint,
        machine=machine_name(),
    )

    [(source, target, attributes)] = _audit(db_conn, child, "fork")
    assert (source, target) == ("agent:4242", parent)
    assert attributes["fork_checkpoint"] == checkpoint
    assert _audit(db_conn, child, "spawn") == []


def test_an_agent_sourced_first_prompt_is_recorded_as_a_message(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    agent_id, _, inbound_id, _ = create_agent_row(
        database,
        event_bus,
        spawner="user",
        machine=machine_name(),
        prompt="hello",
        prompt_source="agent:4242",
    )

    [(source, target, attributes)] = _audit(db_conn, agent_id, "send_message")
    assert (source, target) == ("agent:4242", 4242)
    assert attributes["inbound_id"] == inbound_id


def test_a_spawn_whose_audit_fact_cannot_be_recorded_creates_no_agent(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
) -> None:
    before = db_conn.execute("SELECT count(*) FROM agents").fetchone()
    db_conn.commit()

    def refuse(_conn: psycopg.Connection, _event: object) -> None:
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(spawn, "record_audit", refuse)

    with pytest.raises(RuntimeError, match="audit write failed"):
        create_agent_row(database, event_bus, spawner="user", machine=machine_name())

    after = db_conn.execute("SELECT count(*) FROM agents").fetchone()
    db_conn.commit()
    assert after == before
