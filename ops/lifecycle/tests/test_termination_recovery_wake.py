"""A force terminate can commit the recovery wake that resurrects its agent."""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from base.agents.messages.inbound import InboundKind
from base.cluster.machine import machine_name
from base.config import settings
from base.db import Database
from base.events.live.bus import EventBus
from ops.agents import wake
from ops.agents.resurrection_retry import ResurrectTriggerStaleError
from ops.agents.spawn import create_agent_row
from ops.lifecycle import terminate_agent_op, termination
from ops.rpc_schemas import TerminateAgentRequest

_WAKE = "Continue from the latest checkpoint."
_MARKED = Jsonb({"hosted_turn_recovery": True})


def _publish_nothing(_db: object, _bus: object, *_args: object) -> None:
    return None


@pytest.fixture(autouse=True)
def _no_wake_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wake, "publish_inbound_wake", _publish_nothing)


@pytest.fixture
def db_pool() -> Iterator[ConnectionPool]:
    with ConnectionPool(
        settings.data_plane.db_url,
        min_size=1,
        max_size=1,
        kwargs={"prepare_threshold": None},
    ) as pool:
        yield cast(ConnectionPool, pool)


@pytest.fixture
def agent_id(db_conn: psycopg.Connection, database: Database, event_bus: EventBus) -> int:
    """A never-admitted agent: it has no runtime identity, so a force on it
    leaves the unowned-termination receipt that resurrection accepts."""
    new_id, _, _, _ = create_agent_row(database, event_bus, spawner="user", machine=machine_name())
    db_conn.commit()
    return new_id


def _wakes(db: psycopg.Connection, agent_id: int) -> list[tuple[int, str, str, str]]:
    rows = db.execute(
        "SELECT id, source, status, content FROM inbound_messages "
        "WHERE agent_id=%s AND kind='chat' AND payload @> %s ORDER BY id",
        (agent_id, _MARKED),
    ).fetchall()
    db.commit()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


def _inbound_count(db: psycopg.Connection, agent_id: int) -> int:
    row = db.execute("SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent_id,))
    count = row.fetchone()
    db.commit()
    assert count is not None
    return int(count[0])


def test_the_wake_commits_with_the_fence_and_qualifies_as_a_trigger(
    db_conn: psycopg.Connection, db_pool: ConnectionPool, agent_id: int
) -> None:
    _, _, _, fence = termination._force_terminate_transaction(
        agent_id, db_pool, source="system", recovery_wake=_WAKE
    )

    [(wake_id, source, status, content)] = _wakes(db_conn, agent_id)
    qualifies = db_conn.execute(
        "SELECT m.created_at > a.status_changed_at, m.id > a.last_force_terminate_inbound_id, "
        "a.status FROM inbound_messages m JOIN agents_meta a ON a.id = m.agent_id WHERE m.id=%s",
        (wake_id,),
    ).fetchone()
    db_conn.commit()
    assert (source, status, content) == ("system", "pending", _WAKE)
    assert wake_id > fence
    # The two predicates the resurrection CAS applies to its trigger.
    assert qualifies == (True, True, "terminated")


def test_the_wake_resurrects_the_agent_through_the_final_cas(
    db_conn: psycopg.Connection,
    db_pool: ConnectionPool,
    agent_id: int,
    database: Database,
    event_bus: EventBus,
) -> None:
    termination._force_terminate_transaction(
        agent_id, db_pool, source="system", recovery_wake=_WAKE
    )
    [(wake_id, _, _, _)] = _wakes(db_conn, agent_id)

    wake.resurrect_agent(
        database,
        event_bus,
        agent_id,
        resurrected_by="system",
        trigger_inbound_id=wake_id,
        trigger_inbound_kind=InboundKind.CHAT,
    )

    status = db_conn.execute("SELECT status FROM agents_meta WHERE id=%s", (agent_id,)).fetchone()
    db_conn.commit()
    assert status == ("idling",)


def test_a_chat_queued_before_the_fence_cannot_resurrect(
    db_conn: psycopg.Connection,
    db_pool: ConnectionPool,
    agent_id: int,
    database: Database,
    event_bus: EventBus,
) -> None:
    """Why the wake follows the terminate command instead of preceding it: work
    older than a force cannot reverse that force."""
    termination._force_terminate_transaction(
        agent_id, db_pool, source="user", message="queued before the fence"
    )
    row = db_conn.execute(
        "SELECT id FROM inbound_messages WHERE agent_id=%s AND kind='chat' "
        "AND content='queued before the fence'",
        (agent_id,),
    ).fetchone()
    db_conn.commit()
    assert row is not None

    with pytest.raises(ResurrectTriggerStaleError):
        wake.resurrect_agent(
            database,
            event_bus,
            agent_id,
            resurrected_by="system",
            trigger_inbound_id=row[0],
            trigger_inbound_kind=InboundKind.CHAT,
        )


def test_a_failed_wake_rolls_the_whole_force_back_and_a_retry_adds_one(
    db_conn: psycopg.Connection, db_pool: ConnectionPool, agent_id: int
) -> None:
    before = _inbound_count(db_conn, agent_id)

    # Postgres text cannot hold NUL: the wake INSERT fails after the fence and
    # the status change were already written in the same transaction.
    with pytest.raises(psycopg.DataError):
        termination._force_terminate_transaction(
            agent_id, db_pool, source="system", recovery_wake="a wake\x00that cannot be stored"
        )

    row = db_conn.execute(
        "SELECT status, last_force_terminate_inbound_id FROM agents_meta WHERE id=%s",
        (agent_id,),
    ).fetchone()
    db_conn.commit()
    assert row == ("idling", None)
    assert _inbound_count(db_conn, agent_id) == before

    termination._force_terminate_transaction(
        agent_id, db_pool, source="system", recovery_wake=_WAKE
    )

    assert len(_wakes(db_conn, agent_id)) == 1
    assert _inbound_count(db_conn, agent_id) == before + 2  # terminate command + wake


def test_a_force_terminate_queues_no_wake_unless_asked(
    db_conn: psycopg.Connection, db_pool: ConnectionPool, agent_id: int
) -> None:
    termination._force_terminate_transaction(agent_id, db_pool, source="user")

    assert _wakes(db_conn, agent_id) == []


async def test_a_recovery_wake_is_refused_without_a_force(
    db_conn: psycopg.Connection,
    db_pool: ConnectionPool,
    agent_id: int,
    database: Database,
    event_bus: EventBus,
) -> None:
    before = _inbound_count(db_conn, agent_id)

    with pytest.raises(ValueError, match="rides a force terminate"):
        await terminate_agent_op(
            database,
            event_bus,
            agent_id,
            TerminateAgentRequest(source="system"),
            db_pool,
            recovery_wake=_WAKE,
        )

    assert _inbound_count(db_conn, agent_id) == before


def _audit_rows(
    db: psycopg.Connection, agent_id: int, event_name: str
) -> list[tuple[str, dict[str, object]]]:
    rows = db.execute(
        "SELECT source, attributes FROM audit_events WHERE agent_id=%s AND event_name=%s "
        "ORDER BY id",
        (agent_id, event_name),
    ).fetchall()
    db.commit()
    return [(r[0], r[1]) for r in rows]


def test_a_force_terminate_records_its_audit_fact_in_the_same_transaction(
    db_conn: psycopg.Connection, db_pool: ConnectionPool, agent_id: int
) -> None:
    _, _, _, fence = termination._force_terminate_transaction(agent_id, db_pool, source="system")

    assert _audit_rows(db_conn, agent_id, "terminate") == [("system", {"inbound_id": fence})]


def test_a_force_terminate_whose_audit_fact_cannot_be_recorded_does_not_terminate(
    db_conn: psycopg.Connection,
    db_pool: ConnectionPool,
    agent_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(_conn: psycopg.Connection, _event: object) -> None:
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(termination, "record_audit", refuse)

    with pytest.raises(RuntimeError, match="audit write failed"):
        termination._force_terminate_transaction(agent_id, db_pool, source="system")

    row = db_conn.execute("SELECT status FROM agents_meta WHERE id=%s", (agent_id,)).fetchone()
    db_conn.commit()
    assert row == ("idling",)
    assert _audit_rows(db_conn, agent_id, "terminate") == []


def test_a_resurrection_records_its_audit_fact(
    db_conn: psycopg.Connection,
    db_pool: ConnectionPool,
    agent_id: int,
    database: Database,
    event_bus: EventBus,
) -> None:
    termination._force_terminate_transaction(
        agent_id, db_pool, source="system", recovery_wake=_WAKE
    )
    [(wake_id, _, _, _)] = _wakes(db_conn, agent_id)

    wake.resurrect_agent(
        database,
        event_bus,
        agent_id,
        resurrected_by="system",
        trigger_inbound_id=wake_id,
        trigger_inbound_kind=InboundKind.CHAT,
    )

    assert [source for source, _ in _audit_rows(db_conn, agent_id, "resurrect")] == ["system"]
