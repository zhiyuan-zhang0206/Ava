"""The claim node's compact inbounds: summary, request, retries, supersession and the batches they share with chat and restart."""

from collections.abc import Callable
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import psycopg
import pytest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_core.messages.modifier import RemoveMessage
from langgraph.graph import END
from psycopg_pool import AsyncConnectionPool

from agent.graph import claim_node
from agent.graph.tests.cursor_fixture import _fresh_snapshot_cursor as _fresh_snapshot_cursor
from agent.hooks.compact import compose_summary_message
from agent.state import AgentState, CompactState
from agent.tests.claim.claim_status_support import (
    _await_status,
    _committed_publishes,
    _compact_tail,
    _pair_compact_cycles,
    _set_agent_status,
)
from agent.tests.claim.claim_status_support import running_agent as running_agent
from agent.tests.claim.claim_support import (
    _await_inbound_visible,
    _config,
    _fake_llm,
    _insert_inbound_kind,
    _make_runtime,
)
from base.db import Database, insert_inbound_message
from base.events.live.bus import EventBus
from tests.fixtures.units import spawn_agent


async def _set_agent_status_async(pool: "AsyncConnectionPool", agent_id: int, status: str) -> None:
    """UPDATE agents_meta.status via `pool` — same pool claim_node uses.

    Eliminates the sync db_conn -> async aops_pool visibility window that
    causes the CI-only `idling != restarting` flake.  The CAS-free
    unconditional UPDATE is appropriate for test setup (prod uses CAS because
    concurrent lifecycle ops can race; a test setting up a single agent owns
    the row).
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("UPDATE agents_meta SET status = %s WHERE id = %s", (status, agent_id))
        if cur.rowcount != 1:
            await cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
            actual_row = await cur.fetchone()
            actual = actual_row[0] if actual_row is not None else "<row missing>"
            raise AssertionError(
                f"_set_agent_status_async: agent {agent_id} not updated "
                f"(rowcount={cur.rowcount}, actual status={actual!r})"
            )


async def _insert_inbound_kind_async(
    pool: "AsyncConnectionPool", agent_id: int, content: str, kind: str, source: str = "system"
) -> int:
    """INSERT an inbound row via `pool` — same pool claim_node uses.

    Returns the new inbound id.  Eliminates the sync->async visibility gap:
    because the INSERT happens on the same pool that claim_inbound_batch
    will read from, the row is immediately visible (autocommit).
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (agent_id, content, kind, source),
        )
        row = await cur.fetchone()
    assert row is not None, f"_insert_inbound_kind_async: no RETURNING for agent {agent_id}"
    return row[0]


async def test_claim_compact_summary_replaces_messages_with_remove_sentinel(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """compact_summary inbound (written by agent ava.compact) → claim returns
    Command containing RemoveMessage(REMOVE_ALL_MESSAGES) sentinel + summary.
    The entire history is replaced, leaving no raw tail. Does **not** call LLM."""
    tid = spawn_agent()
    summary_text = "agent-written summary text"
    _insert_inbound_kind(db_conn, tid, summary_text, "compact_summary")

    # state already has some messages (simulate last turn's history) — messages[0] must be
    # SystemMessage (invariant after first claim round injects it)
    sys_msg = SystemMessage(content="<test sys prompt>")
    initial_msgs: list[AnyMessage] = [
        sys_msg,
        *(HumanMessage(content=f"old-{i}") for i in range(8)),
    ]
    state = AgentState(messages=initial_msgs)

    fake_llm = _fake_llm("LLM should not be called")
    cmd = await claim_node(
        state,
        _make_runtime(ops_pool=aops_pool, llm=fake_llm),
        _config(
            tid,
        ),
    )

    fake_llm.bind_tools.return_value.ainvoke.assert_not_called()  # agent-authored summary, skip LLM
    tail = _compact_tail(cmd.update)
    assert isinstance(tail[0], HumanMessage)
    assert tail[0].content == compose_summary_message(summary_text)  # pyright: ignore[reportUnknownMemberType]


async def test_claim_compact_summary_bumps_compact_version(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """Agent-authored compact (claim path) advances compact.version, matching the
    forced path's before_llm hook — this REMOVE_ALL stripped the messages just the
    same, so Layer 3 subscribers (ava_code's context-file re-injection, the
    reminder re-arm) must see it. Without the bump a self-compact is invisible to
    them."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "agent summary", "compact_summary")
    state = AgentState(
        messages=[SystemMessage(content="<sys>"), HumanMessage(content="old")],
        compact=CompactState(version=5),
    )

    cmd = await claim_node(
        state,
        _make_runtime(ops_pool=aops_pool, llm=_fake_llm("LLM should not be called")),
        _config(
            tid,
        ),
    )

    assert cmd.update["compact"].version == 6  # type: ignore[index]


async def test_claim_compact_request_calls_backend_llm(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """compact_request inbound (user "/compact") → claim calls generate_summary,
    running backend LLM to generate a summary, then replaces messages."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "compact_request")

    sys_msg = SystemMessage(content="<test sys prompt>")
    initial_msgs: list[AnyMessage] = [
        sys_msg,
        *(HumanMessage(content=f"history-{i}") for i in range(10)),
    ]
    state = AgentState(messages=initial_msgs)

    fake_llm = _fake_llm("LLM-generated summary")
    cmd = await claim_node(
        state,
        _make_runtime(ops_pool=aops_pool, llm=fake_llm),
        _config(
            tid,
        ),
    )

    fake_llm.bind_tools.return_value.ainvoke.assert_called_once()
    tail = _compact_tail(cmd.update)
    assert tail[0].content == compose_summary_message("LLM-generated summary")  # pyright: ignore[reportUnknownMemberType]


async def test_claim_compact_request_emits_live_run_pair_with_durable_anchor(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """Task #3323: the claim-path compact run (UI Compact / auto-resurrect)
    emits compact_started before the Compaction LLM call and
    compact_finished(success) when the summary is applied — same compact_id —
    and the summary message carries the durable anchor ava_compact_id."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "compact_request")
    state = AgentState(
        messages=[
            SystemMessage(content="<sys>"),
            *(HumanMessage(content=f"history-{i}") for i in range(8)),
        ]
    )
    publisher = MagicMock()

    cmd = await claim_node(
        state,
        _make_runtime(
            ops_pool=aops_pool,
            llm=_fake_llm("LLM-generated summary"),
            event_publisher=publisher,
        ),
        _config(tid),
    )

    [(started, finished)] = _pair_compact_cycles(publisher)
    assert started["mode"] == "request"
    assert finished["status"] == "success"
    assert cmd.goto == "init_context"
    tail = _compact_tail(cmd.update)
    assert tail[0].additional_kwargs["ava_compact_id"] == started["compact_id"]  # pyright: ignore[reportUnknownMemberType]


async def test_claim_two_compact_requests_second_replaces_first(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """Two compact_requests claimed in one batch (double-triggered Compact):
    the later summary wins the payload slot; the earlier run's generated
    summary can never be applied, so its live block closes as `replaced`
    (task #3323) and the applied summary is the second one."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "compact_request")
    _insert_inbound_kind(db_conn, tid, "", "compact_request")
    state = AgentState(
        messages=[
            SystemMessage(content="<sys>"),
            *(HumanMessage(content=f"history-{i}") for i in range(8)),
        ]
    )
    llm = MagicMock()
    llm.bind_tools.return_value.ainvoke = AsyncMock(
        side_effect=[AIMessage(content="first summary"), AIMessage(content="second summary")]
    )
    publisher = MagicMock()

    cmd = await claim_node(
        state,
        _make_runtime(ops_pool=aops_pool, llm=llm, event_publisher=publisher),
        _config(tid),
    )

    pairs = _pair_compact_cycles(publisher)
    assert [p[1]["status"] for p in pairs] == ["replaced", "success"]
    assert llm.bind_tools.return_value.ainvoke.await_count == 2
    tail = _compact_tail(cmd.update)
    assert tail[0].content == compose_summary_message("second summary")  # pyright: ignore[reportUnknownMemberType]
    assert tail[0].additional_kwargs["ava_compact_id"] == pairs[1][0]["compact_id"]  # pyright: ignore[reportUnknownMemberType]


async def test_claim_compact_summary_supersedes_pending_request_and_closes_it_replaced(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """A batch whose compact_request is followed by an agent compact_summary:
    the request's LLM run produced a summary, but the summary overwrites the
    payload slot, so the run's live block closes as `replaced` (task #3323)
    and the applied summary is the agent-authored one (no live-run anchor).
    """
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "compact_request")
    _insert_inbound_kind(db_conn, tid, "agent-authored summary", "compact_summary")
    state = AgentState(
        messages=[
            SystemMessage(content="<sys>"),
            *(HumanMessage(content=f"history-{i}") for i in range(8)),
        ]
    )
    llm = _fake_llm("llm-generated summary")
    publisher = MagicMock()

    cmd = await claim_node(
        state,
        _make_runtime(ops_pool=aops_pool, llm=llm, event_publisher=publisher),
        _config(tid),
    )

    [(started, finished)] = _pair_compact_cycles(publisher)
    assert started["mode"] == "request"
    assert finished["status"] == "replaced"
    assert llm.bind_tools.return_value.ainvoke.await_count == 1
    tail = _compact_tail(cmd.update)
    assert tail[0].content == compose_summary_message("agent-authored summary")  # pyright: ignore[reportUnknownMemberType]
    assert "ava_compact_id" not in tail[0].additional_kwargs  # pyright: ignore[reportUnknownMemberType]


async def test_claim_cancel_beats_pending_compact_and_closes_it_replaced(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """cancel co-batched with an already-run compact_request: the cancel path
    drops the compact payload instead of applying it — the run's live block
    must still close (replaced), and no summary enters the new context."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "compact_request")
    _insert_inbound_kind(db_conn, tid, "", "cancel", source="user")
    state = AgentState(
        messages=[
            SystemMessage(content="<sys>"),
            *(HumanMessage(content=f"history-{i}") for i in range(8)),
        ]
    )
    publisher = MagicMock()

    cmd = await claim_node(
        state,
        _make_runtime(
            ops_pool=aops_pool,
            llm=_fake_llm("never applied"),
            event_publisher=publisher,
        ),
        _config(tid),
    )

    [(started, finished)] = _pair_compact_cycles(publisher)
    assert started["mode"] == "request"
    assert finished["status"] == "replaced"
    assert cmd.goto == "claim"
    assert cmd.update["halted"] is True  # type: ignore[index]
    assert cmd.update is not None and "context_reset" not in cmd.update


async def test_claim_compact_request_empty_conversation_consumed_as_noop(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """compact_request on no conversation messages (only SystemMessage) is a normal user operation,
    not a fault — consumed as a no-op: does not issue LLM request, does not replace messages,
    does not raise an error (raising would crash the process after the batch is already claimed,
    losing the consumed inbound row)."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "compact_request")

    sys_msg = SystemMessage(content="<test sys prompt>")
    state = AgentState(messages=[sys_msg])  # only the system prompt, no conversation

    fake_llm = _fake_llm("should not be generated")
    cmd = await claim_node(
        state,
        _make_runtime(ops_pool=aops_pool, llm=fake_llm),
        _config(
            tid,
        ),
    )

    fake_llm.bind_tools.return_value.ainvoke.assert_not_called()
    assert not any(isinstance(m, RemoveMessage) for m in cmd.update["messages"])  # type: ignore[index]


async def test_claim_compact_request_retries_then_raises_compaction_failed(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """compact_request whose Compaction LLM keeps failing → claim retries
    COMPACT_MAX_ATTEMPTS times, then raises CompactionFailedError (the runloop
    turns that into a turn-abort; the agent stays alive) instead of letting a
    raw provider exception kill the process after the row is consumed."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "compact_request")

    sys_msg = SystemMessage(content="<test sys prompt>")
    initial_msgs: list[AnyMessage] = [
        sys_msg,
        *(HumanMessage(content=f"history-{i}") for i in range(10)),
    ]
    state = AgentState(messages=initial_msgs)

    failing = AsyncMock(side_effect=RuntimeError("provider 502 on every attempt"))
    llm = MagicMock()
    llm.bind_tools.return_value.ainvoke = failing
    publisher = MagicMock()

    from agent.hooks.compact import COMPACT_MAX_ATTEMPTS, CompactionFailedError

    with pytest.raises(CompactionFailedError, match="no usable summary across"):
        await claim_node(
            state,
            _make_runtime(ops_pool=aops_pool, llm=llm, event_publisher=publisher),
            _config(
                tid,
            ),
        )
    assert failing.await_count == COMPACT_MAX_ATTEMPTS
    [(started, finished)] = _pair_compact_cycles(publisher)
    assert started["mode"] == "request"
    assert finished["status"] == "failure"


async def test_claim_compact_request_retries_then_succeeds(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """compact_request whose first Compaction LLM call fails → retried; a later
    attempt's summary is applied (same retry semantics as the auto-compact
    hook's COMPACT_MAX_ATTEMPTS)."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "compact_request")

    sys_msg = SystemMessage(content="<test sys prompt>")
    initial_msgs: list[AnyMessage] = [
        sys_msg,
        *(HumanMessage(content=f"history-{i}") for i in range(10)),
    ]
    state = AgentState(messages=initial_msgs)

    llm = MagicMock()
    llm.bind_tools.return_value.ainvoke = AsyncMock(
        side_effect=[RuntimeError("transient 503"), AIMessage(content="retried summary")]
    )

    cmd = await claim_node(
        state,
        _make_runtime(ops_pool=aops_pool, llm=llm),
        _config(
            tid,
        ),
    )

    assert llm.bind_tools.return_value.ainvoke.await_count == 2
    tail = _compact_tail(cmd.update)
    assert tail[0].content == compose_summary_message("retried summary")  # pyright: ignore[reportUnknownMemberType]


async def test_claim_compact_summary_with_chat_in_same_batch_defers_chat(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
):
    """A chat sent between the agent's ava.self.compact and claim's wake lands in
    the same batch as the compact_summary — it must not be lost, and it must NOT
    survive the compact as a raw message. The compact wipes cleanly (tail = the
    summary alone) and the chat row is reverted to pending so the next claim
    delivers it in the freshly established context. Regression: the chat used to
    be parked after the summary (the extra_msgs tail), which the user observed
    as original messages surviving a compact."""
    tid = spawn_agent()
    summary_text = "agent summary"
    _insert_inbound_kind(db_conn, tid, summary_text, "compact_summary")
    chat_id = insert_inbound_message(
        db_conn, tid, "user during compact", source="user", bus=event_bus, database=database
    )

    sys_msg = SystemMessage(content="<test sys prompt>")
    initial_msgs: list[AnyMessage] = [
        sys_msg,
        *(HumanMessage(content=f"old-{i}") for i in range(7)),
    ]
    state = AgentState(messages=initial_msgs)

    cmd = await claim_node(
        state,
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    # The compact is a clean wipe: the parked tail is the summary alone.
    tail = _compact_tail(cmd.update)
    assert len(tail) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert tail[0].content == compose_summary_message(summary_text)  # pyright: ignore[reportUnknownMemberType]
    assert cmd.goto == "init_context"
    # The co-batched chat is deferred, not dropped: back to pending so the
    # next claim delivers it in the fresh context.
    with db_conn.cursor() as cur:
        cur.execute("SELECT status, claimed_at FROM inbound_messages WHERE id = %s", (chat_id,))
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "pending"
    assert row[1] is None


async def test_claim_compact_summary_finalizes_claimed_history(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
):
    """A compaction finalizes every already-claimed inbound row to 'done'
    BEFORE the REMOVE_ALL wipe. Those rows' HumanMessages live in
    state.messages (about to be wiped) and carry the ava_inbound_id startup
    reconcile matches on; if they stay 'claimed', the next restart sees them
    missing from the checkpoint, resets them to 'pending', and re-delivers
    already-answered messages — a run of consecutive user messages with the
    compacted replies gone (Task #823)."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "agent summary", "compact_summary")
    # Two chats claimed earlier (their HumanMessages are in state.messages,
    # status still 'claimed' — the two-phase path finalizes only at startup).
    chat1 = insert_inbound_message(
        db_conn, tid, "user q1", source="user", bus=event_bus, database=database
    )
    chat2 = insert_inbound_message(
        db_conn, tid, "user q2", source="user", bus=event_bus, database=database
    )
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE inbound_messages SET status = 'claimed', claimed_at = now() WHERE id = ANY(%s)",
            ([chat1, chat2],),
        )
    db_conn.commit()

    sys_msg = SystemMessage(content="<test sys prompt>")
    state = AgentState(messages=[sys_msg, HumanMessage(content="old")])

    cmd = await claim_node(
        state,
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    # Compact still a clean wipe: the parked tail is the summary alone.
    tail = _compact_tail(cmd.update)
    assert len(tail) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert tail[0].content == compose_summary_message("agent summary")  # pyright: ignore[reportUnknownMemberType]
    # ...and the claimed history is finalized: a post-compact restart must not
    # re-deliver them (reconcile only resets 'claimed' rows).
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT id, status FROM inbound_messages WHERE id = ANY(%s) ORDER BY id",
            ([chat1, chat2],),
        )
        rows = cur.fetchall()
    assert [(r[0], r[1]) for r in rows] == [(chat1, "done"), (chat2, "done")]


@pytest.mark.flaky  # poll _await_status for claim_node status transition
async def test_claim_compact_request_batched_with_restart_is_dropped(
    running_agent: Callable[[], int], db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """compact_request + restart in same batch → compact_request is the discarded loser:
    does **not** run the backend Compaction LLM (if it raised, the already consumed restart row
    would be lost before the restart is applied), restart exits normally. Re-trigger /compact afterwards."""
    tid = running_agent()
    _set_agent_status(db_conn, tid, "running")
    _insert_inbound_kind(db_conn, tid, "", "compact_request", source="user")
    restart_id = _insert_inbound_kind(db_conn, tid, "", "restart", source="user")
    await _await_inbound_visible(aops_pool, restart_id)
    llm = _fake_llm()

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        _make_runtime(ops_pool=aops_pool, llm=llm),
        _config(
            tid,
        ),
    )

    assert cmd.goto == END
    # Compaction LLM not called — compact_request is discarded rather than run before exiting
    llm.bind_tools.return_value.ainvoke.assert_not_called()
    # No compact happened: does not go through REMOVE_ALL replacement path
    assert not any(isinstance(m, RemoveMessage) for m in cmd.update["messages"])  # type: ignore[index]
    # idle preserved unchanged — silent after restart
    assert cmd.update["halted"] is True  # type: ignore[index]
    await _await_status(aops_pool, tid, "running")


@pytest.mark.flaky  # poll _await_status for claim_node status transition
async def test_claim_compact_summary_batched_with_restart_applies_and_keeps_idle(
    running_agent: Callable[[], int], db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """compact_summary + restart in same batch → summary is data the agent already wrote itself,
    applied as usual (discarding = silently swallowing the agent's work), while the restart's idle
    preservation is not overwritten by the compact return path's halted — remains silent after restart."""
    tid = running_agent()
    # Confirm the spawn is visible on the async pool before we start writing
    # through it.  Every subsequent write goes through `aops_pool` too, so there
    # is no sync→async visibility window — the same pool is used for writes and
    # for the claim_node read.
    await _await_status(aops_pool, tid, "running")
    await _set_agent_status_async(aops_pool, tid, "running")
    # Confirm the status update is visible before inserting inbounds — a second
    # read-after-write barrier on the same pool eliminates any residual
    # cross-connection visibility gap that could cause claim_node's
    # claim write to see a stale status.
    await _await_status(aops_pool, tid, "running")
    await _insert_inbound_kind_async(
        aops_pool, tid, "compacted summary", "compact_summary", source="self"
    )
    await _insert_inbound_kind_async(aops_pool, tid, "", "restart", source="user")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    # Already-authored data is retained for the successor, not applied by an exiting owner.
    assert cmd.goto == END
    restart_messages = cast(list[AnyMessage], cast(dict[str, Any], cmd.update)["messages"])
    assert (
        len(restart_messages) == 1
        and "Restart was accepted" in restart_messages[0].model_dump()["content"]
    )  # type: ignore[index]
    assert db_conn.execute(
        "SELECT status,content FROM inbound_messages WHERE agent_id=%s AND kind='compact_summary'",
        (tid,),
    ).fetchone() == ("pending", "compacted summary")
    # external restart + idle before restart → halted=True preserved (restart silent)
    assert cmd.update["halted"] is True  # type: ignore[index]
    # Read status on the pool claim_node wrote through; _await_status dumps full
    # state on timeout (the CI-only `idling != restarting` flake).
    await _await_status(aops_pool, tid, "running")


async def test_claim_compact_summary_alone_does_not_publish_committed(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    """compact_summary alone → does not publish InboundCommitted (it goes through state replace
    not inbound append; frontend reload should be triggered by llm_done)."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "summary", "compact_summary")

    pub = MagicMock()
    state = AgentState(messages=[SystemMessage(content="sys")])
    await claim_node(
        state,
        _make_runtime(ops_pool=aops_pool, event_publisher=pub),
        _config(
            tid,
        ),
    )
    assert _committed_publishes(pub) == []


async def test_claim_compact_summary_with_no_existing_system_message(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """state.messages empty (first round) + compact_summary arrives → claim simultaneously
    injects SystemMessage into new_msgs[0] AND takes it out as sys_msg to prepend again
    (Compact path `state.messages[0] if state.messages else new_msgs.pop(0)`).

    Claim used to lay down a cold-start head and then pop it back off when a
    compaction landed on the same turn, so a missed pop duplicated the
    SystemMessage. Claim now never emits one — the head is `init_context`'s — so
    the invariant is stronger and simpler: a compaction emits the clearing
    sentinel and nothing else."""
    tid = spawn_agent()
    summary_text = "first turn summary"
    _insert_inbound_kind(db_conn, tid, summary_text, "compact_summary")

    cmd = await claim_node(
        AgentState(),  # messages empty
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    tail = _compact_tail(cmd.update)
    assert not any(isinstance(m, SystemMessage) for m in cmd.update["messages"])  # type: ignore[index]
    assert not any(isinstance(m, SystemMessage) for m in tail)
    assert [m.content for m in tail] == [compose_summary_message(summary_text)]  # pyright: ignore[reportUnknownMemberType]


async def test_claim_compact_summary_returns_before_llm_with_halted_false(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """compact_summary path → returns Command(goto=before_llm, halted=False).

    Lock down mutant_168 (goto=None), mutant_170 (goto kw deleted), mutant_175-177
    (halted False → True / case change)."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "summary text", "compact_summary")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys"), HumanMessage(content="m1")]),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    # Compact path detours through init_context, resuming at BEFORE_LLM (default)
    assert cmd.goto == "init_context"
    assert cmd.update["context_reset"].resume == "before_llm"  # type: ignore[index]
    # halted must be False (clears halted to enter next LLM round), cannot be True / missing
    assert cmd.update["halted"] is False  # type: ignore[index]
    # update dict key is the literal 'halted', cannot be 'HALTED' / 'XXhaltedXX'
    assert "halted" in cmd.update  # type: ignore[operator]
