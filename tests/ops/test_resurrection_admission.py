"""Resurrection needs proven closure or proven non-admission, and a refusal is loud."""

from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.hosted_ownership import admit_hosted_runtime
from ops import agent_wake, ops_lifecycle
from ops.agent_spawn import create_agent_row
from ops.cluster_rpc import ClusterOpFailed, ClusterOpUnreachable
from shared.agents import AgentStatus, ResurrectError, ResurrectRefused
from shared.db import insert_inbound_message
from shared.incarnation_resources import IncarnationResources, ResourceBirth, decode_resources
from shared.machine import machine_name
from shared.predecessor_closure import ClosureEvidence, close_retired_predecessor
from tests.shared.test_predecessor_closure import _retired


@pytest.fixture(autouse=True)
def wakes(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[int, str]]]:
    captured: list[tuple[int, str]] = []

    def _record(agent_id: int, payload: str) -> None:
        captured.append((agent_id, payload))

    monkeypatch.setattr(agent_wake, "publish_inbound_wake", _record)
    yield captured


def _terminated(db: psycopg.Connection, resources: object) -> int:
    aid, _, _, _ = create_agent_row(spawner="user", machine=machine_name())
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
) -> None:
    """No runtime identity plus the unconsumed fresh-INSERT marker proves no
    predecessor allocation exists: resurrection is a fresh birth."""
    marker = ResourceBirth(birth=uuid4()).model_dump(mode="json")
    aid = _terminated(db_conn, marker)
    trigger = insert_inbound_message(db_conn, aid, "continue", "user") if guarded else None
    db_conn.commit()

    agent_wake.resurrect_agent(
        aid,
        resurrected_by="system" if guarded else "user",
        trigger_inbound_id=trigger,
        trigger_inbound_kind="chat" if guarded else None,
    )

    assert _status(db_conn, aid) == ("idling", None)
    assert _resources(db_conn, aid) == marker
    assert wakes == [(aid, "0")]
    incarnation = await admit_hosted_runtime(
        aops_pool, aid, machine_name(), uuid4(), expected_from="idling"
    )
    assert incarnation is not None
    admitted = decode_resources(_resources(db_conn, aid))
    assert isinstance(admitted, IncarnationResources)
    assert (admitted.generation, admitted.owner) == (incarnation.generation, incarnation.owner)


@pytest.mark.parametrize("trigger", [False, True])
def test_fresh_birth_transition_reproves_the_marker_under_the_row_lock(
    db_conn: psycopg.Connection, trigger: bool
) -> None:
    """The final CAS, not only the earlier read, requires the unconsumed marker."""
    aid = _terminated(db_conn, None)
    chat = insert_inbound_message(db_conn, aid, "continue", "user") if trigger else None
    db_conn.commit()
    with db_conn.cursor() as cur, pytest.raises(ResurrectError, match="0 rows"):
        agent_wake._transition_terminated_to_unclaimed_idling(
            cur,
            aid,
            None,
            trigger_inbound_id=chat,
            trigger_inbound_kind="chat" if trigger else None,
        )
    db_conn.rollback()
    assert _status(db_conn, aid) == ("terminated", None)


def test_retired_resources_require_cutover_before_resurrection(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, wakes: list[tuple[int, str]]
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
        agent_wake.resurrect_agent(aid, resurrected_by="user")
    assert _status(db_conn, aid) == ("terminated", "hosted")
    assert _resources(db_conn, aid) == before
    assert wakes == []


async def test_converted_terminated_row_resurrects_through_its_terminate_receipt(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, wakes: list[tuple[int, str]]
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
    evidence = ClosureEvidence(
        machine=machine_name(), attestation_sha256="c" * 64, operator="op", reason="FC-4"
    )
    with db_conn.transaction():
        close_retired_predecessor(
            db_conn, aid, before=before, receipt=receipt[0], evidence=evidence
        )

    agent_wake.resurrect_agent(aid, resurrected_by="user")
    successor = await admit_hosted_runtime(
        aops_pool, aid, machine_name(), uuid4(), expected_from="idling"
    )
    assert successor is not None
    admitted = decode_resources(_resources(db_conn, aid))
    assert isinstance(admitted, IncarnationResources)
    assert admitted.owner == successor.owner != owner


def _refused_locally(monkeypatch: pytest.MonkeyPatch, db: psycopg.Connection) -> tuple[int, int]:
    """A never-admitted row without the birth marker: unknown, so the real
    in-process op refuses and the chat stays queued."""
    aid = _terminated(db, None)
    trigger = insert_inbound_message(db, aid, "are you there?", "user")
    db.commit()

    async def _unreachable(*_a: object, **_kw: object) -> dict[str, Any]:
        raise ClusterOpUnreachable("local ops server not reachable")

    monkeypatch.setattr(ops_lifecycle._cluster_rpc, "dispatch_to_machine", _unreachable)
    return aid, trigger


def _refused_remotely(monkeypatch: pytest.MonkeyPatch, db: psycopg.Connection) -> tuple[int, int]:
    aid = _terminated(db, None)
    trigger = insert_inbound_message(db, aid, "are you there?", "user")
    db.commit()

    async def _failed(*_a: object, **_kw: object) -> dict[str, Any]:
        raise ClusterOpFailed({"error": "ResurrectRefused: runtime_cutover_required"})

    monkeypatch.setattr(ops_lifecycle._cluster_rpc, "dispatch_to_machine", _failed)
    return aid, trigger


@pytest.mark.parametrize("arrange", [_refused_locally, _refused_remotely])
async def test_auto_resurrect_refusal_is_a_warning_naming_the_reason(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, Any]],
    arrange: Any,
) -> None:
    aid, trigger = arrange(monkeypatch, db_conn)
    status = await ops_lifecycle.resurrect_if_terminated(
        aid, trigger_inbound_id=trigger, trigger_inbound_kind="chat"
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
) -> None:
    aid = _terminated(db_conn, None)
    trigger = insert_inbound_message(db_conn, aid, "hello", "user")
    db_conn.commit()

    async def _failed(*_a: object, **_kw: object) -> dict[str, Any]:
        raise ClusterOpFailed({"error": "launch failed on the home machine"})

    monkeypatch.setattr(ops_lifecycle._cluster_rpc, "dispatch_to_machine", _failed)
    await ops_lifecycle.resurrect_if_terminated(
        aid, trigger_inbound_id=trigger, trigger_inbound_kind="chat"
    )
    events = [r["extra"].get("event") for r in loguru_records if r["level"].name == "WARNING"]
    assert "auto_resurrect_refused" not in events
    assert any(r["extra"].get("event") == "auto_resurrect_failed" for r in loguru_records)
