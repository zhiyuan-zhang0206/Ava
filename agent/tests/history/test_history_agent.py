"""Inbound attachments survive the timeline and the handoff."""

from typing import Any
from uuid import uuid4

import psycopg
import pytest

from base.agents import impersonation as leases
from base.agents.history.timeline_inputs import TimelineReadInputs
from base.agents.impersonation import history as history
from base.agents.impersonation import sessions as sessions
from base.agents.impersonation.notes import HandoffNotes
from base.clock import Clock
from base.cluster.machine import machine_name
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
from tests.impersonation_support import attested_caller, recorded_tree

_TIMELINE_INPUTS = TimelineReadInputs(
    Clock.from_settings, lambda: settings.general.message_timestamps
)


def _handoff_notes() -> HandoffNotes:
    return HandoffNotes(Clock.from_settings, lambda: settings.general.message_timestamps)


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


def start(
    owner: RuntimeIncarnation,
    *,
    active: bool = True,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
) -> dict[str, Any]:
    result = sessions.request(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(),
        owner.agent_id,
        name="Fix login",
        executor_name="Codex: thoughtful squirrel",
        provider="codex",
        thread_id=str(uuid4()),
        process_metadata=recorded_tree(),
        authority=config_authority,
    )
    lease = history.resolve(
        Database.from_settings(gate=database_gate), owner.agent_id, result["session_id"]
    )
    if active:
        leases.accept(
            Database.from_settings(gate=database_gate),
            EventBus.from_settings(),
            str(lease["id"]),
            owner.agent_id,
            owner,
            "Continue the login fix",
        )
        leases.activate(
            Database.from_settings(gate=database_gate),
            EventBus.from_settings(),
            str(lease["id"]),
            owner,
        )
        lease = history.resolve(
            Database.from_settings(gate=database_gate), owner.agent_id, result["session_id"]
        )
    return lease


@pytest.mark.parametrize("automatic", [True, False])
def test_inbound_attachments_survive_timeline_and_handoff(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    automatic: bool,
    config_authority: ConfigAuthority,
    database: Database,
    event_bus: EventBus,
    database_gate: ProcessDbGate,
) -> None:
    from psycopg.types.json import Jsonb

    from agent.impersonation_handoff import start_marker
    from base.agents.history.timeline import build_timeline_items
    from base.agents.impersonation.timeline import hydrate
    from base.agents.upload_delivery.paths import upload_url

    lease = start(owner, config_authority=config_authority, database_gate=database_gate)
    db_conn.execute(
        "UPDATE agent_impersonations SET automatic=%s WHERE id=%s", (automatic, lease["id"])
    )
    valid_url = upload_url(owner.agent_id, "screenshot.png")
    payload = {
        "content_blocks": [
            {"type": "image_url", "image_url": {"url": valid_url}},
            {
                "type": "image_url",
                "image_url": {"url": upload_url(owner.agent_id + 1, "private.png")},
            },
        ]
    }
    inserted = db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,kind,source,content,payload) "
        "VALUES(%s,'chat','user','[image]',%s) RETURNING id",
        (owner.agent_id, Jsonb(payload)),
    ).fetchone()
    assert inserted is not None
    db_conn.commit()
    leases.inbox(database, str(lease["id"]), attested_caller(lease))
    leases.ack(database, event_bus, str(lease["id"]), attested_caller(lease), [inserted[0]])
    items, _ = build_timeline_items(
        [start_marker(lease, notes=_handoff_notes())], [], inputs=_TIMELINE_INPUTS
    )
    projected = hydrate(database, items, owner.agent_id, limit=5)
    image_item = next(item for item in projected if item.inbound_id == inserted[0])
    assert image_item.images == [valid_url]
    document = history.build_document(lease, history.entries(str(lease["id"]), db_conn))
    assert document["messages"][0]["payload"]["payload"] == payload
    assert document["messages"][0]["acknowledged"] is True
