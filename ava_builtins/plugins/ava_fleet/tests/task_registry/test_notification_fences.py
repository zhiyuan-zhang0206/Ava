"""Durable task assignments retain owner fences and bounded failure evidence."""

from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from ava_builtins.plugins.ava_fleet import task_registry
from ava_builtins.plugins.ava_fleet.tests.test_task_registry import _seed_agent
from ava_builtins.plugins.ava_fleet.tests.test_task_registry import root_task_id as root_task_id
from base.agents.incarnation.resources import ResourceBirth
from base.agents.messages.inbound import InboundKind
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db import pool as db_pool
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from ops.agents import wake
from ops.agents.resurrection_retry import ResurrectTriggerStaleError
from services.wake.delivery_watchdog.dead_letter import dead_letter_stale_pending_terminated
from services.wake.delivery_watchdog.resurrect_retry import select_terminated_owners_with_pending
from tests.components.ops.test_resurrection_admission import _status, _terminated
from tests.fixtures.pin_agent import pin_agent


def test_reassigned_task_refuses_delayed_home_wake(
    db_conn: psycopg.Connection,
    root_task_id: int,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
) -> None:
    """An in-flight selector/RPC cannot revive a recipient after reassignment."""
    actor = _seed_agent(db_conn)
    pin_agent(actor)
    owner = _terminated(
        db_conn,
        ResourceBirth(birth=uuid4()).model_dump(mode="json"),
        config_authority=config_authority,
        model_catalog=model_catalog,
    )
    next_owner = _seed_agent(db_conn)
    task = task_registry.create(
        "fenced", "work", parent=root_task_id, owner=owner, operation_key=str(uuid4())
    )
    note_pool = db_pool()
    assert note_pool is not None
    selected = select_terminated_owners_with_pending(note_pool, 86400)
    assert len(selected) == 1
    # Model a producer not using the notification helper, while the old row
    # remains pending. The final home-side transaction must re-check ownership.
    db_conn.execute("UPDATE agent_tasks SET owner=%s WHERE id=%s", (next_owner, task.id))
    db_conn.commit()
    with pytest.raises(ResurrectTriggerStaleError, match="no longer current"):
        wake.resurrect_agent(
            database,
            event_bus,
            owner,
            resurrected_by="system",
            trigger_inbound_id=selected[0][1],
            trigger_inbound_kind=InboundKind.SYSTEM_NOTE,
        )
    assert _status(db_conn, owner)[0] == "terminated"
    assert select_terminated_owners_with_pending(note_pool, 86400) == []
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND kind='resurrect'", (owner,)
    ).fetchone() == (0,)


def test_assignment_deadline_has_inspectable_failure(
    db_conn: psycopg.Connection,
    root_task_id: int,
) -> None:
    actor = _seed_agent(db_conn)
    pin_agent(actor)
    owner = _seed_agent(db_conn, status="terminated")
    task_registry.create(
        "expires", "work", parent=root_task_id, owner=owner, operation_key=str(uuid4())
    )
    db_conn.execute(
        "UPDATE inbound_messages SET created_at=now()-interval '25 hours' WHERE agent_id=%s",
        (owner,),
    )
    db_conn.commit()
    note_pool = db_pool()
    assert note_pool is not None
    assert select_terminated_owners_with_pending(note_pool, 86400) == []
    assert dead_letter_stale_pending_terminated(note_pool, 86400) == 1
    row = db_conn.execute(
        "SELECT status,payload FROM inbound_messages WHERE agent_id=%s", (owner,)
    ).fetchone()
    assert row is not None and row[0] == "done"
    assert row[1]["delivery_result"] == {
        "outcome": "failed",
        "reason": "resurrection_deadline_expired",
    }


def test_reminder_policy_never_resurrects_terminated_owner(
    db_conn: psycopg.Connection,
    root_task_id: int,
) -> None:
    actor = _seed_agent(db_conn)
    pin_agent(actor)
    owner = _seed_agent(db_conn, status="terminated")
    task = task_registry.create(
        "reminder", "work", parent=root_task_id, owner=owner, operation_key=str(uuid4())
    )
    db_conn.execute(
        "UPDATE inbound_messages SET payload=%s WHERE agent_id=%s",
        (
            Jsonb(
                {
                    "note_tag": "task",
                    "task_id": task.id,
                    "task_notification": True,
                    "delivery_resurrect": False,
                }
            ),
            owner,
        ),
    )
    db_conn.commit()
    note_pool = db_pool()
    assert note_pool is not None
    assert select_terminated_owners_with_pending(note_pool, 86400) == []
