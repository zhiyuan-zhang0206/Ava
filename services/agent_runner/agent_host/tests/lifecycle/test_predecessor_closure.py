"""Retired-writer rows refuse. The closed-predecessor form (the incarnation a
retired value names, with no host identity and an empty set) is admitted
exactly once by the ordinary successor rule, and drainable afterwards."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.db import claim_inbound_batch
from agent.ownership.hosted import admit_hosted_runtime, apply_hosted_lifecycle
from base.agents.incarnation.resources import (
    IncarnationResources,
    ResourceEvidenceError,
    ResourceShapeError,
    decode_resources,
)
from base.cluster.machine import machine_name
from base.db import Database
from base.deploy.maintenance import admission
from base.deploy.maintenance.cohort import _applied_capture, verify_drained
from base.deploy.maintenance.state import MaintenanceHold, MaintenancePhase
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
from ops.agents.spawn import create_agent_row
from services.agent_runner.agent_host.maintenance import record_drained

_DRAIN = {
    "maintenance": {"holder": "legacy-host:pid41", "acquired_at": "2026-09-27T01:00:00+00:00"}
}


def _retired(generation: UUID, owner: UUID, *, open_request: bool = False) -> dict[str, Any]:
    """`IncarnationResources.model_dump(mode="json")` as the retired runtime wrote
    it: process receipts carry only pid and wall birth, no boot scope."""
    requests: dict[str, Any] = {}
    if open_request:
        request = str(uuid4())
        requests[request] = {
            "request": request,
            "domain": str(uuid4()),
            "request_digest": "a" * 64,
            "deadline": "2026-09-26T23:00:00Z",
            "owner_process": {"pid": 4243, "birth": 1758900001.5},
            "root_process": {"pid": 4244, "birth": 1758900002.5},
        }
    return {
        "version": 1,
        "state": "admitted",
        "generation": str(generation),
        "owner": str(owner),
        "host_process": {"pid": 4242, "birth": 1758900000.25},
        "frozen_by": None,
        "requests": requests,
    }


def _drained(db: psycopg.Connection, **resources: bool) -> tuple[int, int, dict[str, Any]]:
    """An idle agent exactly as the retired drain left it: the applied restart
    receipt is still the lifecycle pointer and the old owner was released."""
    aid, _, _, _ = create_agent_row(
        Database.from_settings(), EventBus.from_settings(), spawner="user", machine=machine_name()
    )
    before = _retired(uuid4(), uuid4(), **resources)
    receipt = db.execute(
        "INSERT INTO inbound_messages(agent_id,kind,source,content,payload,status,claimed_at,"
        "applied_at,target_generation,target_owner) VALUES(%s,'restart','system:maintenance',"
        "'',%s,'claimed',now(),now(),%s,%s) RETURNING id",
        (aid, Jsonb(_DRAIN), before["generation"], before["owner"]),
    ).fetchone()
    assert receipt is not None
    db.execute(
        "UPDATE agents_meta SET status='idling',runtime_kind=NULL,runtime_generation=NULL,"
        "runtime_owner=NULL,lease_expires_at=NULL,lifecycle_command_id=%s,"
        "incarnation_resources=%s WHERE id=%s",
        (receipt[0], Jsonb(before), aid),
    )
    db.commit()
    return aid, receipt[0], before


def _snapshot(db: psycopg.Connection, aid: int, receipt: int) -> tuple[Any, ...]:
    row = db.execute(
        "SELECT to_jsonb(m) - 'status_changed_at', to_jsonb(i) FROM agents_meta m "
        "JOIN inbound_messages i ON i.id=%s WHERE m.id=%s",
        (receipt, aid),
    ).fetchone()
    db.commit()
    assert row is not None
    return row


def _closed_form(db: psycopg.Connection, aid: int, before: dict[str, Any]) -> IncarnationResources:
    """Store the closed-predecessor form of the incarnation `before` names."""
    closed = IncarnationResources(
        generation=UUID(before["generation"]), owner=UUID(before["owner"]), requests={}
    )
    db.execute(
        "UPDATE agents_meta SET incarnation_resources=%s WHERE id=%s",
        (Jsonb(closed.model_dump(mode="json")), aid),
    )
    db.commit()
    return closed


async def _admit(pool: AsyncConnectionPool, aid: int, owner: UUID) -> RuntimeIncarnation | None:
    return await admit_hosted_runtime(
        pool, aid, machine_name(), owner, expected_from="idling", db=Database.from_settings()
    )


def test_retired_shape_is_a_typed_refusal() -> None:
    with pytest.raises(ResourceShapeError, match="cutover reconciliation"):
        decode_resources(_retired(uuid4(), uuid4()))


def _warnings(records: list[dict[str, Any]]) -> list[str]:
    return [record["message"] for record in records if record["level"].name == "WARNING"]


async def test_retired_row_is_a_recorded_loud_refusal(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    loguru_records: list[dict[str, Any]],
) -> None:
    aid, receipt, before = _drained(db_conn)
    unchanged = _snapshot(db_conn, aid, receipt)

    assert (
        await _admit(
            aops_pool,
            aid,
            uuid4(),
        )
        is None
    )

    after = _snapshot(db_conn, aid, receipt)
    assert after[0]["last_admission_outcome"] == "resource_fence"
    assert after[0]["incarnation_resources"] == before
    assert after[1] == unchanged[1]
    assert [m for m in _warnings(loguru_records) if "cutover reconciliation" in m] != []


async def test_closed_form_is_admitted_exactly_once(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    aid, receipt, before = _drained(db_conn)
    closed = _closed_form(db_conn, aid, before)
    stored = _snapshot(db_conn, aid, receipt)
    assert decode_resources(stored[0]["incarnation_resources"]) == closed

    successor = await _admit(
        aops_pool,
        aid,
        uuid4(),
    )
    assert successor is not None
    admitted_row = _snapshot(db_conn, aid, receipt)
    admitted = decode_resources(admitted_row[0]["incarnation_resources"])
    assert isinstance(admitted, IncarnationResources)
    assert (admitted.generation, admitted.owner) == (successor.generation, successor.owner)
    assert admitted.host_process is not None
    assert admitted.requests == {}
    # The successor observed the restart receipt: the form is consumed.
    assert admitted_row[0]["lifecycle_command_id"] is None
    assert admitted_row[1]["status"] == "done"

    # Exactly once: the successor's own set has no closure receipt for another
    # owner to consume.
    with pytest.raises(ResourceEvidenceError, match="predecessor resource/lifecycle closure"):
        await _admit(
            aops_pool,
            aid,
            uuid4(),
        )


async def test_closed_form_without_its_receipt_is_not_admissible(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    aid, receipt, before = _drained(db_conn)
    _closed_form(db_conn, aid, before)
    # Withdraw the receipt: the same bytes without a settled decision prove nothing.
    db_conn.execute("UPDATE inbound_messages SET applied_at=NULL WHERE id=%s", (receipt,))
    db_conn.commit()
    with pytest.raises(ResourceEvidenceError, match="predecessor resource/lifecycle closure"):
        await _admit(
            aops_pool,
            aid,
            uuid4(),
        )


def _resurrected(db: psycopg.Connection) -> tuple[int, int, dict[str, Any]]:
    """An agent whose recorded incarnation ended through an applied and observed
    terminate, then resurrected and never readmitted: idling, its owner
    released, no lifecycle pointer, the retired value still stored."""
    aid, _, _, _ = create_agent_row(
        Database.from_settings(), EventBus.from_settings(), spawner="user", machine=machine_name()
    )
    before = _retired(uuid4(), uuid4())
    receipt = db.execute(
        "INSERT INTO inbound_messages(agent_id,kind,source,content,status,claimed_at,applied_at,"
        "observed_at,target_generation,target_owner) VALUES(%s,'terminate','user','','done',"
        "now(),now(),now(),%s,%s) RETURNING id",
        (aid, before["generation"], before["owner"]),
    ).fetchone()
    assert receipt is not None
    db.execute(
        "UPDATE agents_meta SET status='idling',runtime_kind=NULL,runtime_generation=NULL,"
        "runtime_owner=NULL,lease_expires_at=NULL,lifecycle_command_id=NULL,"
        "incarnation_resources=%s WHERE id=%s",
        (Jsonb(before), aid),
    )
    db.commit()
    return aid, receipt[0], before


async def test_a_resurrected_row_never_readmitted_is_admitted_through_its_terminate_receipt(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    """Resurrection released the owner and left no pointer; the successor's
    admission consumes the closed form through the observed terminate."""
    aid, receipt, before = _resurrected(db_conn)
    _closed_form(db_conn, aid, before)
    successor = await _admit(
        aops_pool,
        aid,
        uuid4(),
    )
    assert successor is not None
    admitted = decode_resources(_snapshot(db_conn, aid, receipt)[0]["incarnation_resources"])
    assert isinstance(admitted, IncarnationResources)
    assert (admitted.generation, admitted.owner) == (successor.generation, successor.owner)


async def test_admitted_successor_drains_with_its_complete_recorded_set(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    event_bus: EventBus,
) -> None:
    """The next release drains a converted agent: a restart released by the
    managed host leaves the complete empty set of exactly that incarnation,
    which the host receipt, a preparation retry and certification all accept."""
    aid, _receipt, before = _drained(db_conn)
    _closed_form(db_conn, aid, before)
    incarnation = await _admit(
        aops_pool,
        aid,
        uuid4(),
    )
    assert incarnation is not None
    row = db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,kind,source,content,payload) "
        "VALUES(%s,'restart','system:maintenance','',%s) RETURNING id",
        (aid, Jsonb(_DRAIN)),
    ).fetchone()
    db_conn.commit()
    assert row is not None
    command = row[0]
    assert [
        item.id
        for item in await claim_inbound_batch(aops_pool, aid, incarnation=incarnation, work=None)
    ] == [command]
    assert (
        await apply_hosted_lifecycle(aops_pool, incarnation, bus=event_bus, resources=None)
        == "restart"
    )

    recorded: list[tuple[int, int]] = []

    def pending(_agent: int) -> int:
        return command

    def record(agent: int, cmd: int) -> None:
        recorded.append((agent, cmd))

    monkeypatch.setattr(admission, "pending_command", pending)
    monkeypatch.setattr(admission, "record_drained", record)
    await record_drained(aops_pool, incarnation.owner, aid)
    assert recorded == [(aid, command)]
    hold = MaintenanceHold(MaintenancePhase.DRAINED, {aid: command}, drained=(aid,))
    operation = _DRAIN["maintenance"]
    assert _applied_capture(
        db_conn,
        hold,
        incarnation.owner,
        operation["holder"],
        datetime.fromisoformat(operation["acquired_at"]),
    ) == {aid}
    verify_drained(db_conn, hold)
    db_conn.commit()

    # An unclosed or foreign set is not a drained one.
    resources = db_conn.execute(
        "SELECT incarnation_resources FROM agents_meta WHERE id=%s", (aid,)
    ).fetchone()
    assert resources is not None
    for changed in (
        resources[0] | {"frozen_by": command},
        resources[0] | {"owner": str(uuid4())},
    ):
        db_conn.execute(
            "UPDATE agents_meta SET incarnation_resources=%s WHERE id=%s", (Jsonb(changed), aid)
        )
        with pytest.raises(RuntimeError, match="drained restart pointer"):
            verify_drained(db_conn, hold)
        db_conn.rollback()
