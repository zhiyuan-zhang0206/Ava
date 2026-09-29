"""Retired-writer rows: refused until the cutover closes their predecessor, then
admitted exactly once by the ordinary successor rule, and drainable afterwards."""

from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.db import claim_inbound_batch
from agent.hosted_ownership import admit_hosted_runtime, apply_hosted_lifecycle
from ops.agent_spawn import create_agent_row
from services.agent_host.maintenance import record_drained
from shared import maintenance
from shared.db import insert_inbound_message
from shared.incarnation_resources import (
    IncarnationResources,
    ResourceEvidenceError,
    ResourceShapeError,
    decode_resources,
)
from shared.machine import machine_name
from shared.maintenance_cohort import _applied_capture, verify_drained
from shared.maintenance_state import MaintenanceHold
from shared.native_process.runtime_incarnation import RuntimeIncarnation
from shared.native_process.turn_identity import bind_turn_identity
from shared.predecessor_closure import ClosureEvidence, close_retired_predecessor

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


def _evidence(**changes: str) -> ClosureEvidence:
    fields = {
        "machine": machine_name(),
        "attestation_sha256": "b" * 64,
        "operator": "cutover-operator",
        "reason": "FC-4 incarnation reconciliation",
    }
    return ClosureEvidence(**(fields | changes))


def _drained(db: psycopg.Connection, **resources: bool) -> tuple[int, int, dict[str, Any]]:
    """An idle agent exactly as the retired drain left it: the applied restart
    receipt is still the lifecycle pointer and the old owner was released."""
    aid, _, _, _ = create_agent_row(spawner="user", machine=machine_name())
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


def _close(db: psycopg.Connection, aid: int, receipt: int, before: object) -> Any:
    with db.transaction():
        return close_retired_predecessor(
            db, aid, before=before, receipt=receipt, evidence=_evidence()
        )


async def _admit(pool: AsyncConnectionPool, aid: int, owner: UUID) -> RuntimeIncarnation | None:
    return await admit_hosted_runtime(pool, aid, machine_name(), owner, expected_from="idling")


def test_retired_shape_is_a_typed_refusal() -> None:
    with pytest.raises(ResourceShapeError, match="cutover reconciliation"):
        decode_resources(_retired(uuid4(), uuid4()))


def _warnings(records: list[dict[str, Any]]) -> list[str]:
    return [record["message"] for record in records if record["level"].name == "WARNING"]


async def test_unconverted_retired_row_is_a_recorded_loud_refusal(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    loguru_records: list[dict[str, Any]],
) -> None:
    aid, receipt, before = _drained(db_conn)
    unchanged = _snapshot(db_conn, aid, receipt)

    assert await _admit(aops_pool, aid, uuid4()) is None

    after = _snapshot(db_conn, aid, receipt)
    assert after[0]["last_admission_outcome"] == "resource_fence"
    assert after[0]["incarnation_resources"] == before
    assert after[1] == unchanged[1]
    assert [m for m in _warnings(loguru_records) if "cutover reconciliation" in m] != []


async def test_converted_row_is_admitted_exactly_once(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    aid, receipt, before = _drained(db_conn)
    closure = _close(db_conn, aid, receipt, before)
    assert closure.after == IncarnationResources(
        generation=UUID(before["generation"]), owner=UUID(before["owner"]), requests={}
    )
    converted = _snapshot(db_conn, aid, receipt)
    assert decode_resources(converted[0]["incarnation_resources"]) == closure.after
    recorded = converted[1]["payload"]["cutover_closure"]
    assert (recorded["before"], recorded["attestation_sha256"], recorded["operator"]) == (
        before,
        "b" * 64,
        "cutover-operator",
    )

    successor = await _admit(aops_pool, aid, uuid4())
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
    # owner to consume, and a replayed conversion no longer matches the before image.
    with pytest.raises(ResourceEvidenceError, match="predecessor resource/lifecycle closure"):
        await _admit(aops_pool, aid, uuid4())
    with pytest.raises(ResourceEvidenceError, match="before image"):
        _close(db_conn, aid, receipt, before)


async def test_closed_form_without_its_receipt_is_not_admissible(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    aid, receipt, before = _drained(db_conn)
    _close(db_conn, aid, receipt, before)
    # Withdraw the receipt: the same bytes without a settled decision prove nothing.
    db_conn.execute("UPDATE inbound_messages SET applied_at=NULL WHERE id=%s", (receipt,))
    db_conn.commit()
    with pytest.raises(ResourceEvidenceError, match="predecessor resource/lifecycle closure"):
        await _admit(aops_pool, aid, uuid4())


def _null_row(db: psycopg.Connection, aid: int, receipt: int, before: dict[str, Any]) -> object:
    db.execute("UPDATE agents_meta SET incarnation_resources=NULL WHERE id=%s", (aid,))
    return None


def _current_row(db: psycopg.Connection, aid: int, receipt: int, before: dict[str, Any]) -> object:
    current = IncarnationResources(
        generation=UUID(before["generation"]), owner=UUID(before["owner"]), requests={}
    ).model_dump(mode="json")
    db.execute("UPDATE agents_meta SET incarnation_resources=%s WHERE id=%s", (Jsonb(current), aid))
    return current


def _stale_before(db: psycopg.Connection, aid: int, receipt: int, before: dict[str, Any]) -> object:
    return before | {"frozen_by": 7}


def _unapplied(db: psycopg.Connection, aid: int, receipt: int, before: dict[str, Any]) -> object:
    db.execute("UPDATE inbound_messages SET applied_at=NULL WHERE id=%s", (receipt,))
    return before


def _other_target(db: psycopg.Connection, aid: int, receipt: int, before: dict[str, Any]) -> object:
    db.execute("UPDATE inbound_messages SET target_generation=%s WHERE id=%s", (uuid4(), receipt))
    return before


def _pointer_elsewhere(
    db: psycopg.Connection, aid: int, receipt: int, before: dict[str, Any]
) -> object:
    other = insert_inbound_message(db, aid, "", "user", kind="restart")
    db.execute("UPDATE agents_meta SET lifecycle_command_id=%s WHERE id=%s", (other, aid))
    return before


def _live_owner(db: psycopg.Connection, aid: int, receipt: int, before: dict[str, Any]) -> object:
    db.execute(
        "UPDATE agents_meta SET runtime_kind='hosted',runtime_generation=%s,runtime_owner=%s,"
        "lease_expires_at=clock_timestamp()+interval '1 minute' WHERE id=%s",
        (before["generation"], before["owner"], aid),
    )
    return before


def _replayed(db: psycopg.Connection, aid: int, receipt: int, before: dict[str, Any]) -> object:
    db.execute(
        "UPDATE inbound_messages SET payload=payload||'{\"cutover_closure\":{}}'::jsonb "
        "WHERE id=%s",
        (receipt,),
    )
    return before


_Tamper = Callable[[psycopg.Connection, int, int, dict[str, Any]], object]


@pytest.mark.parametrize(
    ("tamper", "refusal"),
    [
        (_null_row, "NULL"),
        (_current_row, "current resource evidence"),
        (_stale_before, "before image"),
        (_unapplied, "unsettled command"),
        (_other_target, "unsettled command"),
        (_pointer_elsewhere, "unsettled command"),
        (_live_owner, "live or different incarnation"),
        (_replayed, "already carries a cutover closure"),
    ],
)
def test_conversion_refuses_unproven_or_contradicting_evidence(
    db_conn: psycopg.Connection, tamper: _Tamper, refusal: str
) -> None:
    aid, receipt, before = _drained(db_conn)
    supplied = tamper(db_conn, aid, receipt, before)
    db_conn.commit()
    unchanged = _snapshot(db_conn, aid, receipt)
    with pytest.raises(ResourceEvidenceError, match=refusal):
        _close(db_conn, aid, receipt, supplied)
    assert _snapshot(db_conn, aid, receipt) == unchanged


def _ended(
    db: psycopg.Connection,
    *,
    status: str = "terminated",
    kind: str | None = "hosted",
    runtime: bool = True,
    pointer: bool = False,
) -> tuple[int, int, dict[str, Any]]:
    """An agent whose recorded incarnation ended through an applied and observed
    terminate, shaped by the fields resurrection and admission read."""
    aid, _, _, _ = create_agent_row(spawner="user", machine=machine_name())
    before = _retired(uuid4(), uuid4())
    receipt = db.execute(
        "INSERT INTO inbound_messages(agent_id,kind,source,content,status,claimed_at,applied_at,"
        "observed_at,target_generation,target_owner) VALUES(%s,'terminate','user','','done',"
        "now(),now(),now(),%s,%s) RETURNING id",
        (aid, before["generation"], before["owner"]),
    ).fetchone()
    assert receipt is not None
    identity = (before["generation"], before["owner"]) if runtime else (None, None)
    db.execute(
        "UPDATE agents_meta SET status=%s,termination_source=CASE WHEN %s='terminated' "
        "THEN 'user' END,runtime_kind=%s,runtime_generation=%s,runtime_owner=%s,"
        "lease_expires_at=NULL,lifecycle_command_id=%s,incarnation_resources=%s WHERE id=%s",
        (
            status,
            status,
            kind,
            *identity,
            receipt[0] if pointer else None,
            Jsonb(before),
            aid,
        ),
    )
    db.commit()
    return aid, receipt[0], before


@pytest.mark.parametrize(
    ("shape", "refusal"),
    [
        ({"kind": None, "runtime": False}, "released its runtime identity"),
        ({"kind": None}, "no hosted runtime kind"),
        ({"pointer": True}, "still points at its receipt"),
        (
            {"status": "idling", "kind": None, "runtime": False, "pointer": True},
            "admission clears only a restart pointer",
        ),
    ],
    ids=["terminated-released", "terminated-kindless", "terminated-pointer", "idling-pointer"],
)
def test_conversion_refuses_a_row_no_successor_would_take(
    db_conn: psycopg.Connection, shape: dict[str, Any], refusal: str
) -> None:
    """The guard is the successor's own rule: a terminated row resurrects only
    with its closed hosted incarnation and no lifecycle pointer, and admission
    observes only a restart pointer. Converting any other shape would leave the
    agent fenced with no conversion left to retry."""
    aid, receipt, before = _ended(db_conn, **shape)
    unchanged = _snapshot(db_conn, aid, receipt)
    with pytest.raises(ResourceEvidenceError, match=refusal):
        _close(db_conn, aid, receipt, before)
    assert _snapshot(db_conn, aid, receipt) == unchanged


async def test_a_resurrected_row_never_readmitted_converts_through_its_terminate_receipt(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    """Resurrection released the owner and left no pointer; the successor's
    admission consumes the closed form through the observed terminate."""
    aid, receipt, before = _ended(db_conn, status="idling", kind=None, runtime=False)
    _close(db_conn, aid, receipt, before)
    successor = await _admit(aops_pool, aid, uuid4())
    assert successor is not None
    admitted = decode_resources(_snapshot(db_conn, aid, receipt)[0]["incarnation_resources"])
    assert isinstance(admitted, IncarnationResources)
    assert (admitted.generation, admitted.owner) == (successor.generation, successor.owner)


def test_conversion_requires_the_attested_machine_and_a_transaction(
    db_conn: psycopg.Connection,
) -> None:
    aid, receipt, before = _drained(db_conn)
    unchanged = _snapshot(db_conn, aid, receipt)
    with pytest.raises(ResourceEvidenceError, match="another machine"), db_conn.transaction():
        close_retired_predecessor(
            db_conn, aid, before=before, receipt=receipt, evidence=_evidence(machine="elsewhere")
        )
    with pytest.raises(ResourceEvidenceError, match="explicit transaction"):
        close_retired_predecessor(
            db_conn, aid, before=before, receipt=receipt, evidence=_evidence()
        )
    db_conn.rollback()
    assert _snapshot(db_conn, aid, receipt) == unchanged


def test_retired_open_allocations_close_only_on_the_attestation(
    db_conn: psycopg.Connection,
) -> None:
    """Recorded retired allocations are not re-parsed: the machine attestation
    is the allocation-closure proof, recorded verbatim with the before image."""
    aid, receipt, before = _drained(db_conn, open_request=True)
    closure = _close(db_conn, aid, receipt, before)
    assert closure.after.requests == {}
    recorded = db_conn.execute(
        "SELECT payload->'cutover_closure'->'before' FROM inbound_messages WHERE id=%s",
        (receipt,),
    ).fetchone()
    db_conn.commit()
    assert recorded == (before,)


async def test_admitted_successor_drains_with_its_complete_recorded_set(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The next release drains a converted agent: a restart released by the
    managed host leaves the complete empty set of exactly that incarnation,
    which the host receipt, a preparation retry and certification all accept."""
    aid, receipt, before = _drained(db_conn)
    _close(db_conn, aid, receipt, before)
    incarnation = await _admit(aops_pool, aid, uuid4())
    assert incarnation is not None
    row = db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,kind,source,content,payload) "
        "VALUES(%s,'restart','system:maintenance','',%s) RETURNING id",
        (aid, Jsonb(_DRAIN)),
    ).fetchone()
    db_conn.commit()
    assert row is not None
    command = row[0]
    with bind_turn_identity(aid, incarnation=incarnation):
        assert [item.id for item in await claim_inbound_batch(aops_pool, aid)] == [command]
        assert await apply_hosted_lifecycle(aops_pool, incarnation) == "restart"

    recorded: list[tuple[int, int]] = []

    def pending(_agent: int) -> int:
        return command

    def record(agent: int, cmd: int) -> None:
        recorded.append((agent, cmd))

    monkeypatch.setattr(maintenance, "pending_command", pending)
    monkeypatch.setattr(maintenance, "record_drained", record)
    await record_drained(aops_pool, incarnation.owner, aid)
    assert recorded == [(aid, command)]
    hold = MaintenanceHold("drained", {aid: command}, drained=(aid,))
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
