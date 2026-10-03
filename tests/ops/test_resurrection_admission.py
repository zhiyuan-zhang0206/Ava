"""Resurrection needs proven closure, proven non-admission or an unowned end
this runtime witnessed, and a refusal is loud."""

from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from agent.db import claim_inbound_batch
from agent.ownership.hosted import admit_hosted_runtime, apply_hosted_lifecycle
from base.agents import AgentStatus, ResurrectError, ResurrectRefused
from base.agents.incarnation.resources import (
    IncarnationResources,
    ResourceBirth,
    decode_resources,
)
from base.cluster.machine import machine_name
from base.config import settings
from base.db import PG_KEEPALIVE_KWARGS, Database, insert_inbound_message
from base.deploy.maintenance import cohort, pause_owner
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import bind_turn_identity
from ops import cluster_rpc, lifecycle
from ops.agents import wake
from ops.agents.resurrection_retry import ResurrectSettlementDeferredError
from ops.agents.spawn import create_agent_row
from ops.cluster_rpc import ClusterOpFailed, ClusterOpUnreachable
from ops.lifecycle import termination
from services.agent_host.tests.test_predecessor_closure import _closed_form, _retired


@pytest.fixture(autouse=True)
def wakes(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[int, str]]]:
    captured: list[tuple[int, str]] = []

    def _record(_db: object, _bus: object, agent_id: int, payload: str) -> None:
        captured.append((agent_id, payload))

    monkeypatch.setattr(wake, "publish_inbound_wake", _record)
    yield captured


def _terminated(db: psycopg.Connection, resources: object) -> int:
    aid, _, _, _ = create_agent_row(
        Database.from_settings(), EventBus.from_settings(), spawner="user", machine=machine_name()
    )
    db.execute(
        "UPDATE agents_meta SET status='terminated',termination_source='user',"
        "incarnation_resources=%s WHERE id=%s",
        (None if resources is None else Jsonb(resources), aid),
    )
    db.commit()
    return aid


def _resources(db: psycopg.Connection, aid: int) -> object:
    row = db.execute("SELECT incarnation_resources FROM agents_meta WHERE id=%s", (aid,)).fetchone()
    db.commit()
    assert row is not None
    return row[0]


def _status(db: psycopg.Connection, aid: int) -> tuple[str, str | None]:
    row = db.execute("SELECT status,runtime_kind FROM agents_meta WHERE id=%s", (aid,)).fetchone()
    db.commit()
    assert row is not None
    return row[0], row[1]


@pytest.mark.parametrize("guarded", [False, True])
async def test_never_admitted_birth_resurrects_as_a_fresh_hosted_birth(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    wakes: list[tuple[int, str]],
    guarded: bool,
    database: Database,
    event_bus: EventBus,
) -> None:
    """No runtime identity plus the unconsumed fresh-INSERT marker proves no
    predecessor allocation exists: resurrection is a fresh birth."""
    marker = ResourceBirth(birth=uuid4()).model_dump(mode="json")
    aid = _terminated(db_conn, marker)
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
        trigger_inbound_kind="chat" if guarded else None,
    )

    assert _status(db_conn, aid) == ("idling", None)
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
) -> None:
    """The final CAS, not only the earlier read, requires the unconsumed marker
    or this agent's own unowned termination receipt."""
    aid = _terminated(db_conn, None)
    named: int | None = None
    if receipt == "unmarked":
        named = insert_inbound_message(
            db_conn, aid, "", "user", kind="terminate", bus=event_bus, database=database
        )
    elif receipt == "foreign":
        other, _, _, _ = create_agent_row(
            database, event_bus, spawner="user", machine=machine_name()
        )
        db_conn.commit()
        named = _force(other)
        assert _unowned_receipt(db_conn, named)
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
            trigger_inbound_kind="chat" if trigger else None,
        )
    db_conn.rollback()
    assert _status(db_conn, aid) == ("terminated", None)


def test_retired_resources_require_cutover_before_resurrection(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    wakes: list[tuple[int, str]],
    database: Database,
    event_bus: EventBus,
) -> None:
    generation, owner = uuid4(), uuid4()
    before = _retired(generation, owner)
    aid = _terminated(db_conn, before)
    db_conn.execute(
        "UPDATE agents_meta SET runtime_kind='hosted',runtime_generation=%s,runtime_owner=%s "
        "WHERE id=%s",
        (generation, owner, aid),
    )
    db_conn.commit()

    with pytest.raises(ResurrectRefused, match="runtime_cutover_required"):
        wake.resurrect_agent(database, event_bus, aid, resurrected_by="user")
    assert _status(db_conn, aid) == ("terminated", "hosted")
    assert _resources(db_conn, aid) == before
    assert wakes == []


async def test_closed_form_terminated_row_resurrects_through_its_terminate_receipt(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    wakes: list[tuple[int, str]],
    database: Database,
    event_bus: EventBus,
) -> None:
    generation, owner = uuid4(), uuid4()
    before = _retired(generation, owner) | {"frozen_by": 1}
    aid = _terminated(db_conn, before)
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


def _refused_locally(monkeypatch: pytest.MonkeyPatch, db: psycopg.Connection) -> tuple[int, int]:
    """A never-admitted row without the birth marker: unknown, so the real
    in-process op refuses and the chat stays queued."""
    aid = _terminated(db, None)
    trigger = insert_inbound_message(
        db,
        aid,
        "are you there?",
        "user",
        bus=EventBus.from_settings(),
        database=Database.from_settings(),
    )
    db.commit()

    async def _unreachable(*_a: object, **_kw: object) -> dict[str, Any]:
        raise ClusterOpUnreachable("local ops server not reachable")

    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _unreachable)
    return aid, trigger


def _refused_remotely(monkeypatch: pytest.MonkeyPatch, db: psycopg.Connection) -> tuple[int, int]:
    aid = _terminated(db, None)
    trigger = insert_inbound_message(
        db,
        aid,
        "are you there?",
        "user",
        bus=EventBus.from_settings(),
        database=Database.from_settings(),
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
) -> None:
    aid, trigger = arrange(monkeypatch, db_conn)
    status = await lifecycle.resurrect_if_terminated(
        Database.from_settings(),
        event_bus,
        aid,
        trigger_inbound_id=trigger,
        trigger_inbound_kind="chat",
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


async def test_other_auto_resurrect_failures_stay_informational(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, Any]],
    database: Database,
    event_bus: EventBus,
) -> None:
    aid = _terminated(db_conn, None)
    trigger = insert_inbound_message(
        db_conn, aid, "hello", "user", bus=event_bus, database=database
    )
    db_conn.commit()

    async def _failed(*_a: object, **_kw: object) -> dict[str, Any]:
        raise ClusterOpFailed({"error": "launch failed on the home machine"})

    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _failed)
    await lifecycle.resurrect_if_terminated(
        Database.from_settings(),
        event_bus,
        aid,
        trigger_inbound_id=trigger,
        trigger_inbound_kind="chat",
    )
    events = [r["extra"].get("event") for r in loguru_records if r["level"].name == "WARNING"]
    assert "auto_resurrect_refused" not in events
    assert any(r["extra"].get("event") == "auto_resurrect_failed" for r in loguru_records)


# ── Unowned termination ──────────────────────────────────────────────────────
# Only a force ends a row that has no runtime identity. When this runtime's own
# lifecycle left the row unowned (a birth, a resurrection, an applied restart),
# that force records a receipt and the row resurrects.

_Arrange = Callable[[psycopg.Connection, AsyncConnectionPool], Awaitable[int]]


def _force(aid: int) -> int:
    with ConnectionPool[psycopg.Connection](
        settings.data_plane.db_url, min_size=1, max_size=1, kwargs=PG_KEEPALIVE_KWARGS
    ) as pool:
        _, _, _, force = termination._force_terminate_transaction(aid, pool, source="user")
    return force


def _unowned_receipt(db: psycopg.Connection, command: int) -> bool:
    row = db.execute(
        "SELECT payload->'unowned_termination' FROM inbound_messages WHERE id=%s", (command,)
    ).fetchone()
    db.commit()
    return row == (True,)


def _unowned_idle(db: psycopg.Connection, aid: int) -> bool:
    row = db.execute(
        "SELECT status,runtime_kind,runtime_generation,runtime_owner,pid "
        "FROM agents_meta WHERE id=%s",
        (aid,),
    ).fetchone()
    db.commit()
    return row == ("idling", None, None, None, None)


def _legacy_row(db: psycopg.Connection) -> int:
    """A row no birth epoch vouches for: only a later lifecycle release can."""
    aid, _, _, _ = create_agent_row(
        Database.from_settings(), EventBus.from_settings(), spawner="user", machine=machine_name()
    )
    db.execute("UPDATE agents_meta SET last_resurrect_inbound_id=NULL WHERE id=%s", (aid,))
    db.commit()
    return aid


async def _admitted(pool: AsyncConnectionPool, aid: int) -> RuntimeIncarnation:
    owner = await admit_hosted_runtime(
        pool, aid, machine_name(), uuid4(), expected_from="idling", db=Database.from_settings()
    )
    assert owner is not None
    return owner


async def _spawned(db: psycopg.Connection, pool: AsyncConnectionPool) -> int:
    """(a) a new agent never admitted: its birth epoch is its origin."""
    aid, _, _, _ = create_agent_row(
        Database.from_settings(), EventBus.from_settings(), spawner="user", machine=machine_name()
    )
    return aid


async def _resurrected(db: psycopg.Connection, pool: AsyncConnectionPool) -> int:
    """(b) resurrected from its retained identity, not admitted again yet."""
    aid = _legacy_row(db)
    await _admitted(pool, aid)
    db.execute(
        "UPDATE agents_meta SET status='terminated',termination_source='user' WHERE id=%s",
        (aid,),
    )
    db.commit()
    wake.resurrect_agent(
        Database.from_settings(), EventBus.from_settings(), aid, resurrected_by="user"
    )
    return aid


async def _restarted(db: psycopg.Connection, pool: AsyncConnectionPool) -> int:
    """(c) released by its applied restart, no successor admitted yet."""
    aid = _legacy_row(db)
    owner = await _admitted(pool, aid)
    insert_inbound_message(
        db,
        aid,
        "",
        "user",
        kind="restart",
        bus=EventBus.from_settings(),
        database=Database.from_settings(),
    )
    db.commit()
    with bind_turn_identity(aid, incarnation=owner):
        await claim_inbound_batch(pool, aid)
        assert await apply_hosted_lifecycle(pool, owner, bus=EventBus.from_settings()) == "restart"
    return aid


@pytest.mark.parametrize("guarded", [False, True])
@pytest.mark.parametrize("arrange", [_spawned, _resurrected, _restarted])
async def test_a_row_this_runtime_left_and_ended_unowned_resurrects(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    wakes: list[tuple[int, str]],
    arrange: _Arrange,
    guarded: bool,
    database: Database,
    event_bus: EventBus,
) -> None:
    """Each way this runtime leaves a row unowned, then a force before the next
    admission: the force records the receipt, the row resurrects as a fresh
    hosted birth, and admission takes the successor."""
    aid = await arrange(db_conn, aops_pool)
    assert _unowned_idle(db_conn, aid)
    force = _force(aid)
    assert _unowned_receipt(db_conn, force)
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
        trigger_inbound_kind="chat" if guarded else None,
    )

    assert _unowned_idle(db_conn, aid)
    assert wakes == [(aid, "0")]
    assert await _admitted(aops_pool, aid)


async def test_a_managed_row_ended_unowned_keeps_its_predecessor_receipt(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
) -> None:
    """(b) with a recorded resource set: resurrection keeps the closed set, the
    force leaves the terminate receipt alone, and the successor is admitted
    through it."""
    generation, owner = uuid4(), uuid4()
    closed = IncarnationResources(generation=generation, owner=owner, requests={})
    aid = _terminated(db_conn, closed.model_dump(mode="json"))
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
    assert _unowned_receipt(db_conn, _force(aid))

    wake.resurrect_agent(database, event_bus, aid, resurrected_by="user")

    successor = await _admitted(aops_pool, aid)
    admitted = decode_resources(_resources(db_conn, aid))
    assert isinstance(admitted, IncarnationResources)
    assert (admitted.generation, admitted.owner) == (successor.generation, successor.owner)


def _legacy_unowned_forced(db: psycopg.Connection) -> int:
    """Unowned, but no lifecycle of this runtime left it so."""
    aid = _legacy_row(db)
    _force(aid)
    return aid


def _legacy_forced_beside_a_marked_neighbour(db: psycopg.Connection) -> int:
    """As above while another agent carries both facts: a lifecycle release
    (its resurrection) and an unowned termination receipt (the force before
    it). Both facts are per agent, so a neighbour's prove nothing here."""
    other, _, _, _ = create_agent_row(
        Database.from_settings(), EventBus.from_settings(), spawner="user", machine=machine_name()
    )
    db.commit()
    assert _unowned_receipt(db, _force(other))
    wake.resurrect_agent(
        Database.from_settings(), EventBus.from_settings(), other, resurrected_by="user"
    )
    released = db.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND kind='resurrect' "
        "AND payload->'lifecycle_release' = 'true'::jsonb",
        (other,),
    ).fetchone()
    db.commit()
    assert released == (1,)
    return _legacy_unowned_forced(db)


def _legacy_termination_swept(db: psycopg.Connection) -> int:
    """Terminated with no identity and no receipt, as every row the cutover
    inherits: a later force (a machine-pause sweep) finds it terminated already
    and records nothing, whatever the row's origin."""
    aid = _terminated(db, None)
    _force(aid)
    return aid


def _partial_identity_forced(db: psycopg.Connection) -> int:
    """A born row whose identity is not empty: a historical process kind."""
    aid, _, _, _ = create_agent_row(
        Database.from_settings(), EventBus.from_settings(), spawner="user", machine=machine_name()
    )
    db.execute("UPDATE agents_meta SET runtime_kind='process' WHERE id=%s", (aid,))
    db.commit()
    _force(aid)
    return aid


def _earlier_life_receipt(db: psycopg.Connection) -> int:
    """A receipt ended an earlier life; this life ended without one."""
    aid, _, _, _ = create_agent_row(
        Database.from_settings(), EventBus.from_settings(), spawner="user", machine=machine_name()
    )
    _force(aid)
    wake.resurrect_agent(
        Database.from_settings(), EventBus.from_settings(), aid, resurrected_by="user"
    )
    db.execute(
        "UPDATE agents_meta SET status='terminated',termination_source='user' WHERE id=%s",
        (aid,),
    )
    db.commit()
    return aid


@pytest.mark.parametrize(
    "arrange",
    [
        _legacy_unowned_forced,
        _legacy_forced_beside_a_marked_neighbour,
        _legacy_termination_swept,
        _partial_identity_forced,
        _earlier_life_receipt,
    ],
)
def test_an_unowned_end_without_this_lifes_receipt_still_refuses(
    db_conn: psycopg.Connection,
    wakes: list[tuple[int, str]],
    arrange: Callable[[psycopg.Connection], int],
    database: Database,
    event_bus: EventBus,
) -> None:
    aid = arrange(db_conn)
    receipts = db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND kind='terminate' "
        "AND id > COALESCE((SELECT last_resurrect_inbound_id FROM agents_meta WHERE id=%s), 0) "
        "AND payload ? 'unowned_termination'",
        (aid, aid),
    ).fetchone()
    db_conn.commit()
    assert receipts == (0,)
    wakes.clear()

    with pytest.raises(ResurrectRefused, match="runtime_cutover_required"):
        wake.resurrect_agent(database, event_bus, aid, resurrected_by="user")

    assert _status(db_conn, aid)[0] == "terminated"
    assert wakes == []


async def test_a_force_on_a_live_incarnation_records_no_unowned_receipt(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
) -> None:
    """An idle agent its host still owns: the force targets that incarnation,
    and resurrection waits for the original host to observe it, whatever origin
    the row has."""
    aid, _, _, _ = create_agent_row(database, event_bus, spawner="user", machine=machine_name())
    await _admitted(aops_pool, aid)
    db_conn.execute("UPDATE agents_meta SET status='idling' WHERE id=%s", (aid,))
    db_conn.commit()

    force = _force(aid)

    assert not _unowned_receipt(db_conn, force)
    with pytest.raises(ResurrectSettlementDeferredError):
        wake.resurrect_agent(database, event_bus, aid, resurrected_by="user")
    assert _status(db_conn, aid) == ("terminated", "hosted")


async def test_maintenance_parks_a_resurrected_unowned_row_and_ignores_its_receipt(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: Database,
    event_bus: EventBus,
) -> None:
    """A resurrected row is the unowned idle row it was before the force, which
    the drain parks; a terminated row carrying a receipt is outside its cohort."""
    monkeypatch.setattr(pause_owner, "state_path", lambda: tmp_path / "pause.json")
    monkeypatch.setattr(pause_owner, "lock_path", lambda: tmp_path / "pause.lock")
    resurrected = await _restarted(db_conn, aops_pool)
    _force(resurrected)
    wake.resurrect_agent(database, event_bus, resurrected, resurrected_by="user")
    ended, _, _, _ = create_agent_row(database, event_bus, spawner="user", machine=machine_name())
    assert _unowned_receipt(db_conn, _force(ended))
    when = datetime(2026, 9, 29, 3, 0, tzinfo=UTC)
    holder = "ops:test:unowned"
    pause_owner.begin_maintenance(holder, when)

    hold = cohort.prepare(
        db_conn,
        machine=machine_name(),
        host_owner=None,
        holder=holder,
        acquired_at=when,
    )

    assert hold.phase == "draining"
    assert hold.parked == (resurrected,)
    assert hold.commands == {}
