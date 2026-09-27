"""The inbound reconcile against real rows: abort settlement and the claim-window side-load.

Task #3615: the same reconcile the startup path runs must also dispose the rows
an aborted hosted turn left `claimed` — at the turn's settlement, so they do not
wait for a boot that may never come. These lock the row-visible contract: the
three-way split, the runtime-ownership fence (a replaced incarnation is a
no-op), idempotent re-runs (the later boot reconcile changes nothing), and that
a committed row is not re-delivered by the next claim.

Task #4788: the committed-id source is the claim window's `messages` write rows
(side-loaded from `shared/agents/history/inbound_sideload.py`), so the common
paths never read the settled checkpoint at all. These lock that source
strategy: the no-read guard, the window resolution, the committed-then-removed
proof, and both fallbacks (unresolved window, thread without messages write
rows).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest
from langchain_core.messages import HumanMessage, RemoveMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import START, StateGraph
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from psycopg_pool import AsyncConnectionPool

from agent import state as states
from agent.db import claim_inbound_batch
from agent.inbound_ownership import RuntimeOwnershipLostError
from agent.startup import _reconcile_claimed_inbounds_at_startup
from services.agent_host import settlement as settlement_mod
from shared.agents.history.delta_read_compat import wrap_saver_reads_with_delta_reconstruction
from shared.agents.history.inbound_sideload import (
    _claim_window_start,
    _messages_writes_in_window,
    sideload_committed_ids,
)
from shared.context import AvaContext
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


class _CountingSaver(AsyncPostgresSaver):
    """Saver that counts full-checkpoint reads — the path the side-load removes."""

    aget_calls: int = 0

    async def aget(self, config: Any) -> Any:
        self.aget_calls += 1
        return await super().aget(config)


def _build_graph(saver: AsyncPostgresSaver) -> Any:
    async def work(_state: states.BaseAgentState) -> dict[str, object]:
        return {}

    builder: Any = StateGraph(states.AgentState, context_schema=AvaContext)
    builder.add_node("work", work)
    builder.add_edge(START, "work")
    builder.add_edge("work", "__end__")
    return builder.compile(checkpointer=saver)


async def _seed_checkpoint(
    pool: AsyncConnectionPool[Any],
    agent: int,
    committed_id: int,
    *,
    saver_cls: type[AsyncPostgresSaver] = AsyncPostgresSaver,
) -> AsyncPostgresSaver:
    """A settled checkpoint holding `committed_id` as a committed message."""

    saver = saver_cls(pool)
    await saver.setup()
    # Delta write model (#3180): fold delta-written messages on read (daemon parity).
    wrap_saver_reads_with_delta_reconstruction(saver)
    graph = _build_graph(saver)
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


async def _seed_counting_checkpoint(
    pool: AsyncConnectionPool[Any], agent: int, committed_id: int
) -> _CountingSaver:
    """`_seed_checkpoint` on a read-counting saver — the side-load's no-read proof."""
    return cast(
        _CountingSaver,
        await _seed_checkpoint(pool, agent, committed_id, saver_cls=_CountingSaver),
    )


async def _seed_delta_written_checkpoint(
    pool: AsyncConnectionPool[Any],
    agent: int,
    committed_id: int,
    *,
    message_id: str = "m-1",
    remove_after: bool = False,
) -> _CountingSaver:
    """A delta-written thread: the committed message as a `messages` write row.

    Seeds through the saver's own write path — `aput_writes`, the call the
    runtime makes for every super-step's appends — so the stored shape is the
    one the side-load reads. `remove_after` adds a later full-wipe write: the
    shape whose id survives only in the write chain.
    """

    saver = _CountingSaver(pool)
    await saver.setup()
    wrap_saver_reads_with_delta_reconstruction(saver)
    graph = _build_graph(saver)
    config = {"configurable": {"thread_id": str(agent)}}
    base = await graph.aupdate_state(config, {"halted": False}, as_node="work")
    await saver.aput_writes(
        base,
        [
            (
                "messages",
                [
                    HumanMessage(
                        id=message_id,
                        content="committed",
                        additional_kwargs={"ava_inbound_id": committed_id},
                    )
                ],
            )
        ],
        str(uuid4()),
    )
    if remove_after:
        wiped = await graph.aupdate_state(config, {"halted": True}, as_node="work")
        await saver.aput_writes(
            wiped, [("messages", [RemoveMessage(id=REMOVE_ALL_MESSAGES)])], str(uuid4())
        )
    return saver


def _insert_checkpoint(
    db_conn: psycopg.Connection[Any], agent: int, checkpoint_id: str, ts: str
) -> None:
    db_conn.execute(
        "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, checkpoint, "
        "metadata) VALUES (%s, '', %s, %s::jsonb, '{}'::jsonb)",
        (str(agent), checkpoint_id, json.dumps({"id": checkpoint_id, "ts": ts})),
    )
    db_conn.commit()


def _insert_write(
    db_conn: psycopg.Connection[Any],
    agent: int,
    checkpoint_id: str,
    *,
    idx: int,
    type_tag: str,
    blob: bytes,
) -> None:
    db_conn.execute(
        "INSERT INTO checkpoint_writes (thread_id, checkpoint_ns, channel, checkpoint_id, "
        "task_id, idx, type, blob) VALUES (%s, '', 'messages', %s, %s, %s, %s, %s)",
        (str(agent), checkpoint_id, str(uuid4()), idx, type_tag, blob),
    )
    db_conn.commit()


async def test_no_claimed_rows_never_reads_the_checkpoint(
    aops_pool: AsyncConnectionPool[Any],
    loguru_records: list[dict[str, Any]],
) -> None:
    incarnation = await _admit(aops_pool)
    agent = incarnation.agent_id
    saver = _CountingSaver(aops_pool)

    with bind_turn_identity(agent, incarnation=incarnation):
        await _reconcile_claimed_inbounds_at_startup(aops_pool, saver, agent)

    assert saver.aget_calls == 0
    assert not [r for r in loguru_records if r["extra"].get("event") == "inbound_reconcile"]


async def test_only_stale_claims_skip_the_read_and_dead_letter(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    loguru_records: list[dict[str, Any]],
) -> None:
    incarnation = await _admit(aops_pool)
    agent = incarnation.agent_id
    stale = _insert_claimed(db_conn, agent, "stale", age=timedelta(days=2))
    saver = _CountingSaver(aops_pool)

    with bind_turn_identity(agent, incarnation=incarnation):
        await _reconcile_claimed_inbounds_at_startup(aops_pool, saver, agent)

    assert saver.aget_calls == 0
    assert _statuses(db_conn, [stale]) == {stale: "done"}
    records = [r for r in loguru_records if r["extra"].get("event") == "inbound_reconcile"]
    extra = records[0]["extra"]
    assert (extra["committed"], extra["reset"], extra["dead_lettered"]) == (0, 0, 1)


async def test_fresh_claim_resolves_from_the_claim_window(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    loguru_records: list[dict[str, Any]],
) -> None:
    incarnation = await _admit(aops_pool)
    agent = incarnation.agent_id
    committed = _insert_claimed(db_conn, agent, "committed")
    orphan = _insert_claimed(db_conn, agent, "orphan")
    saver = await _seed_delta_written_checkpoint(aops_pool, agent, committed)

    with bind_turn_identity(agent, incarnation=incarnation):
        await _reconcile_claimed_inbounds_at_startup(aops_pool, saver, agent)

    assert saver.aget_calls == 0
    assert _statuses(db_conn, [committed, orphan]) == {committed: "done", orphan: "pending"}
    records = [r for r in loguru_records if r["extra"].get("event") == "inbound_reconcile"]
    extra = records[0]["extra"]
    assert (extra["committed"], extra["reset"], extra["dead_lettered"]) == (1, 1, 0)


async def test_committed_and_later_removed_is_finalized_not_reset(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
) -> None:
    """The window proof finalizes a commit the list-presence check would reset."""
    incarnation = await _admit(aops_pool)
    agent = incarnation.agent_id
    committed = _insert_claimed(db_conn, agent, "committed")
    saver = await _seed_delta_written_checkpoint(aops_pool, agent, committed, remove_after=True)

    with bind_turn_identity(agent, incarnation=incarnation):
        await _reconcile_claimed_inbounds_at_startup(aops_pool, saver, agent)

    assert saver.aget_calls == 0
    assert _statuses(db_conn, [committed]) == {committed: "done"}


async def test_unresolved_window_falls_back_to_the_full_read(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: Any,
) -> None:
    incarnation = await _admit(aops_pool)
    agent = incarnation.agent_id
    committed = _insert_claimed(db_conn, agent, "committed")
    orphan = _insert_claimed(db_conn, agent, "orphan")
    saver = await _seed_counting_checkpoint(aops_pool, agent, committed)

    import shared.agents.history.inbound_sideload as sideload_mod

    async def _unresolved(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(sideload_mod, "sideload_committed_ids", _unresolved)

    with bind_turn_identity(agent, incarnation=incarnation):
        await _reconcile_claimed_inbounds_at_startup(aops_pool, saver, agent)

    assert saver.aget_calls == 1
    assert _statuses(db_conn, [committed, orphan]) == {committed: "done", orphan: "pending"}


async def test_thread_without_messages_writes_falls_back_to_the_full_read(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
) -> None:
    """A materialized thread (no write rows) must not read as "nothing committed"."""
    incarnation = await _admit(aops_pool)
    agent = incarnation.agent_id
    committed = _insert_claimed(db_conn, agent, "committed")
    orphan = _insert_claimed(db_conn, agent, "orphan")
    saver = await _seed_counting_checkpoint(aops_pool, agent, committed)

    with bind_turn_identity(agent, incarnation=incarnation):
        await _reconcile_claimed_inbounds_at_startup(aops_pool, saver, agent)

    assert saver.aget_calls == 1
    assert _statuses(db_conn, [committed, orphan]) == {committed: "done", orphan: "pending"}


async def test_boundary_scan_resolves_the_first_older_checkpoint(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
) -> None:
    agent = 879000001
    cutoff = datetime(2026, 1, 10, 0, 0, tzinfo=UTC)
    _insert_checkpoint(
        db_conn, agent, "ffffffff-0000-6000-8000-000000000001", "2026-01-10T00:00:05+00:00"
    )
    _insert_checkpoint(
        db_conn, agent, "eeeeeeee-0000-6000-8000-000000000002", "2026-01-10T00:00:01+00:00"
    )
    _insert_checkpoint(
        db_conn, agent, "dddddddd-0000-6000-8000-000000000003", "2026-01-09T23:59:59+00:00"
    )

    resolved, boundary = await _claim_window_start(aops_pool, agent, cutoff, scan_limit=10)
    assert resolved is True
    assert boundary == "dddddddd-0000-6000-8000-000000000003"


async def test_boundary_scan_gives_up_past_its_limit(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
) -> None:
    agent = 879000002
    cutoff = datetime(2026, 1, 10, 0, 0, tzinfo=UTC)
    for i in range(3):
        _insert_checkpoint(
            db_conn,
            agent,
            f"f0000000-0000-6000-8000-00000000000{i}",
            f"2026-01-10T00:00:0{i + 1}+00:00",
        )

    assert await _claim_window_start(aops_pool, agent, cutoff, scan_limit=2) == (False, None)
    assert await _claim_window_start(aops_pool, agent, cutoff, scan_limit=5) == (True, None)


async def test_window_fetch_cap_never_truncates(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
) -> None:
    agent = 879000003
    checkpoint_id = "f0000000-0000-6000-8000-000000000009"
    for idx in range(3):
        _insert_write(db_conn, agent, checkpoint_id, idx=idx, type_tag="msgpack", blob=b"\x80")

    assert await _messages_writes_in_window(aops_pool, agent, None, row_cap=2) is None
    rows = await _messages_writes_in_window(aops_pool, agent, None, row_cap=3)
    assert rows is not None and len(rows) == 3


async def test_sideload_decodes_inbound_ids_from_write_rows(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
) -> None:
    agent = 879000004
    saver = AsyncPostgresSaver(aops_pool)
    type_tag, blob = saver.serde.dumps_typed(
        [HumanMessage(content="x", additional_kwargs={"ava_inbound_id": 7})]
    )
    _insert_write(
        db_conn,
        agent,
        "f0000000-0000-6000-8000-00000000000a",
        idx=0,
        type_tag=str(type_tag),
        blob=bytes(blob),
    )

    ids = await sideload_committed_ids(aops_pool, saver, agent, since=datetime.now(UTC))
    assert ids == {7}
