"""Resurrection needs proven closure, proven non-admission or an unowned end
this runtime witnessed, and a refusal is loud."""

from collections.abc import Awaitable, Iterator
from typing import Any, Protocol
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.db import claim_inbound_batch
from agent.ownership.hosted import admit_hosted_runtime, apply_hosted_lifecycle
from base.agents import AgentStatus, ResurrectError, ResurrectRefused
from base.agents.incarnation.resources import (
    IncarnationResources,
    ResourceBirth,
    decode_resources,
)
from base.agents.messages.inbound import InboundKind
from base.cluster.machine import machine_name
from base.config.service_read import ConfigAuthority
from base.db import Database, insert_inbound_message
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.native_process.runtime_incarnation import RuntimeIncarnation
from ops import lifecycle
from ops.agents import wake
from ops.agents.spawn import create_agent_row
from ops.cluster import rpc as cluster_rpc
from ops.cluster.rpc import ClusterOpFailed, ClusterOpUnreachable
from services.agent_runner.agent_host.tests.lifecycle.test_predecessor_closure import (
    _closed_form,
    _retired,
)
from tests.components.ops.resurrection_support import (
    force,
    legacy_row,
    status,
    terminated,
    unowned_receipt,
)


@pytest.fixture(autouse=True)
def wakes(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[int, str]]]:
    captured: list[tuple[int, str]] = []

    def _record(_db: object, _bus: object, agent_id: int, payload: str) -> None:
        captured.append((agent_id, payload))

    monkeypatch.setattr(wake, "publish_inbound_wake", _record)
    yield captured


def _resources(db: psycopg.Connection, aid: int) -> object:
    row = db.execute("SELECT incarnation_resources FROM agents_meta WHERE id=%s", (aid,)).fetchone()
    db.commit()
    assert row is not None
    return row[0]


@pytest.mark.parametrize("guarded", [False, True])
async def test_never_admitted_birth_resurrects_as_a_fresh_hosted_birth(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    wakes: list[tuple[int, str]],
    guarded: bool,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    """No runtime identity plus the unconsumed fresh-INSERT marker proves no
    predecessor allocation exists: resurrection is a fresh birth."""
    marker = ResourceBirth(birth=uuid4()).model_dump(mode="json")
    aid = terminated(
        db_conn,
        marker,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
    )
    trigger = (
        insert_inbound_message(db_conn, aid, "continue", "user", bus=event_bus, database=database)
        if guarded
        else None
    )
    db_conn.commit()

    wake.resurrect_agent(
        database,
        event_bus,
        aid,
        resurrected_by="system" if guarded else "user",
        trigger_inbound_id=trigger,
        trigger_inbound_kind=InboundKind.CHAT if guarded else None,
    )

    assert status(db_conn, aid) == ("idling", None)
    assert _resources(db_conn, aid) == marker
    assert wakes == [(aid, "0")]
    incarnation = await admit_hosted_runtime(
        aops_pool, aid, machine_name(), uuid4(), expected_from="idling", db=database
    )
    assert incarnation is not None
    admitted = decode_resources(_resources(db_conn, aid))
    assert isinstance(admitted, IncarnationResources)
    assert (admitted.generation, admitted.owner) == (incarnation.generation, incarnation.owner)


@pytest.mark.parametrize("receipt", ["none", "unmarked", "foreign"])
@pytest.mark.parametrize("trigger", [False, True])
def test_fresh_birth_transition_reproves_its_evidence_under_the_row_lock(
    db_conn: psycopg.Connection,
    trigger: bool,
    receipt: str,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    """The final CAS, not only the earlier read, requires the unconsumed marker
    or this agent's own unowned termination receipt."""
    aid = terminated(
        db_conn,
        None,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
    )
    named: int | None = None
    if receipt == "unmarked":
        named = insert_inbound_message(
            db_conn, aid, "", "user", kind="terminate", bus=event_bus, database=database
        )
    elif receipt == "foreign":
        other, _, _, _ = create_agent_row(
            database,
            event_bus,
            spawner="user",
            machine=machine_name(),
            authority=config_authority,
            catalog=model_catalog,
        )
        db_conn.commit()
        named = force(other)
        assert unowned_receipt(db_conn, named)
    chat = (
        insert_inbound_message(db_conn, aid, "continue", "user", bus=event_bus, database=database)
        if trigger
        else None
    )
    db_conn.commit()
    with db_conn.cursor() as cur, pytest.raises(ResurrectError, match="0 rows"):
        wake._transition_terminated_to_unclaimed_idling(
            cur,
            aid,
            None,
            unowned_termination=named,
            trigger_inbound_id=chat,
            trigger_inbound_kind=InboundKind.CHAT if trigger else None,
        )
    db_conn.rollback()
    assert status(db_conn, aid) == ("terminated", None)


def test_retired_resources_require_cutover_before_resurrection(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    wakes: list[tuple[int, str]],
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    generation, owner = uuid4(), uuid4()
    before = _retired(generation, owner)
    aid = terminated(
        db_conn,
        before,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
    )
    db_conn.execute(
        "UPDATE agents_meta SET runtime_kind='hosted',runtime_generation=%s,runtime_owner=%s "
        "WHERE id=%s",
        (generation, owner, aid),
    )
    db_conn.commit()

    with pytest.raises(ResurrectRefused, match="runtime_cutover_required"):
        wake.resurrect_agent(database, event_bus, aid, resurrected_by="user")
    assert status(db_conn, aid) == ("terminated", "hosted")
    assert _resources(db_conn, aid) == before
    assert wakes == []


async def test_closed_form_terminated_row_resurrects_through_its_terminate_receipt(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    wakes: list[tuple[int, str]],
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    generation, owner = uuid4(), uuid4()
    before = _retired(generation, owner) | {"frozen_by": 1}
    aid = terminated(
        db_conn,
        before,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
    )
    receipt = db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,kind,source,content,status,claimed_at,applied_at,"
        "observed_at,target_generation,target_owner) VALUES(%s,'terminate','user','','done',"
        "now(),now(),now(),%s,%s) RETURNING id",
        (aid, generation, owner),
    ).fetchone()
    assert receipt is not None
    db_conn.execute(
        "UPDATE agents_meta SET runtime_kind='hosted',runtime_generation=%s,runtime_owner=%s "
        "WHERE id=%s",
        (generation, owner, aid),
    )
    db_conn.commit()
    _closed_form(db_conn, aid, before)

    wake.resurrect_agent(database, event_bus, aid, resurrected_by="user")
    successor = await admit_hosted_runtime(
        aops_pool, aid, machine_name(), uuid4(), expected_from="idling", db=database
    )
    assert successor is not None
    admitted = decode_resources(_resources(db_conn, aid))
    assert isinstance(admitted, IncarnationResources)
    assert admitted.owner == successor.owner != owner


def _refused_locally(
    monkeypatch: pytest.MonkeyPatch,
    db: psycopg.Connection,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> tuple[int, int]:
    """A never-admitted row without the birth marker: unknown, so the real
    in-process op refuses and the chat stays queued."""
    aid = terminated(
        db,
        None,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
    )
    trigger = insert_inbound_message(
        db,
        aid,
        "are you there?",
        "user",
        bus=EventBus.from_settings(),
        database=Database.from_settings(gate=database_gate),
    )
    db.commit()

    async def _unreachable(*_a: object, **_kw: object) -> dict[str, Any]:
        raise ClusterOpUnreachable("local ops server not reachable")

    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _unreachable)
    return aid, trigger


def _refused_remotely(
    monkeypatch: pytest.MonkeyPatch,
    db: psycopg.Connection,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> tuple[int, int]:
    aid = terminated(
        db,
        None,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
    )
    trigger = insert_inbound_message(
        db,
        aid,
        "are you there?",
        "user",
        bus=EventBus.from_settings(),
        database=Database.from_settings(gate=database_gate),
    )
    db.commit()

    async def _failed(*_a: object, **_kw: object) -> dict[str, Any]:
        raise ClusterOpFailed({"error": "ResurrectRefused: runtime_cutover_required"})

    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _failed)
    return aid, trigger


@pytest.mark.parametrize("arrange", [_refused_locally, _refused_remotely])
async def test_auto_resurrect_refusal_is_a_warning_naming_the_reason(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, Any]],
    arrange: Any,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    aid, trigger = arrange(
        monkeypatch,
        db_conn,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
    )
    status = await lifecycle.resurrect_if_terminated(
        Database.from_settings(gate=database_gate),
        event_bus,
        aid,
        trigger_inbound_id=trigger,
        trigger_inbound_kind=InboundKind.CHAT,
    )
    assert status is AgentStatus.TERMINATED
    refused = [r for r in loguru_records if r["extra"].get("event") == "auto_resurrect_refused"]
    assert [(r["level"].name, r["extra"]["reason"]) for r in refused] == [
        ("WARNING", "runtime_cutover_required")
    ]
    pending = db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (trigger,)
    ).fetchone()
    db_conn.commit()
    assert pending == ("pending",)


async def test_unknown_auto_resurrect_failure_propagates(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, Any]],
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    aid = terminated(
        db_conn,
        None,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
    )
    trigger = insert_inbound_message(
        db_conn, aid, "hello", "user", bus=event_bus, database=database
    )
    db_conn.commit()

    async def _failed(*_a: object, **_kw: object) -> dict[str, Any]:
        raise ClusterOpFailed({"error": "launch failed on the home machine"})

    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _failed)
    with pytest.raises(ClusterOpFailed):
        await lifecycle.resurrect_if_terminated(
            Database.from_settings(gate=database_gate),
            event_bus,
            aid,
            trigger_inbound_id=trigger,
            trigger_inbound_kind=InboundKind.CHAT,
        )
    assert not any(r["extra"].get("event") == "auto_resurrect_failed" for r in loguru_records)
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (trigger,)
    ).fetchone() == ("pending",)


# ── Unowned termination ──────────────────────────────────────────────────────
# Only a force ends a row that has no runtime identity. When this runtime's own
# lifecycle left the row unowned (a birth, a resurrection, an applied restart),
# that force records a receipt and the row resurrects.


class _Arrange(Protocol):
    def __call__(
        self,
        conn: psycopg.Connection,
        pool: AsyncConnectionPool,
        /,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> Awaitable[int]: ...


def _unowned_idle(db: psycopg.Connection, aid: int) -> bool:
    row = db.execute(
        "SELECT status,runtime_kind,runtime_generation,runtime_owner,pid "
        "FROM agents_meta WHERE id=%s",
        (aid,),
    ).fetchone()
    db.commit()
    return row == ("idling", None, None, None, None)


async def _admitted(
    pool: AsyncConnectionPool, aid: int, database_gate: ProcessDbGate
) -> RuntimeIncarnation:
    owner = await admit_hosted_runtime(
        pool,
        aid,
        machine_name(),
        uuid4(),
        expected_from="idling",
        db=Database.from_settings(gate=database_gate),
    )
    assert owner is not None
    return owner


async def _spawned(
    db: psycopg.Connection,
    pool: AsyncConnectionPool,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> int:
    """(a) a new agent never admitted: its birth epoch is its origin."""
    aid, _, _, _ = create_agent_row(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(),
        spawner="user",
        machine=machine_name(),
        authority=config_authority,
        catalog=model_catalog,
    )
    return aid


async def _resurrected(
    db: psycopg.Connection,
    pool: AsyncConnectionPool,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> int:
    """(b) resurrected from its retained identity, not admitted again yet."""
    aid = legacy_row(
        db,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
    )
    await _admitted(pool, aid, database_gate=database_gate)
    db.execute(
        "UPDATE agents_meta SET status='terminated',termination_source='user' WHERE id=%s",
        (aid,),
    )
    db.commit()
    wake.resurrect_agent(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(),
        aid,
        resurrected_by="user",
    )
    return aid


async def _restarted(
    db: psycopg.Connection,
    pool: AsyncConnectionPool,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> int:
    """(c) released by its applied restart, no successor admitted yet."""
    aid = legacy_row(
        db,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
    )
    owner = await _admitted(pool, aid, database_gate=database_gate)
    insert_inbound_message(
        db,
        aid,
        "",
        "user",
        kind="restart",
        bus=EventBus.from_settings(),
        database=Database.from_settings(gate=database_gate),
    )
    db.commit()
    await claim_inbound_batch(pool, aid, incarnation=owner, work=None)
    assert (
        await apply_hosted_lifecycle(pool, owner, bus=EventBus.from_settings(), resources=None)
        == "restart"
    )
    return aid


async def _managed_restarted(
    db: psycopg.Connection,
    pool: AsyncConnectionPool,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> int:
    """An actual fresh birth whose original host applied its restart."""
    aid = await _spawned(
        db,
        pool,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
    )
    owner = await _admitted(pool, aid, database_gate=database_gate)
    insert_inbound_message(
        db,
        aid,
        "",
        "user",
        kind="restart",
        bus=EventBus.from_settings(),
        database=Database.from_settings(gate=database_gate),
    )
    db.commit()
    await claim_inbound_batch(pool, aid, incarnation=owner, work=None)
    assert (
        await apply_hosted_lifecycle(pool, owner, bus=EventBus.from_settings(), resources=None)
        == "restart"
    )
    assert isinstance(decode_resources(_resources(db, aid)), IncarnationResources)
    return aid


@pytest.mark.parametrize("guarded", [False, True])
@pytest.mark.parametrize("arrange", [_spawned, _resurrected, _restarted, _managed_restarted])
async def test_a_row_this_runtime_left_and_ended_unowned_resurrects(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    wakes: list[tuple[int, str]],
    arrange: _Arrange,
    guarded: bool,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    """Each way this runtime leaves a row unowned, then a force before the next
    admission: the force records the receipt, the row resurrects as a fresh
    hosted birth, and admission takes the successor."""
    aid = await arrange(
        db_conn,
        aops_pool,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
    )
    assert _unowned_idle(db_conn, aid)
    command = force(aid)
    assert unowned_receipt(db_conn, command)
    trigger = (
        insert_inbound_message(db_conn, aid, "continue", "user", bus=event_bus, database=database)
        if guarded
        else None
    )
    db_conn.commit()
    wakes.clear()

    wake.resurrect_agent(
        database,
        event_bus,
        aid,
        resurrected_by="system" if guarded else "user",
        trigger_inbound_id=trigger,
        trigger_inbound_kind=InboundKind.CHAT if guarded else None,
    )

    assert _unowned_idle(db_conn, aid)
    assert wakes == [(aid, "0")]
    assert await _admitted(aops_pool, aid, database_gate=database_gate)


async def test_a_managed_row_ended_unowned_keeps_its_predecessor_receipt(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    """(b) with a recorded resource set: resurrection keeps the closed set, the
    force leaves the terminate receipt alone, and the successor is admitted
    through it."""
    generation, owner = uuid4(), uuid4()
    closed = IncarnationResources(generation=generation, owner=owner, requests={})
    aid = terminated(
        db_conn,
        closed.model_dump(mode="json"),
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
    )
    db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,kind,source,content,status,claimed_at,applied_at,"
        "observed_at,target_generation,target_owner) VALUES(%s,'terminate','user','','done',"
        "now(),now(),now(),%s,%s)",
        (aid, generation, owner),
    )
    db_conn.execute(
        "UPDATE agents_meta SET runtime_kind='hosted',runtime_generation=%s,runtime_owner=%s,"
        "last_resurrect_inbound_id=NULL WHERE id=%s",
        (generation, owner, aid),
    )
    db_conn.commit()
    wake.resurrect_agent(database, event_bus, aid, resurrected_by="user")
    assert unowned_receipt(db_conn, force(aid))

    wake.resurrect_agent(database, event_bus, aid, resurrected_by="user")

    successor = await _admitted(aops_pool, aid, database_gate=database_gate)
    admitted = decode_resources(_resources(db_conn, aid))
    assert isinstance(admitted, IncarnationResources)
    assert (admitted.generation, admitted.owner) == (successor.generation, successor.owner)
