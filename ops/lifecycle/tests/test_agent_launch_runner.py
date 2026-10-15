from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.agents.birth.spawn import create_agent_row
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.events.live.bus import EventBus
from base.lm import factory
from base.packages.plugin_providers import build_model_catalog
from ops import lifecycle
from ops.lifecycle import launch
from ops.rpc_schemas import LaunchAgentRequest


@pytest.mark.asyncio
async def test_new_launch_attempt_is_repeatable_without_prompt_insertion(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    db_conn: psycopg.Connection,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    agent_id, birth_config, inbound_id, attempt = create_agent_row(
        database,
        event_bus,
        machine=machine_identity.name(),
        prompt="one goal",
        prompt_source="user",
        catalog=build_model_catalog(),
        authority=config_authority,
    )
    wakes: list[tuple[int, str]] = []

    def _wake(_db: object, _bus: object, agent_id: int, payload: str) -> None:
        wakes.append((agent_id, payload))

    def _validate_model(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(factory, "validate_model_config", _validate_model)
    monkeypatch.setattr(launch, "publish_inbound_wake", _wake)
    body = LaunchAgentRequest(
        agent_id=agent_id, launch_attempt_id=attempt, birth_config=birth_config
    )
    pool: ConnectionPool = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=1)
    with pool:
        await lifecycle.launch_agent_op(
            database,
            event_bus,
            body,
            pool,
            catalog=build_model_catalog(),
            local_machine=machine_identity.name(),
        )
        await lifecycle.launch_agent_op(
            database,
            event_bus,
            body,
            pool,
            catalog=build_model_catalog(),
            local_machine=machine_identity.name(),
        )
        stale = body.model_copy(update={"launch_attempt_id": uuid4()})
        with pytest.raises(ValueError, match="stale or misplaced"):
            await lifecycle.launch_agent_op(
                database,
                event_bus,
                stale,
                pool,
                catalog=build_model_catalog(),
                local_machine=machine_identity.name(),
            )
        db_conn.execute("UPDATE agents_meta SET machine='later-placement' WHERE id=%s", (agent_id,))
        db_conn.commit()
        with pytest.raises(ValueError, match="stale or misplaced"):
            await lifecycle.launch_agent_op(
                database,
                event_bus,
                body,
                pool,
                catalog=build_model_catalog(),
                local_machine=machine_identity.name(),
            )
    assert wakes == [(agent_id, "0"), (agent_id, "0")]
    assert db_conn.execute(
        "SELECT id, content, status FROM inbound_messages WHERE agent_id=%s", (agent_id,)
    ).fetchall() == [(inbound_id, "one goal", "pending")]
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("idling",)
