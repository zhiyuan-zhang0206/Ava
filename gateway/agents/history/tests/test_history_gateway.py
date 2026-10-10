"""The session timeline pages inside a session using the existing numeric cursors."""

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
def owner(
    db_conn: psycopg.Connection[Any], config_authority: ConfigAuthority
) -> RuntimeIncarnation:
    """Bind the authority home before recording this machine's native owner."""
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


def test_timeline_pages_inside_a_session_using_existing_numeric_cursors(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
) -> None:
    from agent.impersonation_handoff import start_marker
    from base.agents.history.timeline import build_timeline_items
    from base.agents.impersonation.timeline import hydrate
    from gateway.agents.history.timeline import _window_before

    lease = start(owner, config_authority=config_authority, database_gate=database_gate)
    marker = start_marker(lease, notes=_handoff_notes())
    for number in range(15):
        history.say(
            database,
            event_bus,
            str(lease["id"]),
            attested_caller(lease),
            f"Message {number}",
            message_key=str(number),
        )
    items, count = build_timeline_items([marker], [], inputs=_TIMELINE_INPUTS)
    page = hydrate(database, items, owner.agent_id, limit=5)
    assert count == 1
    assert len(page) == 7  # marker + limit+1 lookahead
    assert page[-1].payload == "Message 14"
    cursor = page[-5].item_id
    older = hydrate(database, items, owner.agent_id, limit=5, before=cursor)
    window, more = _window_before(older, cursor, 5)
    assert [item.payload for item in window] == [f"Message {i}" for i in range(5, 10)]
    assert more
    assert page[-1].impersonation is not None
    assert page[-1].impersonation.executor_name == "Codex: thoughtful squirrel"
    archived, _ = build_timeline_items(
        [marker], [], segment_prefix="s2.checkpoint", inputs=_TIMELINE_INPUTS
    )
    archive_page = hydrate(database, archived, owner.agent_id, limit=5)
    assert archive_page[-1].item_id.startswith("s2.checkpoint.0.")
