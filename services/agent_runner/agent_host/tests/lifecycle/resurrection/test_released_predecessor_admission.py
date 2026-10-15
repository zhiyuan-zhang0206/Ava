"""Released runtime ownership transfers an empty resource set after a crash.

Historical release records are not admission credentials. Refusals preserve
current ownership and emit the classified host_admission_refused event.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.ownership.corpse_reap import reap_crash_corpses
from agent.ownership.hosted import (
    admit_hosted_runtime,
    settle_hosted_runtime,
    stamp_turn_fatal,
)
from base.agents.birth.spawn import create_agent_row
from base.agents.incarnation.resources import (
    ExecAllocation,
    IncarnationResources,
    ResourceBirth,
    decode_resources,
)
from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from ops.agents.resurrection_retry import ResurrectSettlementDeferredError
from ops.agents.wake import resurrect_agent


async def _reaped_closed_life(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> tuple[int, UUID, IncarnationResources]:
    """Run the crash path with no lifecycle command up to a reaped closed row.

    Returns the agent, the released owner and the closed resource set as the
    resurrection will see them (status terminated, retained hosted identity,
    this live process as the recorded host).
    """
    machine = machine_identity.name()
    aid, _owner, _prompt_id, _attempt_id = create_agent_row(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(),
        spawner="user",
        machine=machine,
        authority=config_authority,
        catalog=model_catalog,
    )
    db_conn.execute(
        "UPDATE agents_meta SET status='idling',incarnation_resources=%s WHERE id=%s",
        (Jsonb(ResourceBirth(birth=uuid4()).model_dump(mode="json")), aid),
    )
    db_conn.commit()

    owner = uuid4()
    old = await admit_hosted_runtime(
        aops_pool, aid, machine, owner, expected_from="idling", db=database
    )
    assert old is not None
    assert (await stamp_turn_fatal(aops_pool, old)).applied
    assert await settle_hosted_runtime(aops_pool, old, bus=event_bus, resources=None)
    db_conn.execute(
        "UPDATE agents_meta SET last_turn_fatal_at = clock_timestamp() - interval '1 hour' "
        "WHERE id = %s",
        (aid,),
    )
    db_conn.commit()
    reaped = await reap_crash_corpses(aops_pool, machine, owner, bus=event_bus)
    assert [corpse.agent_id for corpse in reaped] == [aid]
    row = db_conn.execute(
        "SELECT incarnation_resources FROM agents_meta WHERE id=%s", (aid,)
    ).fetchone()
    assert row is not None
    resources = decode_resources(row[0])
    assert isinstance(resources, IncarnationResources)
    assert not resources.requests and resources.frozen_by is None
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND kind IN ('restart','terminate')",
        (aid,),
    ).fetchone() == (0,)
    return aid, owner, resources


def _resurrect(
    db_conn: psycopg.Connection[Any], aid: int, machine: str, database_gate: ProcessDbGate
) -> None:
    resurrect_agent(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(),
        aid,
        resurrected_by="user",
        local_machine=machine,
    )
    row = db_conn.execute(
        "SELECT status,runtime_kind,incarnation_resources FROM agents_meta WHERE id=%s", (aid,)
    ).fetchone()
    assert row is not None
    assert (row[0], row[1]) == ("idling", None)
    assert isinstance(decode_resources(row[2]), IncarnationResources)


async def _refused_with(
    aops_pool: AsyncConnectionPool[Any],
    aid: int,
    machine: str,
    owner: UUID,
    database: Database,
    loguru_records: list[dict[str, Any]],
    *,
    reason: str,
) -> None:
    assert (
        await admit_hosted_runtime(
            aops_pool, aid, machine, owner, expected_from="idling", db=database
        )
        is None
    )
    record = next(r for r in loguru_records if r["extra"].get("event") == "host_admission_refused")
    assert record["extra"]["outcome"] == "resource_fence"
    assert record["extra"]["reason"] == reason


async def test_reaper_released_closed_life_readmits_on_its_live_host(
    redis_url: str,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    machine = machine_identity.name()
    aid, owner, resources = await _reaped_closed_life(
        db_conn,
        aops_pool,
        database,
        event_bus,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    _resurrect(db_conn, aid, machine, database_gate)

    # The closed released life re-admits on its own live host — no restart.
    healed = await admit_hosted_runtime(
        aops_pool, aid, machine, owner, expected_from="idling", db=database
    )
    assert healed is not None, (
        "a reaper-released closed previous life must re-admit on its own live host "
        "without a host restart (issue #5049)"
    )
    assert healed.generation != resources.generation


async def test_released_row_does_not_require_a_release_record(
    redis_url: str,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """Current ownership remains sufficient even when all release payload is gone."""
    machine = machine_identity.name()
    aid, owner, _resources = await _reaped_closed_life(
        db_conn,
        aops_pool,
        database,
        event_bus,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    _resurrect(db_conn, aid, machine, database_gate)
    # Admission does not depend on retaining the historical release payload.
    db_conn.execute(
        "UPDATE inbound_messages SET payload = '{}'::jsonb WHERE agent_id=%s AND kind='resurrect'",
        (aid,),
    )
    db_conn.commit()
    assert (
        await admit_hosted_runtime(
            aops_pool, aid, machine, owner, expected_from="idling", db=database
        )
        is not None
    )


async def test_unreleased_live_owner_refuses_a_successor(
    redis_url: str,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    loguru_records: list[dict[str, Any]],
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """An empty set cannot release the current owner of a still-live host."""
    machine = machine_identity.name()
    aid, _owner, resources = await _reaped_closed_life(
        db_conn,
        aops_pool,
        database,
        event_bus,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    _resurrect(db_conn, aid, machine, database_gate)
    db_conn.execute(
        "UPDATE agents_meta SET runtime_kind='hosted',runtime_generation=%s,runtime_owner=%s "
        "WHERE id=%s",
        (resources.generation, resources.owner, aid),
    )
    db_conn.commit()

    await _refused_with(
        aops_pool,
        aid,
        machine,
        uuid4(),
        database,
        loguru_records,
        reason="successor-closure",
    )


async def test_attached_requests_refuse_as_receipt_less(
    redis_url: str,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    loguru_records: list[dict[str, Any]],
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """An attached allocation without a terminal receipt (the V23 shape) refuses
    and is classified as receipt-less, even after runtime ownership is released."""
    machine = machine_identity.name()
    aid, owner, resources = await _reaped_closed_life(
        db_conn,
        aops_pool,
        database,
        event_bus,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    _resurrect(db_conn, aid, machine, database_gate)
    allocation = ExecAllocation(
        request=uuid4(),
        domain=uuid4(),
        request_digest="a" * 64,
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )
    opened = resources.model_copy(update={"requests": {str(allocation.request): allocation}})
    db_conn.execute(
        "UPDATE agents_meta SET incarnation_resources=%s WHERE id=%s",
        (Jsonb(opened.model_dump(mode="json")), aid),
    )
    db_conn.commit()

    await _refused_with(
        aops_pool,
        aid,
        machine,
        owner,
        database,
        loguru_records,
        reason="receipt-less",
    )


async def test_force_freeze_fences_old_owner_until_release(
    redis_url: str,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    loguru_records: list[dict[str, Any]],
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """A freeze blocks same-owner continuation until ownership is released."""
    machine = machine_identity.name()
    aid, owner, resources = await _reaped_closed_life(
        db_conn,
        aops_pool,
        database,
        event_bus,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    frozen = resources.model_copy(update={"frozen_by": 1})
    db_conn.execute(
        "UPDATE agents_meta SET incarnation_resources=%s WHERE id=%s",
        (Jsonb(frozen.model_dump(mode="json")), aid),
    )
    db_conn.commit()
    await _refused_with(
        aops_pool,
        aid,
        machine,
        owner,
        database,
        loguru_records,
        reason="fences",
    )
    _resurrect(db_conn, aid, machine, database_gate)
    successor = await admit_hosted_runtime(
        aops_pool, aid, machine, owner, expected_from="idling", db=database
    )
    assert successor is not None and successor.generation != resources.generation


async def test_deferred_resurrection_rolls_back_its_epoch(
    redis_url: str,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    """No half state: a refused resurrection (an unsettled applied restart still
    anchoring the pointer) leaves no epoch and no flip behind."""
    machine = machine_identity.name()
    aid, _owner, resources = await _reaped_closed_life(
        db_conn,
        aops_pool,
        database,
        event_bus,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    command = db_conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source, status, claimed_at, "
        "applied_at, target_generation, target_owner) "
        "VALUES (%s,'','restart','system:test','claimed',clock_timestamp(),clock_timestamp(),%s,%s) "
        "RETURNING id",
        (aid, resources.generation, resources.owner),
    ).fetchone()
    assert command is not None
    db_conn.execute("UPDATE agents_meta SET lifecycle_command_id=%s WHERE id=%s", (command[0], aid))
    db_conn.commit()

    with pytest.raises(ResurrectSettlementDeferredError):
        resurrect_agent(
            Database.from_settings(gate=database_gate),
            EventBus.from_settings(),
            aid,
            resurrected_by="user",
            local_machine=machine,
        )

    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND kind='resurrect'", (aid,)
    ).fetchone() == (0,)
    row = db_conn.execute(
        "SELECT status,runtime_kind,incarnation_resources FROM agents_meta WHERE id=%s", (aid,)
    ).fetchone()
    assert row is not None
    assert row[0] == "terminated"
    assert decode_resources(row[2]) == resources
