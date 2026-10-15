import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.db import claim_inbound_batch
from agent.ownership.corpse_reap import reap_crash_corpses
from agent.ownership.hosted import (
    admit_hosted_runtime,
    apply_hosted_lifecycle,
    settle_hosted_runtime,
    stamp_turn_fatal,
)
from base.agents.birth.spawn import create_agent_row
from base.agents.inbox.insert import insert_inbound_message
from base.agents.incarnation.resources import (
    ExecAllocation,
    IncarnationResources,
    ResourceEvidenceError,
    ResourceProcess,
    decode_resources,
    register_exec,
)
from base.agents.incarnation.tests.test_resources import exec_allocation_entry, force_incarnation
from base.agents.messages.inbound import InboundKind
from base.api_contracts.agent_contract import ResurrectError
from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.native_process.runtime_incarnation import RuntimeIncarnation
from ops.agents.wake import resurrect_agent


async def _admitted(
    pool: AsyncConnectionPool,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> RuntimeIncarnation:
    aid, _, _prompt_id, _attempt_id = create_agent_row(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(),
        spawner="user",
        machine=machine_identity.name(),
        authority=config_authority,
        catalog=model_catalog,
    )
    old = await admit_hosted_runtime(
        pool,
        aid,
        machine_identity.name(),
        uuid4(),
        expected_from="idling",
        db=Database.from_settings(gate=database_gate),
    )
    assert old is not None
    return old


async def _resurrected(
    db: psycopg.Connection,
    pool: AsyncConnectionPool,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> tuple[int, int, UUID, IncarnationResources]:
    old = await _admitted(
        pool,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    aid = old.agent_id
    command = insert_inbound_message(
        db,
        aid,
        "",
        "self",
        kind="terminate",
        bus=EventBus.from_settings(),
        database=Database.from_settings(gate=database_gate),
    )
    assert [
        item.id for item in await claim_inbound_batch(pool, aid, incarnation=old, work=None)
    ] == [command]
    assert (
        await apply_hosted_lifecycle(pool, old, bus=EventBus.from_settings(), resources=None)
        == "terminate"
    )
    resurrect_agent(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(),
        aid,
        resurrected_by="user",
        local_machine=machine_identity.name(),
    )
    row = db.execute("SELECT incarnation_resources FROM agents_meta WHERE id=%s", (aid,)).fetchone()
    assert row is not None
    resources = decode_resources(row[0])
    assert isinstance(resources, IncarnationResources)
    return aid, command, old.owner, resources


@pytest.mark.parametrize("missing", ["applied", "observed", "resource_closure"])
async def test_released_owner_uses_resource_closure_instead_of_historical_receipts(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    missing: str,
    database: Database,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    aid, command, owner, resources = await _resurrected(
        db_conn,
        aops_pool,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    if missing == "resource_closure":
        request = uuid4()
        allocation = ExecAllocation(
            request=request,
            domain=uuid4(),
            request_digest="a" * 64,
            deadline=datetime.now(UTC) + timedelta(minutes=1),
        )
        resources = resources.model_copy(update={"requests": {str(request): allocation}})
        db_conn.execute(
            "UPDATE agents_meta SET incarnation_resources=%s WHERE id=%s",
            (Jsonb(resources.model_dump(mode="json")), aid),
        )
    elif missing == "applied":
        db_conn.execute(
            "UPDATE inbound_messages SET status='claimed',applied_at=NULL,observed_at=NULL "
            "WHERE id=%s",
            (command,),
        )
    else:
        db_conn.execute(
            "UPDATE inbound_messages SET status='claimed',observed_at=NULL WHERE id=%s",
            (command,),
        )
    db_conn.commit()

    if missing == "resource_closure":
        assert (
            await admit_hosted_runtime(
                aops_pool, aid, machine_identity.name(), owner, expected_from="idling", db=database
            )
            is None
        )
        assert db_conn.execute(
            "SELECT last_admission_outcome FROM agents_meta WHERE id=%s", (aid,)
        ).fetchone() == ("resource_fence",)
        assert db_conn.execute(
            "SELECT status,runtime_generation,runtime_owner,incarnation_resources "
            "FROM agents_meta WHERE id=%s",
            (aid,),
        ).fetchone() == ("idling", None, None, resources.model_dump(mode="json"))
    else:
        successor = await admit_hosted_runtime(
            aops_pool, aid, machine_identity.name(), owner, expected_from="idling", db=database
        )
        assert successor is not None and successor.generation != resources.generation


async def test_released_empty_resources_do_not_require_a_host_exit_probe(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    from base.agents.incarnation import exec_owner_recovery

    aid, _command, owner, resources = await _resurrected(
        db_conn,
        aops_pool,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    assert resources.host_process is not None
    prior_process = resources.host_process.model_copy(
        update={"starttime": resources.host_process.starttime + 1}
        if resources.host_process.starttime is not None
        else {"birth": resources.host_process.birth + 1}
    )
    resources = resources.model_copy(update={"host_process": prior_process})
    db_conn.execute(
        "UPDATE agents_meta SET incarnation_resources=%s WHERE id=%s",
        (Jsonb(resources.model_dump(mode="json")), aid),
    )
    db_conn.commit()
    checked: list[ResourceProcess] = []

    def unresolved(identity: ResourceProcess) -> bool:
        checked.append(identity)
        return False

    monkeypatch.setattr(exec_owner_recovery, "process_ended", unresolved)

    successor = await admit_hosted_runtime(
        aops_pool, aid, machine_identity.name(), owner, expected_from="idling", db=database
    )
    assert successor is not None
    assert checked == []


@pytest.mark.parametrize("fresh_chat", [False, True])
async def test_crash_reap_resurrect_readmits_in_the_same_live_host(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
    fresh_chat: bool,
) -> None:
    old = await _admitted(
        aops_pool,
        model_catalog=model_catalog,
        config_authority=config_authority,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    assert (await stamp_turn_fatal(aops_pool, old)).applied
    assert await settle_hosted_runtime(aops_pool, old, bus=event_bus, resources=None)
    db_conn.execute(
        "UPDATE agents_meta SET last_turn_fatal_at=now()-interval '16 minutes' WHERE id=%s",
        (old.agent_id,),
    )
    db_conn.commit()
    reaped = await reap_crash_corpses(aops_pool, machine_identity.name(), old.owner, bus=event_bus)
    assert [corpse.agent_id for corpse in reaped] == [old.agent_id]
    trigger = (
        insert_inbound_message(
            db_conn, old.agent_id, "continue", "user", bus=event_bus, database=database
        )
        if fresh_chat
        else None
    )
    db_conn.commit()
    resurrect_agent(
        database,
        event_bus,
        old.agent_id,
        resurrected_by="user",
        local_machine=machine_identity.name(),
        trigger_inbound_id=trigger,
        trigger_inbound_kind=InboundKind.CHAT if fresh_chat else None,
    )
    successor = await admit_hosted_runtime(
        aops_pool,
        old.agent_id,
        machine_identity.name(),
        old.owner,
        expected_from="idling",
        db=database,
    )
    assert successor is not None and successor.generation != old.generation
    assert not await settle_hosted_runtime(aops_pool, old, bus=event_bus, resources=None)
    assert not (await stamp_turn_fatal(aops_pool, old)).applied
    with (
        pytest.raises(ResourceEvidenceError, match="lost its runtime incarnation"),
        db_conn.transaction(),
    ):
        register_exec(db_conn, old, exec_allocation_entry())
    row = db_conn.execute(
        "SELECT status,runtime_generation,runtime_owner,last_turn_fatal_at,incarnation_resources "
        "FROM agents_meta WHERE id=%s",
        (old.agent_id,),
    ).fetchone()
    assert row is not None
    assert row[:4] == ("running", successor.generation, old.owner, None)
    state = decode_resources(row[4])
    assert isinstance(state, IncarnationResources) and state.requests == {}
    assert (state.generation, state.owner) == (successor.generation, successor.owner)
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND kind IN ('terminate','restart')",
        (old.agent_id,),
    ).fetchone() == (0,)


async def test_concurrent_successors_cannot_share_released_resources(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    aid, _, _, _ = await _resurrected(
        db_conn,
        aops_pool,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    db_conn.commit()
    results = await asyncio.gather(
        *(
            admit_hosted_runtime(
                aops_pool,
                aid,
                machine_identity.name(),
                uuid4(),
                expected_from="idling",
                db=database,
            )
            for _ in range(2)
        ),
    )
    admitted = [result for result in results if isinstance(result, RuntimeIncarnation)]
    assert len(admitted) == 1
    assert sum(result is None for result in results) == 1
    assert db_conn.execute(
        "SELECT runtime_generation,runtime_owner FROM agents_meta WHERE id=%s", (aid,)
    ).fetchone() == (admitted[0].generation, admitted[0].owner)


async def test_resurrection_keeps_unresolved_exec_identity_for_receipt_recovery(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    old = await _admitted(
        aops_pool,
        model_catalog=model_catalog,
        config_authority=config_authority,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    entry = exec_allocation_entry()
    with db_conn.transaction():
        register_exec(db_conn, old, entry)
        command = force_incarnation(db_conn, old)
    with pytest.raises(ResurrectError, match="exec resources remain unresolved"):
        resurrect_agent(
            database,
            event_bus,
            old.agent_id,
            resurrected_by="user",
            local_machine=machine_identity.name(),
        )
    row = db_conn.execute(
        "SELECT status,runtime_generation,runtime_owner,incarnation_resources,lifecycle_command_id "
        "FROM agents_meta WHERE id=%s",
        (old.agent_id,),
    ).fetchone()
    assert row is not None
    assert row[:3] == ("terminated", old.generation, old.owner)
    assert row[3]["requests"][str(entry.request)] == entry.model_dump(mode="json")
    assert row[4] == command
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND kind='resurrect'",
        (old.agent_id,),
    ).fetchone() == (0,)
