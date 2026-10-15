"""A reaper-released closed previous life transfers through its release record (#5049).

The crash path leaves no lifecycle command: the turn fatal is stamped, the
settled row idles, the reaper flips it terminated (no receipt), and a
resurrection releases the row to unowned idling. Its recorded resource set is
closed (empty requests, not frozen) and names this very live host process. The
resurrection transaction writes a generation-scoped release record on its own
inbound; the successor admission consumes it for the same live host process,
and refuses every shape without that record:
a legacy release, a damaged/unsettled previous life, a different generation, a
force-frozen set, and attached requests (receipt-less). Every refusal is
recorded (``last_admission_outcome='resource_fence'`` + a classified
``host_admission_refused`` event), never a raised crash.
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


def _release_record(db_conn: psycopg.Connection[Any], aid: int) -> tuple[str | None, str | None]:
    row = db_conn.execute(
        "SELECT payload -> 'release_target' ->> 'generation', "
        "payload -> 'release_target' ->> 'owner' FROM inbound_messages "
        "WHERE agent_id=%s AND kind='resurrect' ORDER BY id DESC LIMIT 1",
        (aid,),
    ).fetchone()
    assert row is not None, "the resurrection must have written its inbound"
    return row[0], row[1]


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

    # The release record names the exact released generation and owner.
    assert _release_record(db_conn, aid) == (str(resources.generation), str(resources.owner))

    # The closed released life re-admits on its own live host — no restart.
    healed = await admit_hosted_runtime(
        aops_pool, aid, machine, owner, expected_from="idling", db=database
    )
    assert healed is not None, (
        "a reaper-released closed previous life must re-admit on its own live host "
        "without a host restart (issue #5049)"
    )
    assert healed.generation != resources.generation


async def test_legacy_release_without_record_is_refused(
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
    """A pre-#5049 release (no generation-scoped record) must keep refusing: the
    record is the admission door, and nothing derives closure without it."""
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
    # Withdraw the record to the legacy shape: same release, no target binding.
    db_conn.execute(
        "UPDATE inbound_messages SET payload = payload - 'release_target' "
        "WHERE agent_id=%s AND kind='resurrect'",
        (aid,),
    )
    db_conn.commit()
    assert _release_record(db_conn, aid) == (None, None)

    await _refused_with(
        aops_pool,
        aid,
        machine,
        owner,
        database,
        loguru_records,
        reason="successor-closure",
    )
    assert db_conn.execute(
        "SELECT last_admission_outcome FROM agents_meta WHERE id=%s", (aid,)
    ).fetchone() == ("resource_fence",)


async def test_release_record_is_generation_bound(
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
    """A record for one predecessor generation must not release another."""
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
    changed = resources.model_copy(update={"generation": uuid4()})
    db_conn.execute(
        "UPDATE agents_meta SET incarnation_resources=%s WHERE id=%s",
        (Jsonb(changed.model_dump(mode="json")), aid),
    )
    db_conn.commit()

    await _refused_with(
        aops_pool,
        aid,
        machine,
        owner,
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
    and is classified as receipt-less, even in a row carrying a release record."""
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


async def test_force_frozen_release_records_nothing_and_refuses(
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
    """A force-frozen set vouches for nothing: the flip writes no record."""
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
    _resurrect(db_conn, aid, machine, database_gate)
    assert _release_record(db_conn, aid) == (None, None)

    await _refused_with(
        aops_pool,
        aid,
        machine,
        owner,
        database,
        loguru_records,
        reason="successor-closure",
    )


async def test_deferred_resurrection_rolls_back_its_release_record(
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
    anchoring the pointer) leaves no record and no flip behind."""
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
