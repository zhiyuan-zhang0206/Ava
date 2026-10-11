"""A force terminate can commit the recovery wake that resurrects its agent."""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from base.agents.messages.inbound import InboundKind
from base.cluster.machine import MachineIdentity
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.events.live.bus import EventBus
from base.lm.plugin_providers import build_model_catalog
from ops.agents import wake
from ops.agents.resurrection_retry import ResurrectTriggerStaleError
from ops.agents.spawn import create_agent_row
from ops.lifecycle import termination

_WAKE = "Continue from the latest checkpoint."
_MARKED = Jsonb({"hosted_turn_recovery": True})


def _publish_nothing(_db: object, _bus: object, *_args: object) -> None:
    return None


@pytest.fixture(autouse=True)
def _no_wake_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wake, "publish_inbound_wake", _publish_nothing)


@pytest.fixture
def db_pool(db_url: str) -> Iterator[ConnectionPool]:
    with ConnectionPool(
        settings.data_plane.db_url,
        min_size=1,
        max_size=1,
        kwargs={"prepare_threshold": None},
    ) as pool:
        yield cast(ConnectionPool, pool)


@pytest.fixture
def agent_id(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> int:
    """A never-admitted agent: it has no runtime identity, so a force on it
    leaves the unowned-termination receipt that resurrection accepts."""
    new_id, _, _, _ = create_agent_row(
        database,
        event_bus,
        spawner="user",
        machine=machine_identity.name(),
        catalog=build_model_catalog(),
        authority=config_authority,
    )
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


def test_a_chat_queued_before_the_fence_cannot_resurrect(
    db_conn: psycopg.Connection,
    db_pool: ConnectionPool,
    agent_id: int,
    database: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
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
            local_machine=machine_identity.name(),
        )


def test_a_force_terminate_queues_no_wake_unless_asked(
    db_conn: psycopg.Connection, db_pool: ConnectionPool, agent_id: int
) -> None:
    termination._force_terminate_transaction(agent_id, db_pool, source="user")

    assert _wakes(db_conn, agent_id) == []


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
    _, _, _, fence, _cutoff = termination._force_terminate_transaction(
        agent_id, db_pool, source="system"
    )

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
    *,
    machine_identity: MachineIdentity,
) -> None:
    termination._force_terminate_transaction(agent_id, db_pool, source="system")
    row = db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source,created_at) "
        "VALUES(%s,'continue','chat','user',clock_timestamp()) RETURNING id",
        (agent_id,),
    ).fetchone()
    assert row is not None
    wake_id = row[0]
    db_conn.commit()

    wake.resurrect_agent(
        database,
        event_bus,
        agent_id,
        resurrected_by="system",
        trigger_inbound_id=wake_id,
        trigger_inbound_kind=InboundKind.CHAT,
        local_machine=machine_identity.name(),
    )

    assert [source for source, _ in _audit_rows(db_conn, agent_id, "resurrect")] == ["system"]
