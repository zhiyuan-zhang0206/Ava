"""The versioned launch operation is repeatable and never inserts a prompt."""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.cluster.machine import machine_name
from base.config import settings
from base.db import Database
from base.events.live.bus import EventBus
from ops import lifecycle
from ops.agents.spawn import create_agent_row
from ops.lifecycle import launch
from ops.rpc_schemas import LaunchAgentRequest


@pytest.mark.asyncio
async def test_new_launch_attempt_is_repeatable_without_prompt_insertion(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    db_conn: psycopg.Connection,
) -> None:
    agent_id, birth_config, inbound_id, attempt = create_agent_row(
        database, event_bus, machine=machine_name(), prompt="one goal", prompt_source="user"
    )
    wakes: list[tuple[int, str]] = []

    def _wake(_db: object, _bus: object, agent_id: int, payload: str) -> None:
        wakes.append((agent_id, payload))

    def _validate_model(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr("base.lm.factory.validate_model_config", _validate_model)
    monkeypatch.setattr(launch, "publish_inbound_wake", _wake)
    body = LaunchAgentRequest(
        agent_id=agent_id, launch_attempt_id=attempt, birth_config=birth_config
    )
    pool: ConnectionPool = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=1)
    with pool:
        await lifecycle.launch_agent_op(database, event_bus, body, pool)
        await lifecycle.launch_agent_op(database, event_bus, body, pool)
        stale = body.model_copy(update={"launch_attempt_id": uuid4()})
        with pytest.raises(ValueError, match="stale or misplaced"):
            await lifecycle.launch_agent_op(database, event_bus, stale, pool)
        db_conn.execute("UPDATE agents_meta SET machine='later-placement' WHERE id=%s", (agent_id,))
        db_conn.commit()
        with pytest.raises(ValueError, match="stale or misplaced"):
            await lifecycle.launch_agent_op(database, event_bus, body, pool)
    assert wakes == [(agent_id, "0"), (agent_id, "0")]
    assert db_conn.execute(
        "SELECT id, content, status FROM inbound_messages WHERE agent_id=%s", (agent_id,)
    ).fetchall() == [(inbound_id, "one goal", "pending")]
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("idling",)
