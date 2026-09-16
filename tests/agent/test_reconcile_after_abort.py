"""The abort-time inbound reconcile against real rows.

Task #3615: the same reconcile the startup path runs must also dispose the rows
an aborted hosted turn left `claimed` — at the turn's settlement, so they do not
wait for a boot that may never come. These lock the row-visible contract: the
three-way split, the runtime-ownership fence (a replaced incarnation is a
no-op), idempotent re-runs (the later boot reconcile changes nothing), and that
a committed row is not re-delivered by the next claim.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import START, StateGraph
from psycopg_pool import AsyncConnectionPool

from agent import state as states
from agent.db import claim_inbound_batch
from agent.inbound_ownership import RuntimeOwnershipLostError
from agent.startup import _reconcile_claimed_inbounds_at_startup
from services.agent_host import settlement as settlement_mod
from shared.context import AvaContext
from shared.delta_read_compat import wrap_saver_reads_with_delta_reconstruction
from shared.turn_identity import bind_turn_identity
from tests.agent.test_hosted_db_recovery import _admit


def _insert_claimed(
    conn: psycopg.Connection[Any], agent: int, content: str, *, age: timedelta = timedelta()
) -> int:
    """One `'claimed'` chat row, exactly what an aborted turn leaves behind."""
    row = conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source, status, claimed_at) "
        "VALUES (%s, %s, 'chat', 'user', 'claimed', %s) RETURNING id",
        (agent, content, datetime.now(UTC) - age),
    ).fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


def _statuses(conn: psycopg.Connection[Any], ids: list[int]) -> dict[int, str]:
    rows = conn.execute(
        "SELECT id, status FROM inbound_messages WHERE id = ANY(%s)", (ids,)
    ).fetchall()
    return {int(r[0]): str(r[1]) for r in rows}


async def _seed_checkpoint(
    pool: AsyncConnectionPool[Any], agent: int, committed_id: int
) -> AsyncPostgresSaver:
    """A settled checkpoint holding `committed_id` as a committed message."""

    async def work(_state: states.BaseAgentState) -> dict[str, object]:
        return {}

    saver = AsyncPostgresSaver(pool)
    await saver.setup()
    # Delta write model (#3180): fold delta-written messages on read (daemon parity).
    wrap_saver_reads_with_delta_reconstruction(saver)
    builder: Any = StateGraph(states.AgentState, context_schema=AvaContext)
    builder.add_node("work", work)
    builder.add_edge(START, "work")
    builder.add_edge("work", "__end__")
    graph = builder.compile(checkpointer=saver)
    await graph.aupdate_state(
        {"configurable": {"thread_id": str(agent)}},
        {
            "messages": [
                HumanMessage(
                    content="committed", additional_kwargs={"ava_inbound_id": committed_id}
                )
            ]
        },
        as_node="work",
    )
    return saver


async def test_settled_abort_splits_committed_orphan_and_stale_rows(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    loguru_records: list[dict[str, Any]],
) -> None:
    """The abort settlement's reconcile finalizes the turn's rows at once:
    committed to the flushed checkpoint -> done, fresh orphans -> pending for
    the next claim, rows past the stale threshold -> dead-lettered."""
    incarnation = await _admit(aops_pool)
    agent = incarnation.agent_id
    committed = _insert_claimed(db_conn, agent, "committed")
    orphan = _insert_claimed(db_conn, agent, "orphan")
    stale = _insert_claimed(db_conn, agent, "stale", age=timedelta(days=2))
    saver = await _seed_checkpoint(aops_pool, agent, committed)

    await settlement_mod.reconcile_inbounds_after_abort(aops_pool, saver, incarnation)

    assert _statuses(db_conn, [committed, orphan, stale]) == {
        committed: "done",
        orphan: "pending",
        stale: "done",
    }
    records = [r for r in loguru_records if r["extra"].get("event") == "inbound_reconcile"]
    assert len(records) == 1
    extra = records[0]["extra"]
    assert (extra["committed"], extra["reset"], extra["dead_lettered"]) == (1, 1, 1)


async def test_boot_reconcile_after_the_abort_pass_changes_nothing(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    loguru_records: list[dict[str, Any]],
) -> None:
    """A kill after the abort leaves the rows already final: the boot pass
    (the same helper) is idempotent and silent on the second run."""
    incarnation = await _admit(aops_pool)
    agent = incarnation.agent_id
    committed = _insert_claimed(db_conn, agent, "committed")
    orphan = _insert_claimed(db_conn, agent, "orphan")
    saver = await _seed_checkpoint(aops_pool, agent, committed)

    await settlement_mod.reconcile_inbounds_after_abort(aops_pool, saver, incarnation)
    settled = {committed: "done", orphan: "pending"}
    assert _statuses(db_conn, [committed, orphan]) == settled
    logged = [r for r in loguru_records if r["extra"].get("event") == "inbound_reconcile"]

    with bind_turn_identity(agent, incarnation=incarnation):
        await _reconcile_claimed_inbounds_at_startup(aops_pool, saver, agent)

    assert _statuses(db_conn, [committed, orphan]) == settled
    assert [r for r in loguru_records if r["extra"].get("event") == "inbound_reconcile"] == logged


async def test_replaced_incarnation_writes_nothing(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
) -> None:
    """A replacement runtime owns the row now: the stale incarnation's pass is
    refused by the lease fence before any write."""
    incarnation = await _admit(aops_pool)
    agent = incarnation.agent_id
    committed = _insert_claimed(db_conn, agent, "committed")
    orphan = _insert_claimed(db_conn, agent, "orphan")
    saver = await _seed_checkpoint(aops_pool, agent, committed)
    db_conn.execute(
        "UPDATE agents_meta SET runtime_generation=%s, runtime_owner=%s WHERE id=%s",
        (uuid4(), uuid4(), agent),
    )
    db_conn.commit()

    with (
        bind_turn_identity(agent, incarnation=incarnation),
        pytest.raises(RuntimeOwnershipLostError),
    ):
        await _reconcile_claimed_inbounds_at_startup(aops_pool, saver, agent)

    assert _statuses(db_conn, [committed, orphan]) == {
        committed: "claimed",
        orphan: "claimed",
    }


async def test_settlement_pass_swallows_a_replaced_incarnation(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    loguru_records: list[dict[str, Any]],
) -> None:
    """At the settlement boundary the same refusal is a logged no-op — the
    abort's settlement must not fail because its reconcile was fenced out."""
    incarnation = await _admit(aops_pool)
    agent = incarnation.agent_id
    committed = _insert_claimed(db_conn, agent, "committed")
    saver = await _seed_checkpoint(aops_pool, agent, committed)
    db_conn.execute(
        "UPDATE agents_meta SET runtime_generation=%s, runtime_owner=%s WHERE id=%s",
        (uuid4(), uuid4(), agent),
    )
    db_conn.commit()

    # must not raise: the settlement is not failed by a fenced-out reconcile
    await settlement_mod.reconcile_inbounds_after_abort(aops_pool, saver, incarnation)

    assert _statuses(db_conn, [committed]) == {committed: "claimed"}
    skips = [r for r in loguru_records if r["extra"].get("event") == "host_abort_reconcile_skipped"]
    assert [r["extra"]["reason"] for r in skips] == ["ownership_lost"]


async def test_next_claim_does_not_re_deliver_the_committed_row(
    db_conn: psycopg.Connection[Any], aops_pool: AsyncConnectionPool[Any]
) -> None:
    """At-least-once delivery still permits duplicates, but a row the
    checkpoint committed is not re-delivered by the next claim cycle — only
    the uncommitted orphan is."""
    incarnation = await _admit(aops_pool)
    agent = incarnation.agent_id
    committed = _insert_claimed(db_conn, agent, "committed")
    orphan = _insert_claimed(db_conn, agent, "orphan")
    saver = await _seed_checkpoint(aops_pool, agent, committed)

    await settlement_mod.reconcile_inbounds_after_abort(aops_pool, saver, incarnation)
    with bind_turn_identity(agent, incarnation=incarnation):
        claimed = await claim_inbound_batch(aops_pool, agent)

    assert [c.id for c in claimed] == [orphan]
    assert _statuses(db_conn, [committed]) == {committed: "done"}
