"""claim node unit tests.

claim node is the core dispatcher of the newly designed 6-Node topology — it decides
state updates and routing based on inbound kind. These tests cover each kind case, using
a real DB (adb_conn fixture) + mock LLM.

Coverage includes:
- dispatch of various kinds (chat / compact_summary / compact_request /
  terminate / restart / restart_completed / resurrect / unknown)
- short-path: first SELECT already has inbound, does not enter wait branch
- long-await: first SELECT empty, agents.status switched 'idling' → wait → INSERT
  wake up → take batch → switch back 'running'

Not tested:
- Redis pub/sub wake integration for ops_db AsyncConnection (tested in test_db.py)
- auto-compact behavior (now a before_llm hook, tests in tests/agent/test_compact.py)
"""

import asyncio
import contextlib
import os
import time
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import psycopg
import pytest
from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.messages.modifier import RemoveMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool

from agent.graph import claim_node
from agent.graph._context import AvaContext
from agent.hooks.compact import compose_summary_message
from agent.messages import NoteTag, system_note_message
from agent.state import AgentState, CompactState
from shared.config import settings
from shared.db import insert_inbound_message
from shared.redis_listener import RedisInboundListener
from tests.conftest import spawn_agent

# Almost all claim tests are short-path dispatch: the inbound is INSERTed before
# claim_node runs, so its first SELECT gets the batch — pure DB side-effect +
# routing assertions, deterministic and parallel-safe (each xdist worker has its
# own throwaway DB). Only the handful that park in the real wait_for_inbound /
# _wait_for_batch loop and depend on a real Redis pub/sub wake keep
# `@pytest.mark.flaky` to run serial.


def _committed_publishes(pub: MagicMock) -> list[dict]:
    """Filter event_publisher.emit call_args to the InboundCommitted payload list.

    node_lifecycle emits a timeline_snapshot on every enter; we only want the
    "InboundCommitted emit" behavior here, so filter out the noise.
    """
    import json

    return [
        json.loads(c.args[0])
        for c in pub.emit.call_args_list
        if json.loads(c.args[0]).get("role") == "inbound_committed"
    ]


def _fake_llm(summary: str = "synthetic compaction summary") -> Any:
    """Mock LLM — bind_tools(...).ainvoke returns AIMessage(content=summary),
    matching the call shape of generate_summary (same tool binding as the main llm node)."""
    llm = MagicMock()
    llm.bind_tools.return_value.ainvoke = AsyncMock(return_value=AIMessage(content=summary))
    return llm


def _make_runtime(
    *,
    ops_pool: AsyncConnectionPool | None = None,
    inbound_listener: RedisInboundListener | None = None,
    llm: Any | None = None,
    event_publisher: Any | None = None,
    hosted: bool = False,
) -> Runtime[AvaContext]:
    """test helper: assemble AvaContext into Runtime.

    `ops_pool=None` takes the container early-return path;
    `inbound_listener` is only needed when `_wait_for_batch` enters IDLING wait, most
    tests only run first-claim which goes through SELECT to get the batch without waiting,
    so default `None` is fine.

    InboundCommitted SSE fan-out goes through `ctx.event_publisher.emit`; default to a MagicMock
    so the node's `assert ctx.event_publisher` passes; tests verifying InboundCommitted pass their own
    mock to assert `pub.emit.call_args_list`.

    `hosted=True` is the agent-runner-as-server runtime: claim ends the turn
    instead of parking. Default False keeps every existing test on the process
    path.
    """
    ctx = AvaContext(
        ops_pool=ops_pool,
        inbound_listener=inbound_listener,
        llm=llm if llm is not None else _fake_llm(),
        event_publisher=event_publisher if event_publisher is not None else MagicMock(),
        hosted=hosted,
    )
    return Runtime(context=ctx)


def _insert_inbound_kind(
    db: psycopg.Connection,
    tid: int,
    content: str,
    kind: str,
    source: str = "system",
) -> int:
    """Directly INSERT an inbound of any kind (bypasses the chat-only helper in shared/db.py)."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (tid, content, kind, source),
        )
        new_id = cur.fetchone()[0]  # type: ignore[index]
    db.commit()
    return new_id


async def _set_agent_status_async(pool: "AsyncConnectionPool", agent_id: int, status: str) -> None:
    """UPDATE agents_meta.status via `pool` — same pool claim_node uses.

    Eliminates the sync db_conn -> async aops_pool visibility window that
    causes the CI-only `idling != restarting` flake.  The CAS-free
    unconditional UPDATE is appropriate for test setup (prod uses CAS because
    concurrent lifecycle ops can race; a test setting up a single agent owns
    the row).
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE agents_meta SET status = %s WHERE id = %s",
            (status, agent_id),
        )
        if cur.rowcount != 1:
            await cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
            actual_row = await cur.fetchone()
            actual = actual_row[0] if actual_row is not None else "<row missing>"
            raise AssertionError(
                f"_set_agent_status_async: agent {agent_id} not updated "
                f"(rowcount={cur.rowcount}, actual status={actual!r})"
            )


async def _insert_inbound_kind_async(
    pool: "AsyncConnectionPool",
    agent_id: int,
    content: str,
    kind: str,
    source: str = "system",
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


async def _await_inbound_visible(pool: AsyncConnectionPool, inbound_id: int) -> None:
    """Block until a `db_conn`-committed inbound row is visible on `pool`.

    Setup writes go through the sync `db_conn`; `claim_node` claims the batch
    through `aops_pool` (a different connection). Under `-n auto` there is a
    cross-connection window where a just-committed row is not yet visible on the
    pool, so a single `claim_node` call can read a partial batch and skip the
    lifecycle flip — surfacing later as a baffling `assert 'idling' ==
    'restarting'`. Prod never hits this: claim is Redis-pub/sub-driven and re-claims
    on the next wake, so the still-pending row is picked up. This barrier mirrors
    that guarantee for the test's one-shot call. Waiting on the LAST-committed
    setup row suffices: `db_conn` commits sequentially, so its visibility implies
    every earlier setup write (status, prior inbounds) is visible too.
    """
    for _ in range(100):
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT 1 FROM inbound_messages WHERE id = %s", (inbound_id,))
            if await cur.fetchone() is not None:
                return
        await asyncio.sleep(0.02)
    raise AssertionError(f"inbound {inbound_id} not visible on the claim pool after 2s")


async def _await_status(pool: AsyncConnectionPool, agent_id: int, expected: str) -> None:
    """Poll `agents_meta.status` on `pool` until it equals `expected`.

    The restart arm flips status through `ctx.ops_pool` (= `aops_pool`); read it
    back on the same pool the flip was written on. Raises if `expected` is not
    reached in 2s, reporting the observed status trail.

    (This helper once carried a heavy CI-flake forensic dump for an intermittent
    `idling != restarting`. That flake was a reused-id collision, killed at the
    source by the monotonic-id contract — see
    decisions/2026-06-30-monotonic-test-ids.md — so
    the dump is gone; the status trail is enough for any residual failure.)
    """
    seen: list[object] = []
    row: tuple[object, ...] | None = None
    for _ in range(250):
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
            row = await cur.fetchone()
        current = row[0] if row is not None else None
        if not seen or seen[-1] != current:
            seen.append(current)
        if current == expected:
            return
        await asyncio.sleep(0.02)
    actual = row[0] if row is not None else None
    raise AssertionError(
        f"agent {agent_id} status {actual!r} != {expected!r} after 5s (status trail: {seen})"
    )


def _set_agent_status(db: psycopg.Connection, agent_id: int, status: str) -> None:
    """UPDATE agents_meta.status with rowcount assertion.

    Fail-fast on 0 rows — a silent no-op UPDATE (wrong / nonexistent agent_id)
    would otherwise surface much later as a baffling ``assert <spawn-value> ==
    <intended>`` status mismatch far from its cause.
    """
    with db.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = %s WHERE id = %s", (status, agent_id))
        assert cur.rowcount == 1, (
            f"_set_agent_status: agent {agent_id} not updated (rowcount={cur.rowcount})"
        )
    db.commit()


def _config(tid: int) -> RunnableConfig:
    return {"configurable": {"thread_id": str(tid)}}


@pytest.fixture
def running_agent(db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch):
    """factory: spawn agent row + directly UPDATE to 'running'.

    Simulates production: the bootstrap CAS has already created a running row
    before the process enters claim_node. Dispatch tests need that row for the
    same lifecycle invariant as a real agent.
    """

    def _make() -> int:
        tid = spawn_agent()
        _set_agent_status(db_conn, tid, "running")
        return tid

    return _make


async def test_claim_first_entry_keeps_boot_claim_running(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
    monkeypatch: pytest.MonkeyPatch,
):
    """The bootstrap claim already sets running before the first graph entry."""
    tid = spawn_agent()
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'running' WHERE id = %s", (tid,))
    db_conn.commit()
    insert_inbound_message(db_conn, tid, "hello", source="user")

    await claim_node(
        AgentState(),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        row = cur.fetchone()
    assert row is not None and row[0] == "running"


async def test_claim_subsequent_entry_does_not_disturb_running(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
    monkeypatch: pytest.MonkeyPatch,
):
    """A subsequent graph entry leaves its already-running row untouched."""
    tid = spawn_agent()
    _set_agent_status(db_conn, tid, "running")
    insert_inbound_message(db_conn, tid, "hello", source="user")

    # Don't raise — status is already 'running' when the next turn enters claim_node, 0-row no-op
    await claim_node(
        AgentState(),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        row = cur.fetchone()
    assert row is not None and row[0] == "running"


async def test_claim_inbound_batch_stamps_claimed_at(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
):
    """claim_inbound_batch stamps claimed_at = now() on every grabbed row
    (chat -> claimed, lifecycle kinds -> done), so the gateway can compute
    creation -> pickup latency (claimed_at - created_at) after the fact. A row
    nobody claimed keeps claimed_at NULL."""
    from agent.db import claim_inbound_batch

    tid = spawn_agent()
    chat_id = insert_inbound_message(db_conn, tid, "hello", source="user")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, %s, 'terminate', 'system') RETURNING id",
            (tid, "bye"),
        )
        term_row = cur.fetchone()
        assert term_row is not None
        term_id = term_row[0]
    db_conn.commit()

    rows = await claim_inbound_batch(aops_pool, tid)
    by_id = {r.id: r for r in rows}
    assert set(by_id) == {chat_id, term_id}
    # RETURNING carries claimed_at for both kinds (chat + lifecycle).
    assert by_id[chat_id].claimed_at is not None
    assert by_id[term_id].claimed_at is not None

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT id, status, claimed_at FROM inbound_messages WHERE id = ANY(%s) ORDER BY id",
            ([chat_id, term_id],),
        )
        state = cur.fetchall()
    assert [(r[0], r[1]) for r in state] == [(chat_id, "claimed"), (term_id, "done")]
    assert all(r[2] is not None for r in state)

    # A fresh unclaimed row keeps claimed_at NULL.
    fresh_id = insert_inbound_message(db_conn, tid, "later", source="user")
    with db_conn.cursor() as cur:
        cur.execute("SELECT claimed_at FROM inbound_messages WHERE id = %s", (fresh_id,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] is None


async def test_claim_chat_kind_appends_humanmessage_with_envelope(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """chat inbound → claim returns Command(goto='before_llm'), update.messages
    contains HumanMessage after envelope wrapping.

    state.messages empty (agent first round) → claim simultaneously injects SystemMessage as
    messages[0] for prompt cache hit across restarts.
    """
    tid = spawn_agent()
    insert_inbound_message(db_conn, tid, "hello", source="user")

    cmd = await claim_node(
        AgentState(active_task_id=99),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    assert isinstance(cmd, Command)
    assert cmd.goto == "before_llm"
    msgs = cmd.update["messages"]  # type: ignore[index]
    # Claim appends the batch and nothing else — the standing head (SystemMessage
    # plus the context notes) is laid down by `init_context` before claim runs.
    assert len(msgs) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert isinstance(msgs[0], HumanMessage)
    assert msgs[0].content.startswith("User ")  # pyright: ignore[reportUnknownMemberType]
    assert "hello" in msgs[0].content  # pyright: ignore[reportUnknownMemberType]
    assert cmd.update["halted"] is False  # type: ignore[index]
    assert cmd.update["active_task_id"] is None  # type: ignore[index]


async def test_claim_chat_expands_slash_command(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """A `/<name> ...` chat inbound is expanded by the claim node into the
    command's template + the user's note before being wrapped for the model."""
    tid = spawn_agent()
    insert_inbound_message(db_conn, tid, "/recap just the PRs", source="user")

    cmd = await claim_node(
        AgentState(),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    content = cmd.update["messages"][-1].content  # type: ignore[index]
    # Envelope attributes the sender; the expansion body is source-neutral.
    assert content.startswith("User ")  # pyright: ignore[reportUnknownMemberType]
    assert "Command /recap:" in content
    assert "Additional message: just the PRs" in content


async def test_claim_compact_summary_replaces_messages_with_remove_sentinel(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
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
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener, llm=fake_llm),
        _config(tid),
    )

    fake_llm.bind_tools.return_value.ainvoke.assert_not_called()  # agent-authored summary, skip LLM
    tail = _compact_tail(cmd.update)
    assert isinstance(tail[0], HumanMessage)
    assert tail[0].content == compose_summary_message(summary_text)  # pyright: ignore[reportUnknownMemberType]


async def test_claim_compact_summary_bumps_compact_version(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
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
        _make_runtime(
            ops_pool=aops_pool,
            inbound_listener=aredis_inbound_listener,
            llm=_fake_llm("LLM should not be called"),
        ),
        _config(tid),
    )

    assert cmd.update["compact"].version == 6  # type: ignore[index]


async def test_claim_compact_request_calls_backend_llm(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
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
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener, llm=fake_llm),
        _config(tid),
    )

    fake_llm.bind_tools.return_value.ainvoke.assert_called_once()
    tail = _compact_tail(cmd.update)
    assert tail[0].content == compose_summary_message("LLM-generated summary")  # pyright: ignore[reportUnknownMemberType]


async def test_claim_compact_request_empty_conversation_consumed_as_noop(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
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
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener, llm=fake_llm),
        _config(tid),
    )

    fake_llm.bind_tools.return_value.ainvoke.assert_not_called()
    assert not any(isinstance(m, RemoveMessage) for m in cmd.update["messages"])  # type: ignore[index]


async def test_claim_compact_request_retries_then_raises_compaction_failed(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
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

    from agent.hooks.compact import COMPACT_MAX_ATTEMPTS, CompactionFailedError

    with pytest.raises(CompactionFailedError, match="no usable summary across"):
        await claim_node(
            state,
            _make_runtime(
                ops_pool=aops_pool,
                inbound_listener=aredis_inbound_listener,
                llm=llm,
            ),
            _config(tid),
        )
    assert failing.await_count == COMPACT_MAX_ATTEMPTS


async def test_claim_compact_request_retries_then_succeeds(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
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
        _make_runtime(
            ops_pool=aops_pool,
            inbound_listener=aredis_inbound_listener,
            llm=llm,
        ),
        _config(tid),
    )

    assert llm.bind_tools.return_value.ainvoke.await_count == 2
    tail = _compact_tail(cmd.update)
    assert tail[0].content == compose_summary_message("retried summary")  # pyright: ignore[reportUnknownMemberType]


# ────────────────────────────────────────────────────────────────────────
# Memory index injection — the standing MEMORY.md pointer is put in front of
# the agent at the two context-(re)establishment moments: cold start (first
# human message) and right after a compact summary. Persists in between;
# re-injected after compact because REMOVE_ALL wipes the prior copy.


def test_memory_index_note_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from ava_builtins.plugins.ava_memory import notes as _memory_inject

    monkeypatch.setattr(_memory_inject, "memory_dir", lambda: tmp_path)
    monkeypatch.setattr(
        _memory_inject,
        "settings",
        SimpleNamespace(agent=SimpleNamespace(memory_index_inject_enabled=True)),
    )
    (tmp_path / "MEMORY.md").write_text("prod=~/.ava/source\n- people -> people/", encoding="utf-8")

    note = _memory_inject.memory_index_note()
    assert note is not None
    assert note.additional_kwargs["ava_msg_type"] == "system_note"  # pyright: ignore[reportUnknownMemberType]
    assert note.additional_kwargs["ava_note_tag"] == "memory"  # pyright: ignore[reportUnknownMemberType]
    assert (
        "prod=~/.ava/source" in note.content  # pyright: ignore[reportUnknownMemberType]
    )  # raw file content carried through  # pyright: ignore[reportUnknownMemberType]


def test_memory_index_note_none_when_absent_empty_or_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from ava_builtins.plugins.ava_memory import notes as _memory_inject

    monkeypatch.setattr(_memory_inject, "memory_dir", lambda: tmp_path)
    monkeypatch.setattr(
        _memory_inject,
        "settings",
        SimpleNamespace(agent=SimpleNamespace(memory_index_inject_enabled=True)),
    )
    assert _memory_inject.memory_index_note() is None  # absent
    (tmp_path / "MEMORY.md").write_text("   \n\t\n", encoding="utf-8")
    assert _memory_inject.memory_index_note() is None  # whitespace-only
    (tmp_path / "MEMORY.md").write_text("real content", encoding="utf-8")
    monkeypatch.setattr(
        _memory_inject,
        "settings",
        SimpleNamespace(agent=SimpleNamespace(memory_index_inject_enabled=False)),
    )
    assert _memory_inject.memory_index_note() is None  # disabled


def _compact_tail(update):
    """Assert the transport a compaction now uses — the window is cleared and
    rebuilding the standing head is handed to `init_context` — and return the
    parked tail, which is what this claim batch decided belongs behind it."""
    from langchain_core.messages.modifier import RemoveMessage as _Rm

    msgs = update["messages"]
    assert len(msgs) == 1, f"expected the sentinel alone, got {len(msgs)} messages"  # pyright: ignore[reportUnknownArgumentType]
    assert isinstance(msgs[0], _Rm)
    assert msgs[0].id == REMOVE_ALL_MESSAGES  # pyright: ignore[reportUnknownMemberType]
    return update["context_reset"].tail  # pyright: ignore[reportUnknownMemberType]


async def test_claim_compact_summary_with_chat_in_same_batch_defers_chat(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
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
    chat_id = insert_inbound_message(db_conn, tid, "user during compact", source="user")

    sys_msg = SystemMessage(content="<test sys prompt>")
    initial_msgs: list[AnyMessage] = [
        sys_msg,
        *(HumanMessage(content=f"old-{i}") for i in range(7)),
    ]
    state = AgentState(messages=initial_msgs)

    cmd = await claim_node(
        state,
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    # The compact is a clean wipe: the parked tail is the summary alone.
    tail = _compact_tail(cmd.update)
    assert len(tail) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert tail[0].content == compose_summary_message(summary_text)  # pyright: ignore[reportUnknownMemberType]
    assert cmd.goto == "init_context"
    # The co-batched chat is deferred, not dropped: back to pending so the
    # next claim delivers it in the fresh context.
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT status, claimed_at FROM inbound_messages WHERE id = %s",
            (chat_id,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "pending"
    assert row[1] is None


async def test_claim_compact_summary_finalizes_claimed_history(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
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
    chat1 = insert_inbound_message(db_conn, tid, "user q1", source="user")
    chat2 = insert_inbound_message(db_conn, tid, "user q2", source="user")
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
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
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


async def test_claim_unknown_kind_raises(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
    monkeypatch: pytest.MonkeyPatch,
):
    """Unrecognized inbound kind = framework / DB schema desync — immediately raise,
    do not silently swallow bugs by 'defaulting to chat processing'.

    The DB CHECK constraint prevents production unknown kind, so it cannot be constructed
    via INSERT path; use monkeypatch to directly feed ClaimedInbound to claim_node to verify
    the dispatch's `case _:` fallback branch.
    """
    from agent.db import ClaimedInbound

    tid = spawn_agent()

    async def fake_claim(_db, _tid):
        return [ClaimedInbound(id=99, agent_id=tid, content="x", kind="bogus", source="system")]

    monkeypatch.setattr("agent.graph._claim.claim_inbound_batch", fake_claim)  # pyright: ignore[reportUnknownArgumentType]

    with pytest.raises(ValueError, match="Unknown inbound kind"):
        await claim_node(
            AgentState(),
            _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
            _config(tid),
        )


async def test_claim_terminate_kind_appends_lifecycle_marker_and_routes_to_end(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """terminate inbound → claim appends lifecycle marker (HumanMessage containing
    'You are terminated by {source}' text + ava_msg_type='lifecycle' metadata)
    + goto END with exit_requested=True, so the per-turn runloop returns
    (instead of re-invoking) and the process exits naturally."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "terminate", source="user")

    cmd = await claim_node(
        AgentState(),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )
    assert cmd.goto == END
    assert cmd.update["exit_requested"] is True  # type: ignore[index]
    msgs = cmd.update["messages"]  # type: ignore[index]
    # Claim appends the lifecycle marker alone; the head belongs to `init_context`.
    assert len(msgs) == 1  # pyright: ignore[reportUnknownArgumentType]
    lifecycle = msgs[0]
    assert isinstance(lifecycle, HumanMessage)
    # HumanMessage.content type union (str | list[blocks]); system_note_message
    # passes str, narrow with string methods
    assert isinstance(lifecycle.content, str)  # pyright: ignore[reportUnknownMemberType]
    content = lifecycle.content
    assert "You are terminated by user" in content
    # marker content has timestamp prefix + [system] in single brackets (now_timestamp already has square brackets, no nesting)
    assert content.startswith("[")  # timestamp start: e.g. [2026-...
    assert "[system]" in content
    assert "[system [" not in content  # anti-regression: double bracket bug
    assert lifecycle.additional_kwargs.get("ava_msg_type") == "system_note"  # pyright: ignore[reportUnknownMemberType]
    assert lifecycle.additional_kwargs.get("ava_note_tag") == "lifecycle_terminate"  # pyright: ignore[reportUnknownMemberType]


async def test_claim_turn_boundary_ends_invocation_instead_of_waiting(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """One graph invocation = one TURN: a claim pass that finds nothing to do
    AFTER this invocation already routed work (turn_active=True) ends the
    invocation (goto END, exit_requested stays False → the runloop re-invokes)
    instead of blocking in _wait_for_batch — that is what closes the per-turn
    root span at the turn boundary. Would hang here if it blocked, so a plain
    return IS the lock."""
    tid = spawn_agent()

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True, turn_active=True),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )
    assert cmd.goto == END
    # Turn boundary only: no process exit, no other state touched.
    assert cmd.update == {"turn_active": False}


async def test_claim_hosted_ends_turn_instead_of_parking(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """Hosted mode has no process to park: a fresh invocation (turn_active=False)
    that finds nothing must goto END with `turn_idle`, not enter the IDLING wait.

    The process-mode twin of this call
    (`test_claim_fresh_invocation_waits_then_runs_turn`) blocks in
    `_wait_for_batch`. Here nothing fakes the wait, and no inbound is ever
    inserted — so if the hosted branch were missing, this test would hang on a
    real Redis wait rather than fail. Returning at all IS the lock.

    `exit_requested` stays False: an idle agent is not a terminated one — the
    host drops the task and re-creates it on the next wake.
    """
    tid = spawn_agent()

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True, turn_active=False),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener, hosted=True),
        _config(tid),
    )

    assert cmd.goto == END
    assert cmd.update == {"turn_active": False, "turn_idle": True}


async def test_claim_hosted_never_enters_idling_status(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """The hosted claim branch never writes status: `_wait_for_batch` would flip
    to IDLING before it blocks, while the host owns running/idling around the
    task itself. A direct hosted claim therefore leaves its running row alone."""
    tid = spawn_agent()
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'running' WHERE id = %s", (tid,))
    db_conn.commit()

    await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True, turn_active=False),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener, hosted=True),
        _config(tid),
    )

    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        row = cur.fetchone()
    assert row is not None
    # The row was already running; the hosted claim branch must return without
    # touching it.
    assert row[0] == "running", f"hosted claim left status {row[0]!r}"


async def test_claim_hosted_still_dispatches_an_available_batch(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """Hosted mode changes only the empty-batch branch. When the first SELECT
    finds work, dispatch is byte-for-byte the process path — the turn runs, and
    `turn_idle` is NOT set (the host must re-invoke, not end the task)."""
    tid = spawn_agent()
    insert_inbound_message(db_conn, tid, "hello", kind="chat", source="user")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True, turn_active=False),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener, hosted=True),
        _config(tid),
    )

    assert cmd.goto == "before_llm"
    assert cmd.update["turn_active"] is True  # type: ignore[index]
    assert cmd.update.get("turn_idle") in (None, False)  # type: ignore[union-attr]


async def test_claim_fresh_invocation_waits_then_runs_turn(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
    monkeypatch: pytest.MonkeyPatch,
):
    """A fresh invocation's claim (turn_active=False) still does the long wait
    — the claim long-await works on re-entry after a turn-boundary END. The
    wait is faked to return a chat batch; claim must dispatch it and route to
    before_llm with turn_active=True so the NEXT idle pass ends the turn."""
    from agent.db import ClaimedInbound

    tid = spawn_agent()
    waited: list[int] = []

    async def fake_wait(_ctx: object, agent_id: int) -> list[ClaimedInbound]:
        waited.append(agent_id)
        return [ClaimedInbound(id=1, agent_id=tid, content="hi", kind="chat", source="user")]

    monkeypatch.setattr("agent.graph._claim._wait_for_batch", fake_wait)  # pyright: ignore[reportUnknownArgumentType]

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True, turn_active=False),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )
    assert waited == [tid], "fresh invocation must enter the long wait, not END"
    assert cmd.goto == "before_llm"
    assert cmd.update["turn_active"] is True  # type: ignore[index]
    assert cmd.update["exit_requested"] is False  # type: ignore[index]


async def test_claim_cancel_kind_halts_to_idle_without_marker(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """cancel inbound → pause: halted=True + re-enter CLAIM (-> idle), NOT END
    (process stays alive). No lifecycle marker (a pause leaves no trace); a
    Cancelled SSE is emitted so the live UI clears turn-active state."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "cancel", source="user")
    pub = MagicMock()

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(
            ops_pool=aops_pool, inbound_listener=aredis_inbound_listener, event_publisher=pub
        ),
        _config(tid),
    )
    # re-enter claim to idle (alive), not END (dead)
    assert cmd.goto == "claim"
    assert cmd.goto != END
    assert cmd.update["halted"] is True  # type: ignore[index]
    # no marker appended — pause is silent (state.messages already had the sys msg)
    assert cmd.update["messages"] == []  # type: ignore[index]
    # Cancelled emitted for the live view
    assert pub.emit.call_count >= 1
    assert any("cancelled" in str(c.args[0]).lower() for c in pub.emit.call_args_list)


async def test_claim_cancel_with_chat_cobatch_wakes_to_process_chat(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """User sends a chat, then clicks Stop while the agent's code is executing;
    both land pending and are claimed in one batch (the interrupt aborts the
    in-flight node, which returns to claim). The cancel stopped the OLD work, but
    the fresh chat is new intent — claim must wake straight to before_llm with
    halted=False and the chat committed, NOT idle. Regression: the cancel arm
    used to unconditionally set halted=True + re-enter claim, stranding the
    co-batched chat in state.messages (surfaced to the UI via InboundCommitted)
    until some later inbound happened to arrive — "message picked up but the
    agent never continued"."""
    tid = spawn_agent()
    # chat first (older id), then cancel — the real sequence: message queued,
    # then Stop pressed mid-execution.
    insert_inbound_message(db_conn, tid, "please also do X", source="user")
    cancel_id = _insert_inbound_kind(db_conn, tid, "", "cancel", source="user")
    await _await_inbound_visible(aops_pool, cancel_id)
    pub = MagicMock()

    cmd = await claim_node(
        # non-empty state = an in-flight turn (not cold start); the exec/llm
        # node already aborted and returned here with halted=True.
        AgentState(messages=[SystemMessage(content="sys"), HumanMessage(content="earlier")]),
        _make_runtime(
            ops_pool=aops_pool, inbound_listener=aredis_inbound_listener, event_publisher=pub
        ),
        _config(tid),
    )

    # wake to run the LLM on the new chat, not idle
    assert cmd.goto == "before_llm"
    assert cmd.update["halted"] is False  # type: ignore[index]
    # the co-batched chat is committed and drives the turn
    msgs = cmd.update["messages"]  # type: ignore[index]
    assert len(msgs) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert isinstance(msgs[0], HumanMessage)
    assert "please also do X" in msgs[0].content  # pyright: ignore[reportUnknownMemberType]
    # Cancelled still emitted so the live UI clears the interrupted turn's state
    assert any("cancelled" in str(c.args[0]).lower() for c in pub.emit.call_args_list)


async def test_claim_cancel_before_chat_cobatch_wakes_to_process_chat(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """Same as above but the cancel is the OLDER row (user clicks Stop, then
    sends a new message while both are still pending). The wake decision is
    order-independent — a chat anywhere in the cancel batch means new intent to
    process, so goto=before_llm + halted=False regardless of insertion order."""
    tid = spawn_agent()
    # cancel first (older id), then chat
    _insert_inbound_kind(db_conn, tid, "", "cancel", source="user")
    chat_id = insert_inbound_message(db_conn, tid, "new instruction", source="user")
    await _await_inbound_visible(aops_pool, chat_id)

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys"), HumanMessage(content="earlier")]),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    assert cmd.goto == "before_llm"
    assert cmd.update["halted"] is False  # type: ignore[index]
    msgs = cmd.update["messages"]  # type: ignore[index]
    assert len(msgs) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert isinstance(msgs[0], HumanMessage)
    assert "new instruction" in msgs[0].content  # pyright: ignore[reportUnknownMemberType]


async def test_claim_cancel_batched_with_terminate_terminate_wins(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """cancel + terminate in the same claim batch (user clicks Stop then
    Terminate before claim runs) → terminate WINS: goto END (process exits), not
    the cancel idle. Both rows are claimed/done in one pass; the pause must not
    swallow the stronger kill. Regression for the cancel-over-terminate
    precedence inversion."""
    tid = spawn_agent()
    # insertion order shouldn't matter; put cancel first to make the override tempting
    _insert_inbound_kind(db_conn, tid, "", "cancel", source="user")
    _insert_inbound_kind(db_conn, tid, "", "terminate", source="user")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )
    assert cmd.goto == END  # terminate wins; agent exits, cancel does not keep it alive
    # the terminate lifecycle marker is still committed
    msgs = cmd.update["messages"]  # type: ignore[index]
    assert any(
        isinstance(m, HumanMessage)
        and isinstance(m.content, str)  # pyright: ignore[reportUnknownMemberType]
        and "You are terminated" in m.content
        for m in msgs
    )


async def test_claim_lifecycle_marker_drops_timestamp_when_disabled(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
    monkeypatch: pytest.MonkeyPatch,
):
    """settings.general.message_timestamps=False → the lifecycle marker has no leading
    timestamp; it starts straight at `[system]` with no stray space."""
    monkeypatch.setattr(settings.general, "message_timestamps", False)
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "terminate", source="user")

    cmd = await claim_node(
        AgentState(),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )
    content = cmd.update["messages"][-1].content  # type: ignore[index]
    assert content.startswith("[system] You are terminated by user")  # pyright: ignore[reportUnknownMemberType]


async def test_claim_terminate_self_renders_by_yourself(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """source='self' (ava.self.terminate() suicide) → marker text spells 'by yourself'
    instead of 'by self', more accurately expressing 'agent shuts itself down' semantics."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "terminate", source="self")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )
    assert cmd.goto == END
    msgs = cmd.update["messages"]  # type: ignore[index]
    lifecycle = msgs[0]
    assert "You are terminated by yourself" in lifecycle.content  # pyright: ignore[reportUnknownMemberType]


async def test_claim_self_terminate_with_chat_cobatch_abandons_terminate(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """The terminate race: an agent calls ava.self.terminate() while a peer
    message arrives at the same moment; both land pending and are claimed in one
    batch. The chat arrived during the very turn the agent terminated in — the
    agent never saw it (its LLM context only holds messages claimed at the turn's
    start), so the death decision must be abandoned: the chat is committed and
    drives a wake (goto before_llm), the terminate is a consumed no-op (no
    marker, no END), the agent stays alive to process the message."""
    tid = spawn_agent()
    insert_inbound_message(db_conn, tid, "peer message during suicide", source="agent:1")
    terminate_id = _insert_inbound_kind(db_conn, tid, "", "terminate", source="self")
    await _await_inbound_visible(aops_pool, terminate_id)

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    assert cmd.goto == "before_llm"  # wake to process the chat, not END
    assert cmd.goto != END
    assert cmd.update["halted"] is False  # type: ignore[index]
    msgs = cmd.update["messages"]  # type: ignore[index]
    assert len(msgs) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert isinstance(msgs[0], HumanMessage)
    assert "peer message during suicide" in msgs[0].content  # pyright: ignore[reportUnknownMemberType]
    # no terminate lifecycle marker — the death was abandoned
    assert not any("You are terminated" in m.content for m in msgs)  # pyright: ignore[reportUnknownMemberType]


async def test_claim_self_terminate_with_older_chat_cobatch_abandons_terminate(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """Order-independence half of the self-terminate race: the chat can arrive
    just BEFORE the agent calls ava.self.terminate() (sitting pending during the
    terminating turn). It is still unseen by the agent, so it vetoes the death
    just the same — a co-batched chat is by construction not in the LLM context
    (only chats claimed in an earlier batch are)."""
    tid = spawn_agent()
    chat_id = insert_inbound_message(db_conn, tid, "queued before the suicide", source="user")
    await _await_inbound_visible(aops_pool, chat_id)
    _insert_inbound_kind(db_conn, tid, "", "terminate", source="self")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    assert cmd.goto == "before_llm"
    msgs = cmd.update["messages"]  # type: ignore[index]
    assert any("queued before the suicide" in m.content for m in msgs)  # pyright: ignore[reportUnknownMemberType]
    assert not any("You are terminated" in m.content for m in msgs)  # pyright: ignore[reportUnknownMemberType]


async def test_claim_external_terminate_with_newer_chat_abandons_terminate(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """External terminate (user/admin/peer) with a chat that arrived AFTER it:
    the chat is the most recent genuine intent — same recency rule as the
    resurrect veto — so the terminate is abandoned (consumed no-op, no marker)
    and the agent wakes to process the chat."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "terminate", source="user")
    chat_id = insert_inbound_message(db_conn, tid, "message after the kill", source="user")
    await _await_inbound_visible(aops_pool, chat_id)

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    assert cmd.goto == "before_llm"
    assert cmd.goto != END
    msgs = cmd.update["messages"]  # type: ignore[index]
    assert any("message after the kill" in m.content for m in msgs)  # pyright: ignore[reportUnknownMemberType]
    assert not any("You are terminated" in m.content for m in msgs)  # pyright: ignore[reportUnknownMemberType]


async def test_claim_external_terminate_with_older_chat_still_dies(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """Regression guard: a deliberate external kill is NOT vetoed by chats that
    predate it — the actor decided with the pending queue visible, so the
    terminate is the latest intent and the agent dies (END + marker). The
    pre-death chat is committed to history as before; it is visible after a
    resurrect."""
    tid = spawn_agent()
    chat_id = insert_inbound_message(db_conn, tid, "old message before the kill", source="user")
    await _await_inbound_visible(aops_pool, chat_id)
    _insert_inbound_kind(db_conn, tid, "", "terminate", source="user")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    assert cmd.goto == END
    msgs = cmd.update["messages"]  # type: ignore[index]
    assert any("You are terminated by user" in m.content for m in msgs)  # pyright: ignore[reportUnknownMemberType]
    assert any("old message before the kill" in m.content for m in msgs)  # pyright: ignore[reportUnknownMemberType]


async def test_claim_terminate_vetoed_by_pending_inbound_after_claim(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
    monkeypatch: pytest.MonkeyPatch,
):
    """The other half of the race: the batch (terminate alone) is claimed, but a
    message lands in the queue before the exit is committed. The claim node's
    final recheck must veto the death and re-enter claim so the fresh message is
    dispatched — the terminate row is already consumed ('done'), no marker, no
    END, and the chat stays pending for the re-entered claim to pick up. The
    message arrives after claim_inbound_batch returns, which is simulated by
    monkeypatching claim to return only the terminate row while a newer chat
    stays pending in the table."""
    from agent.db import ClaimedInbound

    tid = spawn_agent()
    terminate_id = _insert_inbound_kind(db_conn, tid, "", "terminate", source="self")
    chat_id = insert_inbound_message(db_conn, tid, "message after the claim", source="user")
    await _await_inbound_visible(aops_pool, chat_id)

    async def fake_claim(_pool, _agent_id):
        # Faithful to claim_inbound_batch: the grab marks lifecycle rows 'done'
        # atomically, so the vetoed terminate is consumed and never retried.
        async with _pool.connection() as conn, conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
            await cur.execute(  # pyright: ignore[reportUnknownMemberType]
                "UPDATE inbound_messages SET status = 'done' WHERE id = %s", (terminate_id,)
            )
        return [
            ClaimedInbound(
                id=terminate_id, agent_id=tid, content="", kind="terminate", source="self"
            )
        ]

    monkeypatch.setattr("agent.graph._claim.claim_inbound_batch", fake_claim)  # pyright: ignore[reportUnknownArgumentType]

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    # re-enter claim to dispatch the fresh message — not END, not a wake
    assert cmd.goto == "claim"
    assert cmd.goto != END
    # no terminate marker was committed
    assert cmd.update["messages"] == []  # type: ignore[index]
    # the newer chat is still pending for the re-entered claim to pick up
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM inbound_messages WHERE agent_id = %s AND kind = 'chat'",
            (tid,),
        )
        chat_row = cur.fetchone()
        assert chat_row is not None
        assert chat_row[0] == "pending"
    # the vetoed terminate row is consumed, never retried
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM inbound_messages WHERE agent_id = %s AND kind = 'terminate'",
            (tid,),
        )
        term_row = cur.fetchone()
        assert term_row is not None
        assert term_row[0] == "done"


@pytest.mark.flaky  # poll _await_status for claim_node status transition
async def test_claim_restart_kind_marks_restarting_and_no_message(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
    monkeypatch: pytest.MonkeyPatch,
):
    """restart inbound → mark agents.status running→restarting + goto END,
    does **not** append message (old process writing 'has been restarted' is writing for the future,
    left to the new process's 'restart_completed' kind dispatch)."""
    tid = spawn_agent()
    # simulate process already up: unclaimed idling → running (mark_agent_status 'restarting' expects from='running')
    _set_agent_status(db_conn, tid, "running")
    restart_id = _insert_inbound_kind(db_conn, tid, "", "restart", source="user")
    await _await_inbound_visible(aops_pool, restart_id)

    cmd = await claim_node(
        AgentState(
            messages=[SystemMessage(content="sys")]
        ),  # non-empty messages skip SystemMessage inject
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    assert cmd.goto == END
    # does **not** append any message (state.messages already has SystemMessage, no inject)
    assert cmd.update["messages"] == []  # type: ignore[index]
    # mid-task restart (halted=False) → committed halted=False, respawn wakes to resume
    assert cmd.update["halted"] is False  # type: ignore[index]
    # status switches to restarting, waiting for restarter respawn
    await _await_status(aops_pool, tid, "restarting")


@pytest.mark.flaky  # poll _await_status for claim_node status transition
async def test_claim_multiple_restart_in_one_batch_flips_status_once(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """Multiple restart inbounds pile up in the same batch (command backlog when agent is stuck) →
    running→restarting CAS runs only once. Before de-dup: after the first restart flips status to
    restarting, the second restart calls mark_agent_status(expected_from=RUNNING) hitting the already
    restarting status -> raises RuntimeError, claim_node crashes, old process does not exit cleanly,
    agent gets stuck and eventually the reaper flips it to terminated. After fix: the second restart
    skips CAS, claim_node returns normally with goto END, status is restarting."""
    tid = spawn_agent()
    _set_agent_status(db_conn, tid, "running")
    _insert_inbound_kind(db_conn, tid, "", "restart", source="user")
    r2 = _insert_inbound_kind(db_conn, tid, "", "restart", source="system:update")
    await _await_inbound_visible(aops_pool, r2)

    # before de-dup this step would raise (second CAS hits running->restarting mismatch)
    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    assert cmd.goto == END
    await _await_status(aops_pool, tid, "restarting")


@pytest.mark.flaky  # poll _await_status for claim_node status transition
async def test_claim_cas_loss_on_wake_flip_exits_cleanly(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
    monkeypatch: pytest.MonkeyPatch,
):
    """A concurrent lifecycle op winning the IDLING→RUNNING claim flip
    (terminate / reaper) used to raise RuntimeError out of the graph and
    crash the process — and a crash during a network outage cannot be
    resurrected (agent 2147, 2026-08-03: 4h dead; Task #688). The claim node
    must END cleanly instead, leaving the row to the owning controller."""
    import agent.graph._claim_batch as claim_batch

    tid = spawn_agent()
    _set_agent_status(db_conn, tid, "running")

    real_mark = claim_batch.mark_agent_status

    async def _cas_lost_on_wake(pool, agent_id, status, *, expected_from):
        if status == "running" and expected_from == "idling":
            # simulate the concurrent op: the row flips to terminated between
            # the idle flip and the claim flip
            with db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE agents_meta SET status = 'terminated' WHERE id = %s",
                    (tid,),
                )
            db_conn.commit()
            raise RuntimeError("simulated CAS loss (Task #688)")
        return await real_mark(pool, agent_id, status, expected_from=expected_from)  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(claim_batch, "mark_agent_status", _cas_lost_on_wake)  # pyright: ignore[reportUnknownArgumentType]

    listener = RedisInboundListener(settings.data_plane.redis_url, tid)

    async def _delayed_insert() -> None:
        # wait until claim_node has parked the agent idling, then wake it —
        # the wake makes the wait loop claim a batch and hit the flip CAS
        deadline = time.monotonic() + 5.0
        while True:
            with db_conn.cursor() as cur:
                cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
                row = cur.fetchone()
            assert row is not None
            if row[0] == "idling":
                break
            assert time.monotonic() < deadline, (
                f"claim_node did not switch status to idling within 5s (currently {row[0]!r})"
            )
            await asyncio.sleep(0.05)
        insert_inbound_message(db_conn, tid, "wake up", source="user")

    claim_task = asyncio.create_task(
        claim_node(
            AgentState(messages=[SystemMessage(content="sys")]),
            _make_runtime(ops_pool=aops_pool, inbound_listener=listener),
            _config(tid),
        )
    )
    insert_task = asyncio.create_task(_delayed_insert())

    try:
        cmd, _ = await asyncio.gather(claim_task, insert_task)
    finally:
        await listener.close()

    # clean END, no crash — the row stays terminated (another op owns it).
    # exit_requested=True: this END means process exit, so the per-turn
    # runloop must NOT re-invoke.
    assert cmd.goto == END
    assert cmd.update["exit_requested"] is True  # type: ignore[index]
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        row = cur.fetchone()
    assert row is not None and row[0] == "terminated"


async def test_claim_restart_kind_hosted_ends_turn_and_stays_runnable(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """Hosted restart: goto END with `restart_requested` (not `exit_requested`),
    no 'restarting' flip (there is no restarter to pick the row up — it must
    stay runnable), and the lifecycle marker renders inline (nothing else will:
    the restarter is what writes restart_completed in process mode)."""
    tid = spawn_agent()
    _set_agent_status(db_conn, tid, "running")
    restart_id = _insert_inbound_kind(db_conn, tid, "", "restart", source="user")
    await _await_inbound_visible(aops_pool, restart_id)

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener, hosted=True),
        _config(tid),
    )

    assert cmd.goto == END
    assert cmd.update["restart_requested"] is True  # type: ignore[index]
    assert cmd.update["exit_requested"] is False  # type: ignore[index]
    msgs = cmd.update["messages"]  # type: ignore[index]
    assert len(msgs) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert "You have been restarted by user" in msgs[0].content  # pyright: ignore[reportUnknownMemberType]
    # the row never left a runnable status — no restarter will come for it
    await _await_status(aops_pool, tid, "running")


async def test_claim_restart_kind_hosted_self_arms_no_process_respawn(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
    monkeypatch: pytest.MonkeyPatch,
):
    """A hosted self-restart must not arm the atexit self-respawn fallback —
    that fallback forks a replacement PROCESS, which hosted mode has no use
    for and which would double-claim with the dispatcher's turn task."""
    scheduled: list[int] = []
    monkeypatch.setattr("atexit.register", scheduled.append)
    tid = spawn_agent()
    _set_agent_status(db_conn, tid, "running")
    restart_id = _insert_inbound_kind(db_conn, tid, "", "restart", source="self")
    await _await_inbound_visible(aops_pool, restart_id)

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener, hosted=True),
        _config(tid),
    )

    assert cmd.goto == END
    assert cmd.update["restart_requested"] is True  # type: ignore[index]
    assert scheduled == []
    await _await_status(aops_pool, tid, "running")


async def test_claim_restart_completed_kind_appends_marker_and_continues(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """restart_completed inbound (delivered to the new process by respawn_agent) → claim appends
    lifecycle marker 'You have been restarted by {source}' + goto BEFORE_LLM.
    halted=False (mid-task before restart) → wakes up to resume interrupted work."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "restart_completed", source="user")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    assert cmd.goto == "before_llm"
    msgs = cmd.update["messages"]  # type: ignore[index]
    assert len(msgs) == 1  # pyright: ignore[reportUnknownArgumentType]
    lifecycle = msgs[0]
    assert isinstance(lifecycle, HumanMessage)
    assert "You have been restarted by user" in lifecycle.content  # pyright: ignore[reportUnknownMemberType]
    assert lifecycle.additional_kwargs.get("ava_msg_type") == "system_note"  # pyright: ignore[reportUnknownMemberType]
    assert lifecycle.additional_kwargs.get("ava_note_tag") == "lifecycle_restart"  # pyright: ignore[reportUnknownMemberType]


async def test_claim_system_note_kind_appends_system_note_and_continues(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """system_note inbound (task assign/update/reminder delivery) → claim appends
    a system note (NoteTag 'task') + goto BEFORE_LLM — never a chat peer message."""
    tid = spawn_agent()
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
            "VALUES (%s, %s, 'system_note', 'agent:405', %s::jsonb) RETURNING id",
            (
                tid,
                'Task #1 "my task" is now assigned to you (by agent #405).',
                '{"note_tag": "task", "task_id": 1}',
            ),
        )
        row = cur.fetchone()
        assert row is not None
        inbound_id = int(row[0])
    db_conn.commit()

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    assert cmd.goto == "before_llm"
    msgs = cmd.update["messages"]  # type: ignore[index]
    assert len(msgs) == 1  # pyright: ignore[reportUnknownArgumentType]
    note = msgs[0]
    assert isinstance(note, HumanMessage)
    assert 'Task #1 "my task" is now assigned to you (by agent #405).' in note.content  # pyright: ignore[reportUnknownMemberType]
    assert note.additional_kwargs.get("ava_msg_type") == "system_note"  # pyright: ignore[reportUnknownMemberType]
    assert note.additional_kwargs.get("ava_note_tag") == "task"  # pyright: ignore[reportUnknownMemberType]
    assert note.additional_kwargs.get("ava_task_id") == 1  # pyright: ignore[reportUnknownMemberType]
    assert cmd.update["active_task_id"] == 1  # pyright: ignore[index, reportOptionalSubscript]
    # The inbound row is consumed (done at claim, like other lifecycle kinds).
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM inbound_messages WHERE id = %s", (inbound_id,))
        status = cur.fetchone()
        assert status is not None and status[0] == "done"


async def test_claim_co_batched_task_notes_leave_usage_untagged(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """One LLM turn cannot be attributed to two distinct task notes."""
    tid = spawn_agent()
    with db_conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
            "VALUES (%s, %s, 'system_note', 'agent:405', %s::jsonb)",
            [
                (tid, 'Task #1 "first" was updated.', '{"note_tag": "task", "task_id": 1}'),
                (tid, 'Task #2 "second" was updated.', '{"note_tag": "task", "task_id": 2}'),
            ],
        )
    db_conn.commit()

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    assert cmd.goto == "before_llm"
    assert cmd.update["active_task_id"] is None  # type: ignore[index]


async def test_claim_system_note_unknown_tag_fails_loud(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """A system_note inbound carrying a non-NoteTag payload fails loud — a
    writer bug must not silently render as the wrong timeline chip."""
    tid = spawn_agent()
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
            "VALUES (%s, %s, 'system_note', 'system', %s::jsonb)",
            (tid, "boom", '{"note_tag": "not_a_tag"}'),
        )
    db_conn.commit()

    with pytest.raises(ValueError, match="not a NoteTag value"):
        await claim_node(
            AgentState(messages=[SystemMessage(content="sys")]),
            _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
            _config(tid),
        )


async def test_claim_restart_while_idle_commits_halted_true(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """external restart hits idle agent (halted=True) → committed halted=True,
    carrying 'no in-flight work before restart' across the respawn boundary for the new process to read."""
    tid = spawn_agent()
    _set_agent_status(db_conn, tid, "running")
    _insert_inbound_kind(db_conn, tid, "", "restart", source="system:update")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    assert cmd.goto == END
    assert cmd.update["halted"] is True  # type: ignore[index]


@pytest.mark.parametrize("source", ["self"])
async def test_claim_restart_self_source_commits_halted_false(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
    source: str,
):
    """agent-initiated restart (ava.self.restart) → even if the exec path
    set halted to True (turn-end semantics), committed halted must be False — agent has in-flight
    intent, must wake after respawn to confirm result and continue."""
    tid = spawn_agent()
    _set_agent_status(db_conn, tid, "running")
    _insert_inbound_kind(db_conn, tid, "", "restart", source=source)

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    assert cmd.goto == END
    assert cmd.update["halted"] is False  # type: ignore[index]


async def test_claim_restart_system_update_after_self_update_wakes(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """update_initiated=True in state (historical checkpoint from the removed
    self:update path) + the rollout quiesce system:update restart → committed
    halted must be False — an update-interrupted agent wakes after respawn, not
    silently idle."""
    tid = spawn_agent()
    _set_agent_status(db_conn, tid, "running")
    _insert_inbound_kind(db_conn, tid, "", "restart", source="system:update")

    cmd = await claim_node(
        AgentState(
            messages=[SystemMessage(content="sys")],
            halted=True,
            update_initiated=True,
        ),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    assert cmd.goto == END
    assert cmd.update["halted"] is False  # type: ignore[index]


async def test_claim_restart_idle_with_chat_cobatch_commits_halted_false(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """idle agent's same batch has both chat + restart (user message coinciding with restart) →
    chat already committed into messages, committed halted must be False, otherwise after respawn
    it would silently idle and this user message would never be answered."""
    tid = spawn_agent()
    _set_agent_status(db_conn, tid, "running")
    insert_inbound_message(db_conn, tid, "hello", source="user")
    _insert_inbound_kind(db_conn, tid, "", "restart", source="system:update")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    assert cmd.goto == END
    assert cmd.update["halted"] is False  # type: ignore[index]
    msgs = cmd.update["messages"]  # type: ignore[index]
    assert len(msgs) == 1 and "hello" in msgs[0].content  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]


async def test_claim_restart_batched_with_terminate_terminate_wins(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """restart then terminate in one batch — terminate is the NEWER intent, so it
    wins by recency: status does **not** flip to 'restarting' (else the restarter
    would respawn an agent the user just killed), goto END via the plain terminate
    exit, restart consumed and dropped. The reverse order (terminate older, restart
    newer) is covered by test_claim_terminate_then_restart_respawns."""
    tid = spawn_agent()
    _set_agent_status(db_conn, tid, "running")
    # terminate inserted last -> newer id -> wins the recency contest over restart
    _insert_inbound_kind(db_conn, tid, "", "restart", source="user")
    _insert_inbound_kind(db_conn, tid, "", "terminate", source="user")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    assert cmd.goto == END
    # terminate should not be overridden by restart's idle preserve — halted=False (end state is fine but don't silently lurk)
    assert cmd.update["halted"] is False  # type: ignore[index]
    msgs = cmd.update["messages"]  # type: ignore[index]
    assert len(msgs) == 1 and "You are terminated by user" in msgs[0].content  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    # status remains running — exit finalize follows normal terminate path, restarter doesn't touch
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        row = cur.fetchone()
    assert row is not None and row[0] == "running"


async def test_claim_terminate_before_restart_completed_still_exits(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """terminate arrives first in the agent downtime window (smaller id, ordered earlier in batch),
    boot batch [terminate, restart_completed] → goto must be END. Lock down 'boot marker must not
    override the stronger END back to wake' — the marker arm once unconditionally set
    next_goto=BEFORE_LLM; this ordering would consume the terminate but the agent wakes up alive
    (ordering-dependent bug)."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "terminate", source="user")
    _insert_inbound_kind(db_conn, tid, "", "restart_completed", source="user")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    assert cmd.goto == END
    # both markers are still committed as usual (terminate's exit + restart's completion leave traces)
    contents = [m.content for m in cmd.update["messages"]]  # type: ignore[index]
    assert any("You are terminated by user" in c for c in contents)
    assert any("You have been restarted by user" in c for c in contents)


@pytest.mark.flaky  # poll _await_status for claim_node status transition
async def test_claim_cancel_batched_with_restart_idle_respawn_silent(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """cancel + restart in the same batch (user clicks Stop then Restart) → restart wins over cancel:
    exits normally via respawn; the Cancelled event for cancel is still emitted (frontend clears
    turn-active), the pause intent is absorbed into 'silent idle after respawn' (halted=True preserved)."""
    tid = spawn_agent()
    _set_agent_status(db_conn, tid, "running")
    _insert_inbound_kind(db_conn, tid, "", "cancel", source="user")
    restart_id = _insert_inbound_kind(db_conn, tid, "", "restart", source="user")
    await _await_inbound_visible(aops_pool, restart_id)
    pub = MagicMock()

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        _make_runtime(
            ops_pool=aops_pool, inbound_listener=aredis_inbound_listener, event_publisher=pub
        ),
        _config(tid),
    )

    # restart exit, not the cancel pause branch
    assert cmd.goto == END
    # idle before restart (halted=True) + external → silent after respawn
    assert cmd.update["halted"] is True  # type: ignore[index]
    # Cancelled event still emitted
    assert any("cancelled" in str(c.args[0]).lower() for c in pub.emit.call_args_list)
    await _await_status(aops_pool, tid, "restarting")


@pytest.mark.flaky  # poll _await_status for claim_node status transition
async def test_claim_second_restart_batched_with_restart_completed_exits_again(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """boot batch [restart_completed, restart] (user clicked restart again within the respawn window) →
    the second restart wins over wake: after committing marker, exits again via respawn, idle preserved
    (external + halted=True) → second respawn remains silent."""
    tid = spawn_agent()
    _set_agent_status(db_conn, tid, "running")
    _insert_inbound_kind(db_conn, tid, "", "restart_completed", source="user")
    restart_id = _insert_inbound_kind(db_conn, tid, "", "restart", source="user")
    await _await_inbound_visible(aops_pool, restart_id)

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    assert cmd.goto == END
    assert cmd.update["halted"] is True  # type: ignore[index]
    msgs = cmd.update["messages"]  # type: ignore[index]
    assert len(msgs) == 1 and "You have been restarted by user" in msgs[0].content  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    await _await_status(aops_pool, tid, "restarting")


@pytest.mark.flaky  # poll _await_status for claim_node status transition
async def test_claim_compact_request_batched_with_restart_is_dropped(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """compact_request + restart in same batch → compact_request is the discarded loser:
    does **not** run the backend Compaction LLM (if it raised, the already consumed restart row
    would be lost before the RESTARTING marker), restart exits normally. Re-trigger /compact afterwards."""
    tid = spawn_agent()
    _set_agent_status(db_conn, tid, "running")
    _insert_inbound_kind(db_conn, tid, "", "compact_request", source="user")
    restart_id = _insert_inbound_kind(db_conn, tid, "", "restart", source="user")
    await _await_inbound_visible(aops_pool, restart_id)
    llm = _fake_llm()

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener, llm=llm),
        _config(tid),
    )

    assert cmd.goto == END
    # Compaction LLM not called — compact_request is discarded rather than run before exiting
    llm.bind_tools.return_value.ainvoke.assert_not_called()
    # No compact happened: does not go through REMOVE_ALL replacement path
    assert not any(isinstance(m, RemoveMessage) for m in cmd.update["messages"])  # type: ignore[index]
    # idle preserved unchanged — silent after respawn
    assert cmd.update["halted"] is True  # type: ignore[index]
    await _await_status(aops_pool, tid, "restarting")


@pytest.mark.flaky  # poll _await_status for claim_node status transition
async def test_claim_compact_summary_batched_with_restart_applies_and_keeps_idle(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """compact_summary + restart in same batch → summary is data the agent already wrote itself,
    applied as usual (discarding = silently swallowing the agent's work), while the restart's idle
    preservation is not overwritten by the compact return path's halted — remains silent after respawn."""
    tid = spawn_agent()
    # Confirm the spawn is visible on the async pool before we start writing
    # through it.  Every subsequent write goes through `aops_pool` too, so there
    # is no sync→async visibility window — the same pool is used for writes and
    # for the claim_node read.
    await _await_status(aops_pool, tid, "idling")
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
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    # The compaction detours through init_context to have the head rebuilt, and
    # carries END on as where the batch resumes — the restart's override survives.
    assert cmd.goto == "init_context"
    assert cmd.update["context_reset"].resume == END  # type: ignore[index]
    tail = _compact_tail(cmd.update)
    assert any(isinstance(m, HumanMessage) and "compacted summary" in m.content for m in tail)  # pyright: ignore[reportUnknownMemberType]
    # external restart + idle before restart → halted=True preserved (respawn silent)
    assert cmd.update["halted"] is True  # type: ignore[index]
    # Read status on the pool claim_node wrote through; _await_status dumps full
    # state on timeout (the CI-only `idling != restarting` flake).
    await _await_status(aops_pool, tid, "restarting")


async def test_claim_restart_completed_while_idle_stays_silent(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """halted=True (idle before restart, preserved from RESTART case) + batch has only
    restart_completed → after committing lifecycle marker, goto CLAIM returns to waiting,
    does **not** enter before_llm — idle agent does not burn an LLM call for 'knowing I was restarted'."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "restart_completed", source="system:update")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    assert cmd.goto == "claim"
    assert cmd.goto != "before_llm"
    assert cmd.update["halted"] is True  # type: ignore[index]
    # marker committed as usual — agent reads it the next time it truly wakes
    msgs = cmd.update["messages"]  # type: ignore[index]
    assert len(msgs) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert "You have been updated and restarted" in msgs[0].content  # pyright: ignore[reportUnknownMemberType]
    assert msgs[0].additional_kwargs.get("ava_note_tag") == "lifecycle_restart"  # pyright: ignore[reportUnknownMemberType]


async def test_claim_restart_completed_with_chat_cobatch_wakes(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """halted=True but batch has chat in addition to restart_completed (user message that arrived
    during agent downtime window) → wakes normally to before_llm, must not silently swallow the chat."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "restart_completed", source="system:update")
    insert_inbound_message(db_conn, tid, "are you back?", source="user")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    assert cmd.goto == "before_llm"
    assert cmd.update["halted"] is False  # type: ignore[index]
    msgs = cmd.update["messages"]  # type: ignore[index]
    assert len(msgs) == 2  # pyright: ignore[reportUnknownArgumentType]
    assert "updated and restarted" in msgs[0].content  # pyright: ignore[reportUnknownMemberType]
    assert "are you back?" in msgs[1].content  # pyright: ignore[reportUnknownMemberType]


async def test_claim_resurrect_kind_appends_marker_and_continues(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """resurrect inbound (delivered to the new process by resurrect_agent) → claim appends
    lifecycle marker 'You have been resurrected by {source}' + goto BEFORE_LLM."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "resurrect", source="user")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    assert cmd.goto == "before_llm"
    msgs = cmd.update["messages"]  # type: ignore[index]
    assert len(msgs) == 1  # pyright: ignore[reportUnknownArgumentType]
    lifecycle = msgs[0]
    assert isinstance(lifecycle, HumanMessage)
    assert "You have been resurrected by user" in lifecycle.content  # pyright: ignore[reportUnknownMemberType]
    assert lifecycle.additional_kwargs.get("ava_msg_type") == "system_note"  # pyright: ignore[reportUnknownMemberType]
    assert lifecycle.additional_kwargs.get("ava_note_tag") == "lifecycle_resurrect"  # pyright: ignore[reportUnknownMemberType]


async def test_claim_resurrect_batch_appends_only_latest_marker(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """Repeated failed recoveries are consumed together but render one marker."""
    tid = spawn_agent()
    first = _insert_inbound_kind(db_conn, tid, "", "resurrect", source="system:retry")
    latest = _insert_inbound_kind(db_conn, tid, "", "resurrect", source="user")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    msgs = cmd.update["messages"]  # type: ignore[index]
    assert len(msgs) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert "You have been resurrected by user" in msgs[0].content  # pyright: ignore[reportUnknownMemberType]
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT id, status FROM inbound_messages WHERE id = ANY(%s)", ([first, latest],)
        )
        assert dict(cur.fetchall()) == {first: "done", latest: "done"}


# ────────────────────────────────────────────────────────────────────────
# Lifecycle routing by recency: when a claim batch carries several conflicting
# intents (only happens after the agent was stuck / down long enough for them to
# pile up), the LATEST one by row id wins — not a fixed kind precedence. This is
# what stops a stale terminate from a prior life killing an agent a newer
# resurrect just brought back (production agent 716).


async def test_claim_stale_terminate_loses_to_newer_resurrect(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """The production stuck-agent recovery batch [cancel, cancel, terminate,
    resurrect]: the resurrect (newest id) brought the process up and is the user's
    latest intent → the agent wakes (goto before_llm), the stale prior-life
    terminate is the consumed loser (no terminate marker, no END). Without recency
    the terminate would kill the freshly-resurrected process on its first claim."""
    tid = spawn_agent()
    # id == insertion order: terminate older than the resurrect that follows it
    _insert_inbound_kind(db_conn, tid, "", "cancel", source="user")
    _insert_inbound_kind(db_conn, tid, "", "cancel", source="user")
    _insert_inbound_kind(db_conn, tid, "", "terminate", source="user")
    _insert_inbound_kind(db_conn, tid, "", "resurrect", source="user")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    assert cmd.goto == "before_llm"  # wakes, not END — the kill was vetoed
    assert cmd.update["halted"] is False  # type: ignore[index]
    contents = [m.content for m in cmd.update["messages"]]  # type: ignore[index]
    assert any("You have been resurrected by user" in c for c in contents)
    assert not any("You are terminated" in c for c in contents)  # stale terminate dropped


async def test_claim_resurrect_then_terminate_still_dies(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """Mirror case: the user resurrects an agent, then terminates the fresh
    process before its first claim. The terminate is the newer intent → the agent
    dies (goto END + terminate marker); the resurrect is the consumed loser (no
    'resurrected' marker). Fixed `resurrect > terminate` priority would wrongly
    keep alive an agent the user just killed; recency resolves it correctly."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "resurrect", source="user")
    _insert_inbound_kind(db_conn, tid, "", "terminate", source="user")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    assert cmd.goto == END
    contents = [m.content for m in cmd.update["messages"]]  # type: ignore[index]
    assert any("You are terminated by user" in c for c in contents)
    assert not any("resurrected" in c for c in contents)  # superseded revive dropped


@pytest.mark.flaky  # poll _await_status for claim_node status transition
async def test_claim_terminate_then_restart_respawns(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """Reverse of test_claim_restart_batched_with_terminate_terminate_wins:
    terminate older, restart newer → restart wins (the user's latest intent is to
    recycle). status flips to 'restarting' (the restarter respawns); the stale
    terminate is the consumed loser (no marker). Recency resolves both directions
    of the terminate/restart conflict, not a fixed terminate > restart."""
    tid = spawn_agent()
    _set_agent_status(db_conn, tid, "running")
    # restart inserted last -> newer id -> wins over the older terminate
    _insert_inbound_kind(db_conn, tid, "", "terminate", source="user")
    restart_id = _insert_inbound_kind(db_conn, tid, "", "restart", source="user")
    await _await_inbound_visible(aops_pool, restart_id)

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    assert cmd.goto == END
    # terminate lost -> no terminate marker; restart writes no marker either
    assert cmd.update["messages"] == []  # type: ignore[index]
    # Read status on the pool claim_node wrote through; _await_status dumps full
    # state on timeout (the CI-only `idling != restarting` flake).
    await _await_status(aops_pool, tid, "restarting")


async def test_claim_auto_resurrect_chat_batch_wakes_and_keeps_chat(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """Auto-resurrect-on-chat path: a chat delivered to a terminated agent inserts
    the chat then a resurrect, so a stale prior-life terminate is co-batched as
    [terminate, chat, resurrect]. The resurrect (newest) wins → the agent wakes and
    the chat survives to be answered; the stale terminate is vetoed. Without recency
    the auto-resurrected agent would die before reading the message."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "terminate", source="user")
    insert_inbound_message(db_conn, tid, "are you there?", source="user")
    _insert_inbound_kind(db_conn, tid, "", "resurrect", source="user")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    assert cmd.goto == "before_llm"
    assert cmd.update["halted"] is False  # type: ignore[index]
    contents = [m.content for m in cmd.update["messages"]]  # type: ignore[index]
    assert any("You have been resurrected by user" in c for c in contents)
    assert any("are you there?" in c for c in contents)  # chat not swallowed
    assert not any("You are terminated" in c for c in contents)


async def test_claim_auto_resurrect_compact_request_batch_compacts_and_wakes(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """Auto-resurrect-on-compact path: a /compact delivered to a terminated agent
    inserts the compact_request then a resurrect (newer id). The resurrect wins the
    recency routing so the agent wakes; the compact_request is NOT an exit loser
    (exit_kind is None), so it still runs — the history is compacted and the
    resurrect marker is appended after the summary. Without auto-resurrect the
    compact_request would sit pending with no live process to claim it."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "compact_request", source="user")
    _insert_inbound_kind(db_conn, tid, "", "resurrect", source="user")

    state = AgentState(
        messages=[
            SystemMessage(content="<test sys prompt>"),
            *(HumanMessage(content=f"history-{i}") for i in range(10)),
        ]
    )

    fake_llm = _fake_llm("LLM-generated summary")
    cmd = await claim_node(
        state,
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener, llm=fake_llm),
        _config(tid),
    )

    assert cmd.goto == "init_context"
    assert cmd.update["context_reset"].resume == "before_llm"  # type: ignore[index]
    fake_llm.bind_tools.return_value.ainvoke.assert_called_once()  # compact ran
    msgs = _compact_tail(cmd.update)
    assert msgs[0].content == compose_summary_message("LLM-generated summary")  # pyright: ignore[reportUnknownMemberType]
    # resurrect marker appended after the summary — the revive still wakes the agent
    contents = [m.content for m in msgs if isinstance(m.content, str)]  # pyright: ignore[reportUnknownMemberType]
    assert any("You have been resurrected by user" in c for c in contents)


async def test_claim_fork_kind_appends_identity_marker_and_continues(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """fork inbound (delivered to the new process by spawn_agent on fork) → claim appends
    identity marker: contains the fork source from source (agent:M) + new agent's own id (N) +
    'inherited' wording + goto BEFORE_LLM. Simulates forked checkpoint: messages non-empty
    (inherited history from source agent), so no SystemMessage injection; marker appended at
    the end, then the `on_fork` notes (fork_notes stubbed here — its membership is pinned in
    test_fork_notes.py, issue #1320)."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "fork", source="agent:7")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        "agent.graph._claim_dispatch.fork_notes",
        lambda: [system_note_message(content="Your Agent ID is N.", tag=NoteTag.AGENT_ID)],
    )
    try:
        cmd = await claim_node(
            AgentState(messages=[SystemMessage(content="sys"), HumanMessage(content="inherited")]),
            _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
            _config(tid),
        )
    finally:
        monkeypatch.undo()

    assert cmd.goto == "before_llm"
    msgs = cmd.update["messages"]  # type: ignore[index]
    # messages non-empty → no SystemMessage injection; the fork rebuilds the
    # head (full wipe + inherited history re-listed) and appends marker + notes.
    # Shape: [RemoveMessage(__remove_all__), sys, inherited, marker, agent_id].
    assert len(msgs) == 5  # pyright: ignore[reportUnknownArgumentType]
    assert isinstance(msgs[0], RemoveMessage) and msgs[0].id == REMOVE_ALL_MESSAGES  # pyright: ignore[reportUnknownMemberType]
    assert [m.additional_kwargs.get("ava_note_tag") for m in msgs[-2:]] == [  # pyright: ignore[reportUnknownMemberType]
        "lifecycle_fork",
        "agent_id",
    ]
    lifecycle = msgs[3]
    assert isinstance(lifecycle, HumanMessage)
    assert isinstance(lifecycle.content, str)  # pyright: ignore[reportUnknownMemberType]
    content = lifecycle.content
    # fork source (M=7, from source) + new agent's own id (N=tid, from config)
    assert "forked from agent:7" in content
    assert f"id {tid}" in content
    assert "inherited from agent:7" in content
    assert lifecycle.additional_kwargs.get("ava_msg_type") == "system_note"  # pyright: ignore[reportUnknownMemberType]
    assert lifecycle.additional_kwargs.get("ava_note_tag") == "lifecycle_fork"  # pyright: ignore[reportUnknownMemberType]


async def test_claim_fork_strips_inherited_source_notes(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """The fork strip (issue #1320): inherited head notes that name the SOURCE —
    its agent id, its per-agent memory, its preloaded skills — are removed, and
    the new agent's own copies are grafted. The cluster memory index is
    cluster-wide: the inherited copy is kept and NOT re-grafted."""
    from langchain_core.messages import RemoveMessage

    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "fork", source="agent:7")

    def _tagged(tag: NoteTag, content: str, id: str) -> HumanMessage:  # pyright: ignore[reportShadowedBuiltins]
        return HumanMessage(
            content=f"[system] {content}",
            id=id,
            additional_kwargs={"ava_msg_type": "system_note", "ava_note_tag": tag.value},
        )

    old_id = _tagged(NoteTag.AGENT_ID, "old agent id", "note-old-id")
    old_mem = _tagged(NoteTag.AGENT_MEMORY, "source's memory", "note-old-mem")
    old_preload = _tagged(NoteTag.PRELOADED_SKILLS, "source's preloaded skills", "note-old-preload")
    cluster_index = _tagged(NoteTag.MEMORY, "shared pool index", "note-cluster-index")
    inherited = [SystemMessage(content="sys"), old_id, old_mem, old_preload, cluster_index]
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        "agent.graph._claim_dispatch.fork_notes",
        lambda: [
            system_note_message(content="Your Agent ID is N.", tag=NoteTag.AGENT_ID),
            system_note_message(content="the new agent's memory", tag=NoteTag.AGENT_MEMORY),
        ],
    )
    try:
        cmd = await claim_node(
            AgentState(messages=list(inherited)),
            _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
            _config(tid),
        )
    finally:
        monkeypatch.undo()

    assert cmd.goto == "before_llm"
    msgs = cast(list[BaseMessage], (cmd.update or {})["messages"])
    # The rebuild: one full-wipe marker, then the inherited history re-listed
    # with the three source-identity notes dropped (the cluster index — the
    # SYSTEM_NOTE not owned by the source — survives the rebuild).
    assert isinstance(msgs[0], RemoveMessage) and msgs[0].id == REMOVE_ALL_MESSAGES  # pyright: ignore[reportUnknownMemberType]
    rebuilt_ids = {m.id for m in msgs[1:] if not isinstance(m, RemoveMessage)}
    assert {old_id.id, old_mem.id, old_preload.id} & rebuilt_ids == set()
    assert cluster_index.id in rebuilt_ids
    # Grafted after the rebuild: fork marker + the new agent's own id + its
    # own per-agent memory.
    tail = [m for m in msgs if not isinstance(m, RemoveMessage)][-3:]
    assert [m.additional_kwargs.get("ava_note_tag") for m in tail] == [  # pyright: ignore[reportUnknownMemberType]
        "lifecycle_fork",
        "agent_id",
        "agent_memory",
    ]
    grafted_content: object = tail[-1].content  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    assert isinstance(grafted_content, str) and "source's memory" not in grafted_content


async def test_claim_multiple_chat_inbounds_all_appended_in_fifo_order(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """multiple chat inbounds in same batch → all appended in FIFO order by created_at (none lost)."""
    tid = spawn_agent()
    insert_inbound_message(db_conn, tid, "first", source="user")
    insert_inbound_message(db_conn, tid, "second", source="agent:5")
    insert_inbound_message(db_conn, tid, "third", source="user")

    cmd = await claim_node(
        AgentState(),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    msgs = cmd.update["messages"]  # type: ignore[index]
    # Claim appends the batch alone — the head is `init_context`'s.
    assert len(msgs) == 3  # pyright: ignore[reportUnknownArgumentType]
    contents = [m.content for m in msgs]  # pyright: ignore[reportUnknownMemberType]
    assert "first" in contents[0]
    assert "second" in contents[1]
    assert "third" in contents[2]


# auto-compact behavior is now implemented by the before_llm hook in agent/hooks/compact.py,
# tests in tests/agent/test_compact.py's hook tests. This file only tests claim node
# itself (inbound dispatch + state replacement), no longer tests auto-compact.


async def test_claim_chat_marks_inbound_claimed_immediately(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """chat inbound uses two-phase commit (since 2026-05-27): claim UPDATE pending → claimed;
    a subsequent startup reconcile will move claimed → done; if the process dies midway,
    claimed rows will be reset back to pending by the new process for re-delivery."""
    tid = spawn_agent()
    iid = insert_inbound_message(db_conn, tid, "msg", source="user")

    await claim_node(
        AgentState(),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM inbound_messages WHERE id = %s", (iid,))
        status = cur.fetchone()[0]  # type: ignore[index]
    assert status == "claimed"


async def test_claim_lifecycle_marks_inbound_done_immediately(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """Non-chat kinds (lifecycle / compact) keep the legacy one-step pending→done
    semantics so the new two-phase reconcile doesn't loop them.

    Critical: if a lifecycle inbound (terminate/restart/restart_completed/resurrect)
    or compact (compact_summary/compact_request) ended up `claimed`, the next
    startup's reconcile would see no `ava_inbound_id` in state.messages for it
    and reset it to `pending` — that loops resurrect→terminate→resurrect, and
    re-delivers compact summaries on every boot. See codex review of PR #539."""
    tid = spawn_agent()
    iid = _insert_inbound_kind(db_conn, tid, "terminate me", "terminate", source="user")

    await claim_node(
        AgentState(),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM inbound_messages WHERE id = %s", (iid,))
        status = cur.fetchone()[0]  # type: ignore[index]
    assert status == "done", (
        f"non-chat kind 'terminate' must skip two-phase claim (codex PR #539 review); "
        f"got status={status!r}"
    )


@pytest.mark.flaky  # parks in real wait_for_inbound; woken by a real Redis pub/sub publish
async def test_claim_marks_idling_during_wait_and_running_after_batch(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """first SELECT empty → claim_node changes agents.status 'running' → 'idling',
    parks in wait_for_inbound; external INSERT wakes → takes batch → 'idling' → 'running'.
    """
    tid = spawn_agent()
    # simulate process startup: unclaimed 'idling' → 'running', so mark_agent_status idling's
    # expected_from='running' hits
    _set_agent_status(db_conn, tid, "running")
    # Per-agent wake listener: claim parks in wait_for_inbound on this agent's
    # Redis channel, so the listener must subscribe to tid's channel — the shared
    # agent_id=0 fixture would never receive tid's publish.
    listener = RedisInboundListener(settings.data_plane.redis_url, tid)

    async def _delayed_insert() -> None:
        # poll until claim_node completes first SELECT + mark idling + enters wait — a fixed
        # sleep on overloaded runners would read 'running' before mark idling and flake;
        # polling semantics unchanged (still proves status reached idling during wait), just removes the race.
        deadline = time.monotonic() + 5.0
        while True:
            with db_conn.cursor() as cur:
                cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
                row = cur.fetchone()
            assert row is not None
            if row[0] == "idling":
                break
            assert time.monotonic() < deadline, (
                f"claim_node did not switch status to idling within 5s (currently {row[0]!r})"
            )
            await asyncio.sleep(0.05)
        # insert inbound to wake
        insert_inbound_message(db_conn, tid, "wake up", source="user")

    claim_task = asyncio.create_task(
        claim_node(
            AgentState(),
            _make_runtime(ops_pool=aops_pool, inbound_listener=listener),
            _config(tid),
        )
    )
    insert_task = asyncio.create_task(_delayed_insert())

    try:
        cmd, _ = await asyncio.gather(claim_task, insert_task)
    finally:
        await listener.close()

    # took batch + status switched back to running; claim appends the chat alone
    assert isinstance(cmd, Command)
    msgs = cmd.update["messages"]  # type: ignore[index]
    assert len(msgs) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert "wake up" in msgs[0].content  # pyright: ignore[reportUnknownMemberType]

    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        row = cur.fetchone()
    assert row is not None and row[0] == "running"


async def test_claim_short_path_does_not_enter_idling(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
    running_agent,
) -> None:
    """first SELECT already has inbound → does not enter wait branch, status not switched to idling.

    Anti-regression: if someone changes to 'unconditionally mark idling then mark running',
    it would wrongly touch the caller's expected state machine; this test verifies that the
    non-wait path does NOT enter IDLING — by starting status 'running' and still 'running'
    after completion (not idling).
    """
    tid = running_agent()
    insert_inbound_message(db_conn, tid, "preexisting", source="user")  # pyright: ignore[reportUnknownArgumentType]

    cmd = await claim_node(
        AgentState(),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),  # pyright: ignore[reportUnknownArgumentType]
    )

    assert isinstance(cmd, Command)
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))  # pyright: ignore[reportUnknownArgumentType]
        row = cur.fetchone()
    # short path: did not enter _wait_for_batch, no mark idling/running switch, status remains 'running'
    assert row is not None and row[0] == "running"


async def test_claim_chat_publishes_inbound_committed_per_id(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
) -> None:
    """After each chat inbound is envelope-wrapped into state, publish one InboundCommitted
    (frontend relies on this event to trigger reload to fetch the committed version).

    Anchor the protocol layer ACK contract: changing publish order / missing a publish /
    wrong inbound_id any of these → test fails.
    """
    import json

    tid = spawn_agent()
    id1 = insert_inbound_message(db_conn, tid, "first", source="user")
    id2 = insert_inbound_message(db_conn, tid, "second", source="user")

    pub = MagicMock()
    await claim_node(
        AgentState(),
        _make_runtime(
            ops_pool=aops_pool, inbound_listener=aredis_inbound_listener, event_publisher=pub
        ),
        _config(tid),
    )

    # two chats → two InboundCommitted emits, order by id ascending (claim node internal
    # created_at FIFO). node_lifecycle also emits timeline_snapshot; filter out and only look at
    # inbound_committed payloads.
    payloads = [json.loads(c.args[0]) for c in pub.emit.call_args_list]
    committed = [p for p in payloads if p["role"] == "inbound_committed"]
    assert len(committed) == 2
    assert all(p["agent_id"] == tid for p in committed)
    assert {p["inbound_id"] for p in committed} == {id1, id2}


@pytest.mark.parametrize("kind", ["terminate", "restart_completed", "resurrect"])
async def test_claim_lifecycle_kind_does_not_publish_committed(
    kind: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
) -> None:
    """lifecycle kind (terminate / restart_completed / resurrect) does **not** publish
    InboundCommitted — they are lifecycle markers not user conversations; frontend does not
    depend on reload trigger (timeline renders lifecycle HumanMessage via system_marker, not
    part of the inbound_chat anchor sequence).

    The restart test below (claim does not append message nor publish)."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", kind, source="user")

    pub = MagicMock()
    await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(
            ops_pool=aops_pool, inbound_listener=aredis_inbound_listener, event_publisher=pub
        ),
        _config(tid),
    )
    assert _committed_publishes(pub) == []


async def test_claim_restart_kind_does_not_publish_committed(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """restart kind does not publish InboundCommitted (appends no message and no chat
    inbound id enters committed list)."""
    tid = spawn_agent()
    _set_agent_status(db_conn, tid, "running")
    _insert_inbound_kind(db_conn, tid, "", "restart", source="user")

    pub = MagicMock()
    await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(
            ops_pool=aops_pool, inbound_listener=aredis_inbound_listener, event_publisher=pub
        ),
        _config(tid),
    )
    assert _committed_publishes(pub) == []


async def test_claim_compact_summary_alone_does_not_publish_committed(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
) -> None:
    """compact_summary alone → does not publish InboundCommitted (it goes through state replace
    not inbound append; frontend reload should be triggered by llm_done)."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "summary", "compact_summary")

    pub = MagicMock()
    state = AgentState(messages=[SystemMessage(content="sys")])
    await claim_node(
        state,
        _make_runtime(
            ops_pool=aops_pool, inbound_listener=aredis_inbound_listener, event_publisher=pub
        ),
        _config(tid),
    )
    assert _committed_publishes(pub) == []


async def test_claim_mixed_batch_publishes_only_chat_ids(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
) -> None:
    """same batch chat + compact_summary → publish only for the chat's inbound_id,
    summary does not emit publish."""
    tid = spawn_agent()
    chat_id = insert_inbound_message(db_conn, tid, "user msg", source="user")
    _insert_inbound_kind(db_conn, tid, "summary", "compact_summary")

    pub = MagicMock()
    state = AgentState(messages=[SystemMessage(content="sys")])
    await claim_node(
        state,
        _make_runtime(
            ops_pool=aops_pool, inbound_listener=aredis_inbound_listener, event_publisher=pub
        ),
        _config(tid),
    )

    committed = _committed_publishes(pub)
    assert len(committed) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert committed[0]["inbound_id"] == chat_id


# ────────────────────────────────────────────────────────────────────────
# mutmut gap-fix follow-up tests (PR #296 → this PR)
# ────────────────────────────────────────────────────────────────────────
# Lock down actionable survived mutation cluster from baseline:
# - `_by_who`: 'self' literal case-sensitivity + return text
# - `_wait_for_batch`: empty batch retry loop + try/finally state machine rollback
# - `_claim_node_impl`: dispatch boundary conditions, container mode, multi-step continue
# - `claim_node`: outer wrapper does not swallow return + msg_count not skewed
# Target ~15-20 actionable mutations killed; _render_restart_completed_marker's
# ~70 string noise not covered.


# ───────────── _by_who unit tests (case-sensitive dispatch) ─────────────


def test_by_who_self_returns_yourself():
    """source 'self' (ava.self.terminate/restart self-invocation) → 'yourself'.
    Lock down that the literal 'self' cannot be mutated to 'SELF' / 'XXselfXX' / '' etc. by mutmut."""
    from agent.graph._claim import _by_who

    assert _by_who("self") == "yourself"


def test_by_who_uppercase_self_passthrough():
    """source is case-sensitive — 'SELF' does not match the 'self' branch, falls back to
    return original value. `_by_who` does not do case-folding (to avoid mistakenly treating
    'Self' / 'SELF' as self-trigger)."""
    from agent.graph._claim import _by_who

    assert _by_who("SELF") == "SELF"
    assert _by_who("Self") == "Self"
    assert _by_who("SELF:UPDATE") == "SELF:UPDATE"


def test_by_who_external_source_passthrough():
    """Non self source ('user' / 'agent:42' / 'system')
    returns as-is — the marker text shows who triggered it at a glance."""
    from agent.graph._claim import _by_who

    assert _by_who("user") == "user"
    assert _by_who("user") == "user"
    assert _by_who("agent:42") == "agent:42"
    assert _by_who("system") == "system"


def test_by_who_self_prefix_does_not_match_self():
    """'self_xxx' / 'selfish' should not be recognized as 'self' (literal == comparison,
    not startswith)."""
    from agent.graph._claim import _by_who

    # anti-regression: changing to startswith("self") would make this test fail
    assert _by_who("selfish") == "selfish"
    assert _by_who("self_other") == "self_other"
    assert _by_who("self:other") == "self:other"  # only self is specifically handled


# ───────────── _render_restart_completed_marker wording ───────────────


def test_render_restart_completed_marker_system_update_no_by_clause():
    """source='system:update' → 'updated and restarted' with no trailing 'by ...' noise."""
    from agent.graph._claim import _render_restart_completed_marker

    text = _render_restart_completed_marker("system:update")
    assert "updated and restarted" in text
    assert "by " not in text  # no actor suffix for system-driven rollout


def test_render_restart_completed_marker_plain_self_unchanged():
    """source='self' (ordinary restart, not update) → 'restarted by yourself', no 'updated'."""
    from agent.graph._claim import _render_restart_completed_marker

    text = _render_restart_completed_marker("self")
    assert "restarted by yourself" in text
    assert "updated" not in text


def test_render_restart_completed_marker_external_source_unchanged():
    """Non-update sources → plain 'restarted by <source>' wording."""
    from agent.graph._claim import _render_restart_completed_marker

    text = _render_restart_completed_marker("user")
    assert "restarted by user" in text
    assert "updated" not in text


# ───────────── _wait_for_batch state machine + retry loop ─────────────


@pytest.mark.flaky  # parks in real _wait_for_batch loop; two real Redis pub/sub wakes
async def test_claim_idles_on_a_freshly_established_window(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A window holding only the standing head, no pending inbound, halted=False
    must block for inbound — not read as a multi-step loop in progress.

    Regression: claim's idle test used to be `not state.messages`, which was
    equivalent while claim itself built the head (state was still empty when the
    test ran). Once `init_context` began committing the head before claim, a
    just-started agent arrived here with a non-empty window and went straight to
    the LLM, spending a turn on an empty conversation.
    """
    tid = spawn_agent()  # no inbound inserted

    waited = False

    async def _fake_wait(*_args, **_kwargs):
        nonlocal waited
        waited = True
        return []

    monkeypatch.setattr("agent.graph._claim._wait_for_batch", _fake_wait)  # pyright: ignore[reportUnknownArgumentType]

    head: list[AnyMessage] = [
        SystemMessage(content="<prompt>"),
        system_note_message(content="your id", tag=NoteTag.AGENT_ID),
        system_note_message(content="your memory", tag=NoteTag.AGENT_MEMORY),
    ]
    await claim_node(
        AgentState(messages=head, halted=False),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    # The short-circuit returns straight to before_llm *without* waiting, so
    # having reached the wait at all is the discriminator. (The stub returns an
    # empty batch, after which claim continues normally — not what is under test.)
    assert waited, "a fresh window must block for inbound, not continue to the LLM"


async def test_wait_for_batch_retries_on_empty_claim(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
):
    """`_wait_for_batch`'s `while not batch:` loop — wait_for_inbound wakes but
    claim_inbound_batch returns empty (race: another worker / kind filter already consumed),
    should wait + claim again until non-empty batch obtained.

    Lock down mutation `while not batch:` → `while batch:` (would immediately exit with
    empty batch, claim_node dispatches 0 rows with empty batch — resembles multi-step continue
    path but status already switched to idling, causing state machine misalignment)."""
    tid = spawn_agent()
    _set_agent_status(db_conn, tid, "running")
    # Per-agent wake listener (see test_claim_marks_idling_during_wait_...): the
    # claim parks on tid's Redis channel, so subscribe there, not agent_id=0.
    listener = RedisInboundListener(settings.data_plane.redis_url, tid)

    from agent.graph import _claim as claim_module

    call_count = {"n": 0}
    real_claim = claim_module.claim_inbound_batch

    async def flaky_claim(db, agent_id):
        # call 1 (claim_node top first SELECT): returns empty → enters wait
        # call 2 (_wait_for_batch loop iter 1): after waking claim still empty → wait again
        # call 3 (loop iter 2): obtains batch
        call_count["n"] += 1
        if call_count["n"] <= 2:
            return []
        return await real_claim(db, agent_id)  # pyright: ignore[reportUnknownArgumentType]

    # Task #1006 split: the first SELECT lives in _claim (claim_node_impl), the
    # wait-loop claims in _claim_batch (_wait_for_batch) — patch both.
    monkeypatch.setattr("agent.graph._claim.claim_inbound_batch", flaky_claim)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("agent.graph._claim_batch.claim_inbound_batch", flaky_claim)  # pyright: ignore[reportUnknownArgumentType]

    async def _delayed_inserts():
        # The fixed sleeps are load-bearing for correctness, not just pacing: the
        # first sleep lets claim_node's wait_for_inbound subscribe to the Redis
        # channel before the INSERT publishes, so the wake isn't lost racing ahead
        # of the subscription (the empty first SELECT returns before the subscribe
        # completes, and a publish landing then is dropped). This test stays serial
        # (flaky) precisely because it depends on that real subscribe/publish timing.
        # first wake → claim returns empty → wait again
        await asyncio.sleep(0.15)
        insert_inbound_message(db_conn, tid, "first wake but no batch", source="user")
        # give the loop time to run claim_inbound_batch = []
        await asyncio.sleep(0.15)
        # second wake → claim gets batch
        insert_inbound_message(db_conn, tid, "second wake delivers", source="user")

    claim_task = asyncio.create_task(
        claim_node(
            AgentState(),
            _make_runtime(ops_pool=aops_pool, inbound_listener=listener),
            _config(tid),
        )
    )
    insert_task = asyncio.create_task(_delayed_inserts())
    try:
        cmd, _ = await asyncio.gather(claim_task, insert_task)
    finally:
        await listener.close()

    # verify: entered wait loop at least 2+ times (1 first SELECT + 2 wait loop)
    assert call_count["n"] >= 3
    assert isinstance(cmd, Command)
    msgs = cmd.update["messages"]  # type: ignore[index]
    # Was the first inbound done when fake_claim returned empty on call 2?
    # No: flaky_claim returns [] without touching DB, so both inbounds are still pending
    # call 3 real claim → takes both
    contents = [m.content for m in msgs if hasattr(m, "content")]  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert any("first wake" in str(c) or "second wake" in str(c) for c in contents)  # pyright: ignore[reportUnknownArgumentType]


async def test_wait_for_batch_resets_status_to_running_on_exception(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
    monkeypatch: pytest.MonkeyPatch,
):
    """`_wait_for_batch` uses try/finally to guarantee status returns to RUNNING — even if
    wait_for_inbound raises, finally triggers mark_agent_status RUNNING.

    Lock down mutation that removes try/finally / deletes finally body — that mutation would
    leave agent status permanently stuck at 'idling' on wait exception, restarter won't recognize it."""
    tid = spawn_agent()
    _set_agent_status(db_conn, tid, "running")

    boom = RuntimeError("simulated wait failure")

    async def exploding_wait(pool, listener, *, agent_id):
        raise boom

    monkeypatch.setattr("agent.graph._claim_batch.wait_for_inbound", exploding_wait)  # pyright: ignore[reportUnknownArgumentType]

    with pytest.raises(RuntimeError, match="simulated wait failure"):
        await claim_node(
            AgentState(),
            _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
            _config(tid),
        )

    # finally ran → status back to 'running' (not stuck at 'idling')
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "running", f"finally didn't run / didn't mark RUNNING: status={row[0]!r}"


async def test_wait_for_batch_records_wait_inside_claim_idle_wait_span(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_wait_for_batch` runs its wait loop inside `claim_idle_wait_span()`
    (Task #1970): the end-the-node-span helper must wrap the while loop — the
    idle park is what would otherwise be drawn as a giant `execute_task claim`
    span."""
    from agent.db import ClaimedInbound
    from agent.graph._claim_batch import _wait_for_batch

    tid = spawn_agent()
    _set_agent_status(db_conn, tid, "running")
    order: list[str] = []

    @contextlib.contextmanager
    def _fake_span():
        order.append("span-enter")
        yield
        order.append("span-exit")

    async def _fake_wait(pool, listener, *, agent_id):
        order.append("wait")

    async def _fake_claim(pool, agent_id):
        return [ClaimedInbound(id=1, agent_id=tid, content="x", kind="chat", source="user")]

    monkeypatch.setattr("agent.graph._claim_batch.claim_idle_wait_span", _fake_span)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("agent.graph._claim_batch.wait_for_inbound", _fake_wait)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("agent.graph._claim_batch.claim_inbound_batch", _fake_claim)  # pyright: ignore[reportUnknownArgumentType]

    batch: list[ClaimedInbound] = await _wait_for_batch(
        _make_runtime(ops_pool=aops_pool, inbound_listener=MagicMock()).context, tid
    )

    # the wait round happened INSIDE the span helper's enter/exit pair
    assert order == ["span-enter", "wait", "span-exit"]
    assert len(batch) == 1
    assert batch[0].agent_id == tid


# ───────────── _claim_node_impl: container mode (ops_db None) ─────────────


async def test_container_mode_continues_without_touching_messages():
    """ops_pool=None → container mode skips all inbound dispatch and heads to
    before_llm without writing messages. The system prompt an eval starts from is
    laid down by `init_context`, which runs before claim (see
    tests/agent/test_init_context.py)."""
    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="<sys>")]),
        _make_runtime(ops_pool=None),
        _config(1),
    )
    assert isinstance(cmd, Command)
    assert cmd.goto == "before_llm"
    assert cmd.update == {"halted": False}


async def test_container_mode_halted_routes_to_end():
    """ops_pool=None + state.halted=True → goto END (after eval finishes one round exec exit 42/43 →
    halted=True means this ainvoke should end, no dispatch, no inject)."""
    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        _make_runtime(ops_pool=None),
        _config(1),
    )
    assert isinstance(cmd, Command)
    assert cmd.goto == END
    # should not have update.messages (END route writes no message)
    assert cmd.update is None or "messages" not in (cmd.update or {})  # type: ignore[operator]


async def test_container_mode_continue_clears_halted():
    """ops_pool=None + state.messages non-empty + halted=False → goto before_llm,
    update={'halted': False} (clear halted state to enter next LLM round)."""
    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys"), HumanMessage(content="hi")]),
        _make_runtime(ops_pool=None),
        _config(1),
    )
    assert isinstance(cmd, Command)
    assert cmd.goto == "before_llm"
    assert cmd.update["halted"] is False  # type: ignore[index]


# ───────────── _claim_node_impl: multi-step continue (no batch, not halted) ─────────────


async def test_claim_multi_step_continue_no_inbound(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
    running_agent,
):
    """state.halted=False + state.messages non-empty + no pending inbound →
    `_claim_node_impl` does not enter _wait_for_batch, immediately goto before_llm to let LLM
    continue multi-step.

    Lock down dispatch's `if state.halted or not state.messages` boolean short-circuit logic
    (mutation changing `or` to `and` / `not state.messages` to `state.messages` would make
    multi-step stuck waiting, conversely single-step would never wait)."""
    tid = running_agent()
    # no INSERT inbound → first SELECT returns empty
    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys"), HumanMessage(content="hi")]),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),  # pyright: ignore[reportUnknownArgumentType]
    )
    assert isinstance(cmd, Command)
    assert cmd.goto == "before_llm"
    assert cmd.update["halted"] is False  # type: ignore[index]
    # key: messages should not be injected with anything (multi-step continue does not touch messages)
    assert cmd.update.get("messages") in ([], None) or "messages" not in (cmd.update or {})  # type: ignore[union-attr]
    # status should remain running (did not enter _wait_for_batch to switch to idling)
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))  # pyright: ignore[reportUnknownArgumentType]
        row = cur.fetchone()
    assert row is not None and row[0] == "running"


@pytest.mark.flaky  # parks in real wait_for_inbound; woken by a real Redis pub/sub publish
async def test_claim_halted_with_messages_waits_for_inbound(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
):
    """state.halted=True + state.messages non-empty + no pending inbound →
    should enter _wait_for_batch (halted cannot multi-step continue, must wait for new inbound).

    Lock down `if state.halted or not state.messages` halted short-circuit —
    mutation changing `state.halted` to `not state.halted`: halted=True but goes
    multi-step continue (wrong), would make agent immediately call LLM again after exec exit 42
    wasting tokens."""
    tid = spawn_agent()
    _set_agent_status(db_conn, tid, "running")
    # Per-agent wake listener (see test_claim_marks_idling_during_wait_...): the
    # claim parks on tid's Redis channel, so subscribe there, not agent_id=0.
    listener = RedisInboundListener(settings.data_plane.redis_url, tid)

    async def _delayed_insert():
        # poll until claim_node enters wait + mark idling — a fixed sleep on overloaded
        # runners would read 'running' before mark idling and flake (same fix as
        # test_claim_marks_idling_during_wait_and_running_after_batch);
        # polling semantics unchanged: still proves halted=True took the wait branch
        # (status reached idling).
        deadline = time.monotonic() + 5.0
        while True:
            with db_conn.cursor() as cur:
                cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
                row = cur.fetchone()
            assert row is not None
            if row[0] == "idling":
                break
            assert time.monotonic() < deadline, (
                f"halted=True should enter wait + mark idling, after 5s status={row[0]!r}"
            )
            await asyncio.sleep(0.05)
        insert_inbound_message(db_conn, tid, "wake", source="user")

    claim_task = asyncio.create_task(
        claim_node(
            AgentState(
                messages=[SystemMessage(content="sys"), HumanMessage(content="hi")],
                halted=True,
            ),
            _make_runtime(ops_pool=aops_pool, inbound_listener=listener),
            _config(tid),
        )
    )
    insert_task = asyncio.create_task(_delayed_insert())
    try:
        cmd, _ = await asyncio.gather(claim_task, insert_task)
    finally:
        await listener.close()
    assert isinstance(cmd, Command)


# ───────────── _claim_node_impl: dispatch details ─────────────


async def test_claim_terminate_external_source_renders_source_verbatim(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """source='agent:42' (another agent triggering terminate) → marker uses 'by agent:42'
    as-is, does not go through _by_who's self special case."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "terminate", source="agent:42")
    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )
    assert cmd.goto == END
    msgs = cmd.update["messages"]  # type: ignore[index]
    assert isinstance(msgs[0].content, str)  # pyright: ignore[reportUnknownMemberType]
    assert "You are terminated by agent:42" in msgs[0].content
    # anti-regression: must not contain 'yourself' (mutation that changed != to == made all sources go through self)
    assert "yourself" not in msgs[0].content


async def test_claim_compact_summary_with_no_existing_system_message(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
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
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    tail = _compact_tail(cmd.update)
    assert not any(isinstance(m, SystemMessage) for m in cmd.update["messages"])  # type: ignore[index]
    assert not any(isinstance(m, SystemMessage) for m in tail)
    assert [m.content for m in tail] == [compose_summary_message(summary_text)]  # pyright: ignore[reportUnknownMemberType]


async def test_claim_chat_only_publishes_chat_id_not_lifecycle_in_mixed_batch(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """same batch chat + resurrect → publish only for the chat's inbound_id, lifecycle
    inbound id does not enter committed_chat_ids.

    Lock down `committed_chat_ids.append(item.id)` appearing only in CHAT branch —
    mutation moving it to dispatch top / resurrect branch would cause publish for extra
    lifecycle id, frontend fetching timeline would not find reload anchor."""
    tid = spawn_agent()
    chat_id = insert_inbound_message(db_conn, tid, "user msg", source="user")
    _insert_inbound_kind(db_conn, tid, "", "resurrect", source="user")

    pub = MagicMock()
    state = AgentState(messages=[SystemMessage(content="sys")])
    await claim_node(
        state,
        _make_runtime(
            ops_pool=aops_pool, inbound_listener=aredis_inbound_listener, event_publisher=pub
        ),
        _config(tid),
    )

    committed = _committed_publishes(pub)
    assert len(committed) == 1, f"should emit only 1 InboundCommitted (chat), got {len(committed)}"  # pyright: ignore[reportUnknownArgumentType]
    assert committed[0]["inbound_id"] == chat_id


async def test_claim_compact_summary_returns_before_llm_with_halted_false(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """compact_summary path → returns Command(goto=before_llm, halted=False).

    Lock down mutant_168 (goto=None), mutant_170 (goto kw deleted), mutant_175-177
    (halted False → True / case change)."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "summary text", "compact_summary")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys"), HumanMessage(content="m1")]),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    # Compact path detours through init_context, resuming at BEFORE_LLM (default)
    assert cmd.goto == "init_context"
    assert cmd.update["context_reset"].resume == "before_llm"  # type: ignore[index]
    # halted must be False (clears halted to enter next LLM round), cannot be True / missing
    assert cmd.update["halted"] is False  # type: ignore[index]
    # update dict key is the literal 'halted', cannot be 'HALTED' / 'XXhaltedXX'
    assert "halted" in cmd.update  # type: ignore[operator]


async def test_claim_chat_message_carries_source_metadata(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """chat dispatch passes source=item.source to inbound_message helper, cannot be None.

    Lock down mutant_76: `source=item.source` → `source=None`. Verify message's
    additional_kwargs.ava_source equals original item.source ('user'), preventing None leak."""
    tid = spawn_agent()
    insert_inbound_message(db_conn, tid, "hello", source="user")

    cmd = await claim_node(
        AgentState(),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )
    msgs = cmd.update["messages"]  # type: ignore[index]
    chat_msg = msgs[0]
    assert isinstance(chat_msg, HumanMessage)
    assert chat_msg.additional_kwargs.get("ava_source") == "user"  # pyright: ignore[reportUnknownMemberType]


async def test_claim_restart_completed_with_payload_overlay_preserves_args(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """restart_completed inbound contains payload → claim passes both source + payload arguments
    to _render_restart_completed_marker; source determines the marker wording, payload determines
    'with config {...}' segment.

    Lock down mutation that replaces source / payload args with None or skips them: losing source
    misses the 'restarted by' wording, losing payload misses overlay diff segment."""
    tid = spawn_agent()
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
            "VALUES (%s, '', 'restart_completed', 'user', "
            '\'{"config_overlay": {"foo": "bar"}}\'::jsonb)',
            (tid,),
        )
    db_conn.commit()

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )
    msgs = cmd.update["messages"]  # type: ignore[index]
    lifecycle = msgs[0]
    assert isinstance(lifecycle, HumanMessage)
    assert isinstance(lifecycle.content, str)  # pyright: ignore[reportUnknownMemberType]
    text = lifecycle.content
    # source 'user' was really passed (changing to None would lose the "by user" wording)
    assert "restarted by user" in text
    # payload was really passed (changing to None / removing arg would cause overlay segment to not appear)
    assert "with config {foo='bar'}" in text


async def test_claim_node_wrapper_returns_underlying_command(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """`claim_node` is a thin wrapper around `node_lifecycle` enter/exit, **must** return the
    inner `_claim_node_impl`'s Command (cannot drop / change goto / wrap into something else).
    Lock down mutation that removes `await` / `return` from `return await _claim_node_impl(...)`."""
    tid = spawn_agent()
    insert_inbound_message(db_conn, tid, "wrapper test", source="user")

    cmd = await claim_node(
        AgentState(),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    # wrapper must return Command, goto field must be 'before_llm' as decided by _claim_node_impl
    assert isinstance(cmd, Command)
    assert cmd.goto == "before_llm"
    # update must contain chat dispatch's messages
    assert "messages" in (cmd.update or {})  # type: ignore[operator]


# ────────────────────────────────────────────────────────────────────────
# claim_agent_row_or_die_on_stale_schema — schema gate ahead of the row claim
# ────────────────────────────────────────────────────────────────────────
# Boot must verify the central DB schema matches this code BEFORE flipping the
# row directly to running. Gating first keeps a doomed child unclaimed.


def test_claim_or_die_current_schema_claims_running(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
):
    """Schema current -> assert_schema_current is a no-op -> the row is claimed
    unclaimed idling -> running with this process's pid, exactly as the bare
    claim would."""
    monkeypatch.setattr("agent._starting.assert_schema_current", lambda _url: None)  # pyright: ignore[reportUnknownArgumentType]
    from agent._starting import claim_agent_row_or_die_on_stale_schema

    tid = spawn_agent()
    claim_agent_row_or_die_on_stale_schema(tid)

    with db_conn.cursor() as cur:
        cur.execute("SELECT status, pid FROM agents_meta WHERE id = %s", (tid,))
        status, pid = cur.fetchone()  # type: ignore[misc]
    assert status == "running"
    assert pid == os.getpid()


def test_claim_or_die_stale_schema_marks_terminated_without_claiming(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
):
    """A stale-schema child terminates its still-unclaimed row without a pid."""
    from shared.migrations import CodeBehindSchema

    def _boom(_url: object) -> None:
        raise CodeBehindSchema("DB schema version 24, code only up to 23")

    monkeypatch.setattr("agent._starting.assert_schema_current", _boom)
    from agent._starting import claim_agent_row_or_die_on_stale_schema

    tid = spawn_agent()

    with pytest.raises(CodeBehindSchema):
        claim_agent_row_or_die_on_stale_schema(tid)

    with db_conn.cursor() as cur:
        cur.execute("SELECT status, pid FROM agents_meta WHERE id = %s", (tid,))
        status, pid = cur.fetchone()  # type: ignore[misc]
    assert status == "terminated"
    assert pid is None


def test_claim_or_die_code_ahead_of_db_marks_terminated_without_claiming(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
):
    """Code ahead of the DB (`SchemaVersionMismatch` — pending migrations the DB
    has not seen, e.g. the prod checkout switched to a feature branch) is the
    other mismatch direction and must be gated identically: the row is marked
    'terminated', never claims a pid, and the error re-raises so the process
    exits instead of stranding the unclaimed row until a 10s
    spawn-poll timeout."""
    from shared.migrations import SchemaVersionMismatch

    def _boom(_url: object) -> None:
        raise SchemaVersionMismatch("DB schema version 23, code requires 24 (pending: 1)")

    monkeypatch.setattr("agent._starting.assert_schema_current", _boom)
    from agent._starting import claim_agent_row_or_die_on_stale_schema

    tid = spawn_agent()

    with pytest.raises(SchemaVersionMismatch):
        claim_agent_row_or_die_on_stale_schema(tid)

    with db_conn.cursor() as cur:
        cur.execute("SELECT status, pid FROM agents_meta WHERE id = %s", (tid,))
        status, pid = cur.fetchone()  # type: ignore[misc]
    assert status == "terminated"
    assert pid is None


def test_claim_placement_mismatch_marks_terminated_without_claiming(
    db_conn: psycopg.Connection,
):
    """agents_meta.machine != this host -> the placement gate marks the
    still-unclaimed row 'terminated' (same as the schema gate) and raises, so
    the launcher's confirm poll fails fast on 'terminated'+no-pid instead of
    waiting out its full timeout on a row that will never be claimed (the
    agent-1513 wrong-host crash loop)."""
    from agent._starting import claim_agent_row

    tid = spawn_agent()
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET machine = 'other-host' WHERE id = %s", (tid,))
        assert cur.rowcount == 1
    db_conn.commit()

    with pytest.raises(RuntimeError, match="placement mismatch"):
        claim_agent_row(tid)

    with db_conn.cursor() as cur:
        cur.execute("SELECT status, pid FROM agents_meta WHERE id = %s", (tid,))
        status, pid = cur.fetchone()  # type: ignore[misc]
    assert status == "terminated"
    assert pid is None


def test_mark_preclaim_terminated_guarded_to_unclaimed_idling(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
):
    """The pre-claim terminal write cannot clobber an already-running row."""
    from agent._starting import _mark_preclaim_terminated

    tid = spawn_agent()
    _set_agent_status(db_conn, tid, "running")

    _mark_preclaim_terminated(tid)

    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        assert cur.fetchone()[0] == "running"  # type: ignore[index]


async def test_claim_restart_self_source_sets_update_initiated(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """self source RESTART → update_initiated stays True, writing 'this agent
    is in an update session' into Checkpoint (historical: only the removed
    self:update path set it; self.restart preserves whatever was there)."""
    tid = spawn_agent()
    _set_agent_status(db_conn, tid, "running")
    _insert_inbound_kind(db_conn, tid, "", "restart", source="self")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    assert cmd.goto == END
    assert cmd.update["update_initiated"] is True  # type: ignore[index]


async def test_claim_restart_completed_system_update_clears_update_initiated(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """system:update RESTART_COMPLETED with current update_initiated=True → return
    update_initiated becomes False, update session ends."""
    tid = spawn_agent()
    _set_agent_status(db_conn, tid, "running")
    _insert_inbound_kind(db_conn, tid, "", "restart_completed", source="system:update")

    cmd = await claim_node(
        AgentState(
            messages=[SystemMessage(content="sys")],
            update_initiated=True,
        ),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    assert cmd.goto == "before_llm"
    assert cmd.update["update_initiated"] is False  # type: ignore[index]


async def test_claim_restart_completed_non_system_update_preserves_update_initiated(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """non-system:update RESTART_COMPLETED → update_initiated unchanged, flag not cleared."""
    tid = spawn_agent()
    _set_agent_status(db_conn, tid, "running")
    _insert_inbound_kind(db_conn, tid, "", "restart_completed", source="self")

    cmd = await claim_node(
        AgentState(
            messages=[SystemMessage(content="sys")],
            update_initiated=True,
        ),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    assert cmd.goto == "before_llm"
    assert cmd.update["update_initiated"] is True  # type: ignore[index]


async def test_claim_node_idle_enter_publishes_full_window_snapshot(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
):
    """Turn-end fallback: when claim is about to idle (no conversation yet),
    the wrapper must pass full_window=True so the enter snapshot is the full
    window — the only race-free view of the finished turn (reconnect GET can
    read a lagging checkpoint). Pins the will_idle wiring."""
    import json

    from langchain_core.messages import SystemMessage

    from agent.graph import _claim
    from agent.graph._claim import claim_node

    tid = spawn_agent()

    # stub the body: we only exercise the wrapper + node_lifecycle enter path
    async def _stub_impl(_state, _runtime, _config):
        return Command(goto="end")

    monkeypatch.setattr(_claim, "_claim_node_impl", _stub_impl)  # pyright: ignore[reportUnknownArgumentType]

    pub = MagicMock()
    state = AgentState()
    state.messages = [SystemMessage(content="prompt")]
    await claim_node(
        state,
        _make_runtime(ops_pool=aops_pool, event_publisher=pub),
        _config(tid),
    )
    snaps = [
        json.loads(c.args[0]) for c in pub.emit.call_args_list if "timeline_snapshot" in c.args[0]
    ]
    assert len(snaps) == 1
    # will_idle=True (no conversation) → full-window: msg_count = full length,
    # window renders the whole (short) history — including the system-prompt
    # item (no Aw-Snap drop rule anymore: incremental snapshots never carry
    # 0.0 by construction, full-window ones are rare and the frontend's
    # id-replace merge keeps a single copy either way).
    assert snaps[0]["msg_count"] == 1
    assert [it["item_id"] for it in snaps[0]["items"]] == ["0.0"]
    assert snaps[0]["items"][0]["kind"] == "system_prompt"


async def test_claim_restart_cas_lost_retries_from_idling(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    aredis_inbound_listener: RedisInboundListener,
):
    """A concurrent op left the row 'idling' between the claim's running-mark and the
    restart CAS (audit #689 G2 — the pre-fix code raised RuntimeError out of the graph
    and killed the process; a crash during a network outage cannot be resurrected).
    _flip_to_restarting re-reads the row, retries the flip from 'idling', and the
    claim completes normally with goto END."""
    tid = spawn_agent()
    _set_agent_status(db_conn, tid, "idling")  # race: row no longer 'running'
    restart_id = _insert_inbound_kind(db_conn, tid, "", "restart", source="user")
    await _await_inbound_visible(aops_pool, restart_id)

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool, inbound_listener=aredis_inbound_listener),
        _config(tid),
    )

    assert cmd.goto == END
    # retry from idling won the CAS: row is 'restarting', restarter will respawn
    await _await_status(aops_pool, tid, "restarting")


async def test_flip_to_restarting_cas_lost_foreign_status_ends_normally(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
):
    """_flip_to_restarting loses the running→restarting CAS to a foreign status
    (e.g. a terminate parked the row 'terminated' between the claim's
    running-mark and the CAS) — it logs and returns without raising, leaving
    the row untouched. Pre-fix code raised RuntimeError out of the graph and
    killed the process; a crash during a network outage cannot be resurrected
    (audit #689 G2, agent 2147 2026-08-03: 4h dead)."""
    tid = spawn_agent()
    _set_agent_status(db_conn, tid, "terminated")  # race: terminate won

    from agent.graph._claim import _flip_to_restarting

    await _flip_to_restarting(aops_pool, tid)  # must not raise

    await _await_status(aops_pool, tid, "terminated")  # untouched


async def test_flip_to_restarting_cas_lost_retries_from_idling(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
):
    """_flip_to_restarting loses the running→restarting CAS to a re-entered claim
    that left the row 'idling' — it re-reads and retries from the actual live
    state, landing 'restarting' for the restarter (the claim-level variant of
    this is covered by test_claim_restart_cas_lost_retries_from_idling)."""
    tid = spawn_agent()
    _set_agent_status(db_conn, tid, "idling")

    from agent.graph._claim import _flip_to_restarting

    await _flip_to_restarting(aops_pool, tid)

    await _await_status(aops_pool, tid, "restarting")


def test_claim_agent_row_grants_the_liveness_lease(db_conn: psycopg.Connection) -> None:
    """R1 (Task #1021): the claim UPDATE writes `lease_expires_at` (now + TTL)
    in the same statement as running — the claim is the lease's birth, and
    the run loop's renewer keeps it fresh from there."""
    from agent._starting import claim_agent_row

    tid = spawn_agent()
    claim_agent_row(tid)

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT status, lease_expires_at > now() FROM agents_meta WHERE id = %s", (tid,)
        )
        status, lease_live = cur.fetchone()  # type: ignore[misc]
    assert status == "running"
    assert lease_live is True
