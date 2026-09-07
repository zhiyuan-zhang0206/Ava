"""Real database resurrection: guarded status transition, durable inbound, and one host wake."""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from ops import agent_wake
from ops.agent_spawn import create_agent_row
from shared.machine import machine_name

_DEAD_PID = 424243


@pytest.fixture
def wakes() -> Iterator[list[tuple[int, str]]]:
    captured: list[tuple[int, str]] = []
    yield captured


@pytest.fixture(autouse=True)
def _capture_wakes(monkeypatch: pytest.MonkeyPatch, wakes: list[tuple[int, str]]) -> Iterator[None]:
    def _record(agent_id: int, payload: str) -> None:
        wakes.append((agent_id, payload))

    # Both wake modules publish through their own namespace (the split moved
    # swap-in / revive to `ops.agent_revive`), so capture both.
    monkeypatch.setattr(agent_wake, "publish_inbound_wake", _record)
    yield


def _park(
    db: psycopg.Connection,
    *,
    status: str,
    pid: int | None = None,
) -> int:
    """Seed a row WITHOUT any launch — hosted spawn is row-only, and the
    guard above turns a stray process launch into a loud failure."""
    aid, _birth = create_agent_row(spawner="user", machine=machine_name())
    with db.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status=%s, pid=%s WHERE id=%s", (status, pid, aid))
    db.commit()
    return aid


def _row(db: psycopg.Connection, aid: int) -> tuple[str, int | None]:
    with db.cursor() as cur:
        cur.execute("SELECT status, pid FROM agents_meta WHERE id = %s", (aid,))
        row = cur.fetchone()
    assert row is not None, f"agents_meta row {aid} missing"
    return row[0], row[1]


def _kind_rows(db: psycopg.Connection, aid: int, kind: str) -> int:
    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM inbound_messages WHERE agent_id = %s AND kind = %s",
            (aid, kind),
        )
        row = cur.fetchone()
    assert row is not None, f"inbound count row for agent {aid} missing"
    return row[0]


# ── resurrect ────────────────────────────────────────────────────────────────


def test_resurrect_agent_hosted_flips_and_wakes(
    db_conn: psycopg.Connection, wakes: list[tuple[int, str]]
) -> None:
    """terminated -> idling + resurrect inbound + one wake; no launch, no
    pid-confirm polling."""
    aid = _park(db_conn, status="terminated")
    out = agent_wake.resurrect_agent(aid, resurrected_by="user")
    assert out == aid
    assert _row(db_conn, aid) == ("idling", None)
    assert _kind_rows(db_conn, aid, "resurrect") == 1
    assert wakes == [(aid, "0")]


def test_resurrect_agent_hosted_keeps_trigger_guard(
    db_conn: psycopg.Connection, wakes: list[tuple[int, str]]
) -> None:
    """The auto-resurrect trigger CAS semantics are mode-independent: a stale
    trigger still refuses the transition."""
    aid = _park(db_conn, status="terminated")
    with pytest.raises(agent_wake.ResurrectTriggerStaleError):
        agent_wake.resurrect_agent(
            aid,
            resurrected_by="system",
            trigger_inbound_id=999999,
            trigger_inbound_kind="chat",
        )
    assert _row(db_conn, aid)[0] == "terminated"
    assert wakes == []


@pytest.mark.parametrize("guarded", [False, True])
@pytest.mark.parametrize("managed", [False, True])
async def test_resurrection_admits_a_new_incarnation_on_the_same_host(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    guarded: bool,
    managed: bool,
) -> None:
    """An observed termination never revalidates its original runtime token."""
    from uuid import uuid4

    from psycopg.types.json import Jsonb

    from agent.db import claim_inbound_batch
    from agent.hosted_ownership import admit_hosted_runtime, apply_hosted_lifecycle
    from agent.inbound_ownership import RuntimeOwnershipLostError
    from shared.db import insert_inbound_message
    from shared.incarnation_resources import ResourceBirth
    from shared.turn_identity import bind_turn_identity

    aid = _park(db_conn, status="idling")
    if managed:
        db_conn.execute(
            "UPDATE agents_meta SET incarnation_resources=%s WHERE id=%s",
            (Jsonb(ResourceBirth(birth=uuid4()).model_dump(mode="json")), aid),
        )
        db_conn.commit()
    owner = uuid4()
    old = await admit_hosted_runtime(aops_pool, aid, machine_name(), owner, expected_from="idling")
    assert old is not None
    command = insert_inbound_message(db_conn, aid, "", "self", kind="terminate")
    with bind_turn_identity(aid, incarnation=old):
        assert [item.id for item in await claim_inbound_batch(aops_pool, aid)] == [command]
        assert await apply_hosted_lifecycle(aops_pool, old) == "terminate"
    trigger = insert_inbound_message(db_conn, aid, "continue", "user")
    agent_wake.resurrect_agent(
        aid,
        resurrected_by="system" if guarded else "user",
        trigger_inbound_id=trigger if guarded else None,
        trigger_inbound_kind="chat" if guarded else None,
    )
    successor = await admit_hosted_runtime(
        aops_pool, aid, machine_name(), owner, expected_from="idling"
    )
    assert successor is not None and successor.generation != old.generation
    with bind_turn_identity(aid, incarnation=old), pytest.raises(RuntimeOwnershipLostError):
        await claim_inbound_batch(aops_pool, aid)
    with bind_turn_identity(aid, incarnation=successor):
        assert {item.kind for item in await claim_inbound_batch(aops_pool, aid)} == {
            "chat",
            "resurrect",
        }
