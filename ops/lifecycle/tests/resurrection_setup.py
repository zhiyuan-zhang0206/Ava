"""Row setup and retained hosted-force operations for resurrection tests."""

from __future__ import annotations

from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

import base.db
from base.agents.incarnation.resources import IncarnationResources
from base.cluster.machine import machine_name
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from ops.agents import create_agent_row


def noop(_db: object, _bus: object, *_args: object, **_kwargs: object) -> None:
    return None


def agents_row(db: psycopg.Connection, agent_id: int) -> tuple[int, str, str, int | None] | None:
    with db.cursor() as cur:
        cur.execute(
            "SELECT id, spawner, status, pid FROM agents_meta WHERE id = %s",
            (agent_id,),
        )
        return cur.fetchone()


def inbound_count(db: psycopg.Connection, agent_id: int) -> int:
    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM inbound_messages WHERE agent_id = %s", (agent_id,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def spawn_agent(
    *,
    spawner: str = "user",
    fork_from: int | None = None,
    fork_checkpoint: str | None = None,
    config: dict[str, object] | None = None,
    label: str | None = None,
    prompt: str | None = None,
    prompt_source: str | None = None,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> int:
    """Test setup helper — mirrors the pre-#1236 `spawn_agent()` contract
    (create row + launch) as the two-phase split: `create_agent_row`
    (gateway-side, the main data-plane identity) then `_launch_agent_process`
    (runner-side), with the launch stubbed by the autouse guard. The launch op's
    prompt-delivery half is covered in ops/lifecycle/tests/test_operations.py."""
    agent_id, _birth_config, _prompt_id, _attempt_id = create_agent_row(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(),
        spawner=spawner,
        fork_from=fork_from,
        fork_checkpoint=fork_checkpoint,
        machine=machine_name(),
        config=config,
        label=label,
        prompt=prompt,
        prompt_source=prompt_source,
        catalog=model_catalog,
        authority=config_authority,
    )
    base.db.publish_inbound_wake(
        Database.from_settings(gate=database_gate), EventBus.from_settings(), agent_id, "0"
    )
    return agent_id


def inbound_rows(db: psycopg.Connection, agent_id: int) -> list[tuple[str, str, str | None]]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT content, kind, source FROM inbound_messages "
            "WHERE agent_id = %s ORDER BY id ASC",
            (agent_id,),
        )
        return cur.fetchall()


def hosted_agent(
    db: psycopg.Connection,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> int:
    """Seed the retained authority of a hosted incarnation for guard tests."""
    agent_id = spawn_agent(
        config_authority=config_authority, model_catalog=model_catalog, database_gate=database_gate
    )
    generation, owner = uuid4(), uuid4()
    resources = IncarnationResources(generation=generation, owner=owner, requests={})
    db.execute(
        "UPDATE agents_meta SET runtime_kind='hosted', runtime_generation=%s, "
        "runtime_owner=%s, incarnation_resources=%s WHERE id=%s",
        (generation, owner, Jsonb(resources.model_dump(mode="json")), agent_id),
    )
    db.commit()
    return agent_id


async def settle_hosted_force(
    db: psycopg.Connection, pool: AsyncConnectionPool, agent_id: int
) -> None:
    """Complete this inactive fixture through its retained hosted owner."""
    from base.agents.incarnation.hosted_force import original_host_force

    row = db.execute("SELECT runtime_owner FROM agents_meta WHERE id=%s", (agent_id,)).fetchone()
    assert row is not None
    db.commit()
    assert await original_host_force(pool, agent_id, row[0], machine_name(), quiescent=True)


__all__ = [
    "agents_row",
    "hosted_agent",
    "inbound_count",
    "inbound_rows",
    "noop",
    "settle_hosted_force",
    "spawn_agent",
]
