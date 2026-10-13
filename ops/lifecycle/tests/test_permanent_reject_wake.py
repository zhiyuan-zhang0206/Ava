"""Permanent provider errors block background wakes until explicit fresh chat."""

from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from base.agents.inbox.insert import insert_inbound_message
from base.agents.incarnation.resources import ResourceBirth
from base.agents.messages.inbound import InboundKind
from base.api_contracts.agent_contract import AgentStatus
from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from ops import lifecycle
from ops.agents.spawn import create_agent_row


async def test_permanent_reject_blocks_heartbeat_but_fresh_chat_can_try_again(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    agent = terminated(
        db_conn,
        ResourceBirth(birth=uuid4()).model_dump(mode="json"),
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        local_machine=machine_identity,
    )
    db_conn.execute(
        "UPDATE agents_meta SET permanent_reject_streak=1,last_permanent_reject_reason='billing' "
        "WHERE id=%s",
        (agent,),
    )
    heartbeat = db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source) "
        "VALUES(%s,'Heartbeat','heartbeat','system') RETURNING id",
        (agent,),
    ).fetchone()
    assert heartbeat is not None
    db_conn.commit()
    assert (
        await lifecycle.resurrect_if_terminated(
            database,
            event_bus,
            agent,
            local_machine=machine_identity.name(),
            trigger_inbound_id=heartbeat[0],
            trigger_inbound_kind=InboundKind.HEARTBEAT,
        )
        is AgentStatus.TERMINATED
    )
    chat = insert_inbound_message(
        db_conn,
        agent,
        "The provider is fixed; continue.",
        "user",
        bus=event_bus,
        database=database,
    )
    assert (
        await lifecycle.resurrect_if_terminated(
            database,
            event_bus,
            agent,
            local_machine=machine_identity.name(),
            trigger_inbound_id=chat,
            trigger_inbound_kind=InboundKind.CHAT,
        )
        is AgentStatus.IDLING
    )
    assert db_conn.execute(
        "SELECT permanent_reject_streak,last_permanent_reject_reason FROM agents_meta WHERE id=%s",
        (agent,),
    ).fetchone() == (0, None)


def terminated(
    conn: psycopg.Connection,
    resources: dict[str, object],
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    local_machine: MachineIdentity,
) -> int:
    agent, *_ = create_agent_row(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(),
        spawner="user",
        machine=local_machine.name(),
        catalog=model_catalog,
        authority=config_authority,
    )
    conn.execute(
        "UPDATE agents_meta SET status='terminated',termination_source='user',incarnation_resources=%s WHERE id=%s",
        (Jsonb(resources), agent),
    )
    conn.commit()
    return agent


async def test_idling_owner_blocks_background_wake_until_new_chat(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    from base.agents.recovery.breaker import admit_pending_wake

    agent = terminated(
        db_conn,
        ResourceBirth(birth=uuid4()).model_dump(mode="json"),
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        local_machine=machine_identity,
    )
    db_conn.execute(
        "UPDATE agents_meta SET status='idling',last_turn_fatal_at=clock_timestamp(),permanent_reject_streak=1 WHERE id=%s",
        (agent,),
    )
    db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source) VALUES(%s,'Heartbeat','heartbeat','system')",
        (agent,),
    )
    db_conn.commit()
    assert not await admit_pending_wake(aops_pool, agent)
    assert not await admit_pending_wake(aops_pool, agent)
    insert_inbound_message(
        db_conn, agent, "The provider is fixed.", "user", bus=event_bus, database=database
    )
    assert await admit_pending_wake(aops_pool, agent)
    assert db_conn.execute(
        "SELECT permanent_reject_streak FROM agents_meta WHERE id=%s", (agent,)
    ).fetchone() == (0,)
