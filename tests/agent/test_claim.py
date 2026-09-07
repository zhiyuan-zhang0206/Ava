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
from collections.abc import Callable
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
    llm: Any | None = None,
    event_publisher: Any | None = None,
) -> Runtime[AvaContext]:
    """test helper: assemble AvaContext into Runtime.

    `ops_pool=None` takes the container early-return path;

    InboundCommitted SSE fan-out goes through `ctx.event_publisher.emit`; default to a MagicMock
    so the node's `assert ctx.event_publisher` passes; tests verifying InboundCommitted pass their own
    mock to assert `pub.emit.call_args_list`.

    """
    ctx = AvaContext(
        ops_pool=ops_pool,
        llm=llm if llm is not None else _fake_llm(),
        event_publisher=event_publisher if event_publisher is not None else MagicMock(),
    )
    return Runtime(context=ctx)


def _insert_inbound_kind(
    db: psycopg.Connection, tid: int, content: str, kind: str, source: str = "system"
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
    return {
        "configurable": {
            "thread_id": str(
                tid,
            )
        }
    }


@pytest.fixture
async def running_agent(aops_pool: AsyncConnectionPool):
    """Admit a real hosted owner and bind it throughout each dispatch test."""
    from uuid import uuid4

    from agent.hosted_ownership import admit_hosted_runtime
    from shared.machine import machine_name
    from shared.turn_identity import bind_turn_identity

    agent_id = spawn_agent()
    incarnation = await admit_hosted_runtime(
        aops_pool, agent_id, machine_name(), uuid4(), expected_from="idling"
    )
    assert incarnation is not None
    with bind_turn_identity(agent_id, incarnation=incarnation):
        yield lambda: agent_id


async def test_claim_first_entry_keeps_boot_claim_running(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
):
    """The bootstrap claim already sets running before the first graph entry."""
    tid = spawn_agent()
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'running' WHERE id = %s", (tid,))
    db_conn.commit()
    insert_inbound_message(db_conn, tid, "hello", source="user")

    await claim_node(
        AgentState(),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        row = cur.fetchone()
    assert row is not None and row[0] == "running"


async def test_claim_subsequent_entry_does_not_disturb_running(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
):
    """A subsequent graph entry leaves its already-running row untouched."""
    tid = spawn_agent()
    _set_agent_status(db_conn, tid, "running")
    insert_inbound_message(db_conn, tid, "hello", source="user")

    # Don't raise — status is already 'running' when the next turn enters claim_node, 0-row no-op
    await claim_node(
        AgentState(),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        row = cur.fetchone()
    assert row is not None and row[0] == "running"


async def test_claim_inbound_batch_stamps_claimed_at(
    running_agent: Callable[[], int], db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """Only the accepted command gets pickup time; queued chat stays unclaimed."""
    from agent.db import claim_inbound_batch

    tid = running_agent()
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
    assert set(by_id) == {term_id}
    assert by_id[term_id].claimed_at is not None

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT id, status, claimed_at FROM inbound_messages WHERE id = ANY(%s) ORDER BY id",
            ([chat_id, term_id],),
        )
        state = cur.fetchall()
    assert [(r[0], r[1]) for r in state] == [(chat_id, "pending"), (term_id, "claimed")]
    assert state[0][2] is None and state[1][2] is not None

    # A fresh unclaimed row keeps claimed_at NULL.
    fresh_id = insert_inbound_message(db_conn, tid, "later", source="user")
    with db_conn.cursor() as cur:
        cur.execute("SELECT claimed_at FROM inbound_messages WHERE id = %s", (fresh_id,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] is None


async def test_claim_chat_kind_appends_humanmessage_with_envelope(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
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
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
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
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """A `/<name> ...` chat inbound is expanded by the claim node into the
    command's template + the user's note before being wrapped for the model."""
    tid = spawn_agent()
    insert_inbound_message(db_conn, tid, "/recap just the PRs", source="user")

    cmd = await claim_node(
        AgentState(),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    content = cmd.update["messages"][-1].content  # type: ignore[index]
    # Envelope attributes the sender; the expansion body is source-neutral.
    assert content.startswith("User ")  # pyright: ignore[reportUnknownMemberType]
    assert "Command /recap:" in content
    assert "Additional message: just the PRs" in content


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

    from agent.hooks.compact import COMPACT_MAX_ATTEMPTS, CompactionFailedError

    with pytest.raises(CompactionFailedError, match="no usable summary across"):
        await claim_node(
            state,
            _make_runtime(ops_pool=aops_pool, llm=llm),
            _config(
                tid,
            ),
        )
    assert failing.await_count == COMPACT_MAX_ATTEMPTS


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
    )  # raw file content carried through


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
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
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
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
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


async def test_claim_unknown_kind_raises(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
):
    """Unrecognized inbound kind = framework / DB schema desync — immediately raise,
    do not silently swallow bugs by 'defaulting to chat processing'.

    The DB CHECK constraint prevents production unknown kind, so it cannot be constructed
    via INSERT path; use monkeypatch to directly feed ClaimedInbound to claim_node to verify
    the dispatch's `case _:` fallback branch.
    """
    from agent.db import ClaimedInbound

    tid = spawn_agent()

    async def fake_claim(_db, _tid, *, lifecycle_only=False):
        assert not lifecycle_only
        return [ClaimedInbound(id=99, agent_id=tid, content="x", kind="bogus", source="system")]

    monkeypatch.setattr("agent.graph._claim.claim_inbound_batch", fake_claim)  # pyright: ignore[reportUnknownArgumentType]

    with pytest.raises(ValueError, match="Unknown inbound kind"):
        await claim_node(
            AgentState(),
            _make_runtime(ops_pool=aops_pool),
            _config(
                tid,
            ),
        )


async def test_claim_terminate_kind_appends_lifecycle_marker_and_routes_to_end(
    running_agent: Callable[[], int], db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """terminate inbound → claim appends lifecycle marker (HumanMessage containing
    'Termination was accepted from {source}' text + ava_msg_type='lifecycle' metadata)
    + goto END with exit_requested=True, so the per-turn runloop returns
    (instead of re-invoking) and the process exits naturally."""
    tid = running_agent()
    _insert_inbound_kind(db_conn, tid, "", "terminate", source="user")

    cmd = await claim_node(
        AgentState(),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
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
    assert "Termination was accepted from user" in content
    # marker content has timestamp prefix + [system] in single brackets (now_timestamp already has square brackets, no nesting)
    assert content.startswith("[")  # timestamp start: e.g. [2026-...
    assert "[system]" in content
    assert "[system [" not in content  # anti-regression: double bracket bug
    assert lifecycle.additional_kwargs.get("ava_msg_type") == "system_note"  # pyright: ignore[reportUnknownMemberType]
    assert lifecycle.additional_kwargs.get("ava_note_tag") == "lifecycle_terminate"  # pyright: ignore[reportUnknownMemberType]


async def test_claim_turn_boundary_ends_invocation_instead_of_waiting(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
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
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )
    assert cmd.goto == END
    # Turn boundary only: no process exit, no other state touched.
    assert cmd.update == {"turn_active": False}


async def test_claim_hosted_ends_turn_instead_of_parking(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """Hosted mode has no process to park: a fresh invocation (turn_active=False)
    that finds nothing must goto END with `turn_idle`, not enter the IDLING wait.

    No inbound is inserted; the empty queue must end the invocation immediately.

    `exit_requested` stays False: an idle agent is not a terminated one — the
    host drops the task and re-creates it on the next wake.
    """
    tid = spawn_agent()

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True, turn_active=False),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    assert cmd.goto == END
    assert cmd.update == {"turn_active": False, "turn_idle": True}


async def test_claim_hosted_never_enters_idling_status(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
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
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        row = cur.fetchone()
    assert row is not None
    # The row was already running; the hosted claim branch must return without
    # touching it.
    assert row[0] == "running", f"hosted claim left status {row[0]!r}"


async def test_claim_hosted_still_dispatches_an_available_batch(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """Hosted mode changes only the empty-batch branch. When the first SELECT
    finds work, dispatch is byte-for-byte the process path — the turn runs, and
    `turn_idle` is NOT set (the host must re-invoke, not end the task)."""
    tid = spawn_agent()
    insert_inbound_message(db_conn, tid, "hello", kind="chat", source="user")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True, turn_active=False),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    assert cmd.goto == "before_llm"
    assert cmd.update["turn_active"] is True  # type: ignore[index]
    assert cmd.update.get("turn_idle") in (None, False)  # type: ignore[union-attr]


async def test_claim_cancel_kind_halts_to_idle_without_marker(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """cancel inbound → pause: halted=True + re-enter CLAIM (-> idle), NOT END
    (process stays alive). No lifecycle marker (a pause leaves no trace); a
    Cancelled SSE is emitted so the live UI clears turn-active state."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "cancel", source="user")
    pub = MagicMock()

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool, event_publisher=pub),
        _config(
            tid,
        ),
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
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
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
        _make_runtime(ops_pool=aops_pool, event_publisher=pub),
        _config(
            tid,
        ),
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
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
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
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    assert cmd.goto == "before_llm"
    assert cmd.update["halted"] is False  # type: ignore[index]
    msgs = cmd.update["messages"]  # type: ignore[index]
    assert len(msgs) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert isinstance(msgs[0], HumanMessage)
    assert "new instruction" in msgs[0].content  # pyright: ignore[reportUnknownMemberType]


async def test_claim_cancel_batched_with_terminate_terminate_wins(
    running_agent: Callable[[], int], db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """cancel + terminate in the same claim batch (user clicks Stop then
    Terminate before claim runs) → terminate WINS: goto END (process exits), not
    the cancel idle. Both rows are claimed/done in one pass; the pause must not
    swallow the stronger kill. Regression for the cancel-over-terminate
    precedence inversion."""
    tid = running_agent()
    # insertion order shouldn't matter; put cancel first to make the override tempting
    _insert_inbound_kind(db_conn, tid, "", "cancel", source="user")
    _insert_inbound_kind(db_conn, tid, "", "terminate", source="user")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )
    assert cmd.goto == END  # terminate wins; agent exits, cancel does not keep it alive
    # the terminate lifecycle marker is still committed
    msgs = cmd.update["messages"]  # type: ignore[index]
    assert any(
        isinstance(m, HumanMessage)
        and isinstance(m.content, str)  # pyright: ignore[reportUnknownMemberType]
        and "Termination was accepted" in m.content
        for m in msgs
    )


async def test_claim_lifecycle_marker_drops_timestamp_when_disabled(
    running_agent: Callable[[], int],
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
):
    """settings.general.message_timestamps=False → the lifecycle marker has no leading
    timestamp; it starts straight at `[system]` with no stray space."""
    monkeypatch.setattr(settings.general, "message_timestamps", False)
    tid = running_agent()
    _insert_inbound_kind(db_conn, tid, "", "terminate", source="user")

    cmd = await claim_node(
        AgentState(),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )
    content = cmd.update["messages"][-1].content  # type: ignore[index]
    assert content.startswith("[system] Termination was accepted from user")  # pyright: ignore[reportUnknownMemberType]


async def test_claim_terminate_self_renders_by_yourself(
    running_agent: Callable[[], int], db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """source='self' (ava.self.terminate() suicide) → marker text spells 'by yourself'
    instead of 'by self', more accurately expressing 'agent shuts itself down' semantics."""
    tid = running_agent()
    _insert_inbound_kind(db_conn, tid, "", "terminate", source="self")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )
    assert cmd.goto == END
    msgs = cmd.update["messages"]  # type: ignore[index]
    lifecycle = msgs[0]
    assert "Termination was accepted from yourself" in lifecycle.content  # pyright: ignore[reportUnknownMemberType]


async def test_claim_self_terminate_retains_peer_chat_for_successor(
    running_agent: Callable[[], int], db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """Accepting self termination never acknowledges an unseen peer message."""
    tid = running_agent()
    insert_inbound_message(db_conn, tid, "peer message during suicide", source="agent:1")
    terminate_id = _insert_inbound_kind(db_conn, tid, "", "terminate", source="self")
    await _await_inbound_visible(aops_pool, terminate_id)

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    assert cmd.goto == END
    msgs = cmd.update["messages"]  # type: ignore[index]
    assert len(msgs) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert isinstance(msgs[0], HumanMessage)
    assert "Termination was accepted" in msgs[0].content  # pyright: ignore[reportUnknownMemberType]
    assert db_conn.execute(
        "SELECT status,content FROM inbound_messages WHERE agent_id=%s AND kind='chat'", (tid,)
    ).fetchone() == ("pending", "peer message during suicide")


async def test_claim_self_terminate_retains_older_user_chat(
    running_agent: Callable[[], int], db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """An older user chat remains durable without vetoing the accepted command."""
    tid = running_agent()
    chat_id = insert_inbound_message(db_conn, tid, "queued before the suicide", source="user")
    await _await_inbound_visible(aops_pool, chat_id)
    _insert_inbound_kind(db_conn, tid, "", "terminate", source="self")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    assert cmd.goto == END
    msgs = cmd.update["messages"]  # type: ignore[index]
    assert any("Termination was accepted" in m.content for m in msgs)  # pyright: ignore[reportUnknownMemberType]
    assert db_conn.execute(
        "SELECT status,content FROM inbound_messages WHERE id=%s", (chat_id,)
    ).fetchone() == ("pending", "queued before the suicide")


async def test_claim_external_terminate_retains_newer_chat(
    running_agent: Callable[[], int], db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """Newer chat does not replace an accepted lifecycle command or get lost."""
    tid = running_agent()
    _insert_inbound_kind(db_conn, tid, "", "terminate", source="user")
    chat_id = insert_inbound_message(db_conn, tid, "message after the kill", source="user")
    await _await_inbound_visible(aops_pool, chat_id)

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    assert cmd.goto == END
    msgs = cmd.update["messages"]  # type: ignore[index]
    assert any("Termination was accepted" in m.content for m in msgs)  # pyright: ignore[reportUnknownMemberType]
    assert db_conn.execute(
        "SELECT status,content FROM inbound_messages WHERE id=%s", (chat_id,)
    ).fetchone() == ("pending", "message after the kill")


async def test_claim_external_terminate_with_older_chat_still_dies(
    running_agent: Callable[[], int], db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """Regression guard: a deliberate external kill is NOT vetoed by chats that
    predate it — the actor decided with the pending queue visible, so the
    terminate is the latest intent and the agent dies (END + marker). The
    pre-death chat is committed to history as before; it is visible after a
    resurrect."""
    tid = running_agent()
    chat_id = insert_inbound_message(db_conn, tid, "old message before the kill", source="user")
    await _await_inbound_visible(aops_pool, chat_id)
    _insert_inbound_kind(db_conn, tid, "", "terminate", source="user")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    assert cmd.goto == END
    msgs = cmd.update["messages"]  # type: ignore[index]
    assert any("Termination was accepted from user" in m.content for m in msgs)  # pyright: ignore[reportUnknownMemberType]
    assert db_conn.execute(
        "SELECT status,content FROM inbound_messages WHERE agent_id=%s AND kind='chat'", (tid,)
    ).fetchone() == ("pending", "old message before the kill")


async def test_claim_terminate_vetoed_by_pending_inbound_after_claim(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
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

    async def fake_claim(_pool, _agent_id, *, lifecycle_only=False):
        assert not lifecycle_only
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
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    # re-enter claim to dispatch the fresh message — not END, not a wake
    assert cmd.goto == "claim"
    assert cmd.goto != END
    # no terminate marker was committed
    assert cmd.update["messages"] == []  # type: ignore[index]
    # the newer chat is still pending for the re-entered claim to pick up
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM inbound_messages WHERE agent_id = %s AND kind = 'chat'", (tid,)
        )
        chat_row = cur.fetchone()
        assert chat_row is not None
        assert chat_row[0] == "pending"
    # the vetoed terminate row is consumed, never retried
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM inbound_messages WHERE agent_id = %s AND kind = 'terminate'", (tid,)
        )
        term_row = cur.fetchone()
        assert term_row is not None
        assert term_row[0] == "done"


async def test_claim_restart_kind_hosted_ends_turn_and_stays_runnable(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """Hosted restart: goto END with `restart_requested` (not `exit_requested`),
    leaves lifecycle application to the host after the acceptance checkpoint
    has been flushed."""
    from shared.turn_identity import bind_turn_identity
    from tests.agent.test_inbound_ownership import _admit, _agent

    tid = _agent(db_conn)
    owner = await _admit(aops_pool, tid)
    restart_id = _insert_inbound_kind(db_conn, tid, "", "restart", source="user")
    await _await_inbound_visible(aops_pool, restart_id)

    with bind_turn_identity(tid, incarnation=owner):
        cmd = await claim_node(
            AgentState(messages=[SystemMessage(content="sys")]),
            _make_runtime(ops_pool=aops_pool),
            _config(
                tid,
            ),
        )

    assert cmd.goto == END
    assert cmd.update["restart_requested"] is True  # type: ignore[index]
    assert cmd.update["exit_requested"] is False  # type: ignore[index]
    msgs = cmd.update["messages"]  # type: ignore[index]
    assert len(msgs) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert "Restart was accepted from user" in msgs[0].content  # pyright: ignore[reportUnknownMemberType]
    # The host has not applied the accepted restart yet.
    await _await_status(aops_pool, tid, "running")
    assert db_conn.execute(
        "SELECT status,applied_at,observed_at FROM inbound_messages WHERE id=%s", (restart_id,)
    ).fetchone() == ("claimed", None, None)


async def test_claim_restart_completed_kind_appends_marker_and_continues(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """Historical restart_completed inbound → claim appends
    lifecycle marker 'You have been restarted by {source}' + goto BEFORE_LLM.
    halted=False (mid-task before restart) → wakes up to resume interrupted work."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "restart_completed", source="user")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
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
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
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
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
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
    assert cmd.update["active_task_id"] == 1  # pyright: ignore[reportOptionalSubscript]
    # The inbound row is consumed (done at claim, like other lifecycle kinds).
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM inbound_messages WHERE id = %s", (inbound_id,))
        status = cur.fetchone()
        assert status is not None and status[0] == "done"


async def test_claim_co_batched_task_notes_leave_usage_untagged(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
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
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    assert cmd.goto == "before_llm"
    assert cmd.update["active_task_id"] is None  # type: ignore[index]


async def test_claim_system_note_unknown_tag_fails_loud(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
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
            _make_runtime(ops_pool=aops_pool),
            _config(
                tid,
            ),
        )


async def test_claim_restart_while_idle_commits_halted_true(
    running_agent: Callable[[], int], db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """external restart hits idle agent (halted=True) → committed halted=True,
    carrying 'no in-flight work before restart' across the restart boundary for the new process to read."""
    tid = running_agent()
    _set_agent_status(db_conn, tid, "running")
    _insert_inbound_kind(db_conn, tid, "", "restart", source="system:update")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    assert cmd.goto == END
    assert cmd.update["halted"] is True  # type: ignore[index]


@pytest.mark.parametrize("source", ["self"])
async def test_claim_restart_self_source_commits_halted_false(
    running_agent: Callable[[], int],
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    source: str,
):
    """agent-initiated restart (ava.self.restart) → even if the exec path
    set halted to True (turn-end semantics), committed halted must be False — agent has in-flight
    intent, must wake after restart to confirm result and continue."""
    tid = running_agent()
    _set_agent_status(db_conn, tid, "running")
    _insert_inbound_kind(db_conn, tid, "", "restart", source=source)

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    assert cmd.goto == END
    assert cmd.update["halted"] is False  # type: ignore[index]


async def test_claim_restart_system_update_after_self_update_wakes(
    running_agent: Callable[[], int], db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """update_initiated=True in state (historical checkpoint from the removed
    self:update path) + the rollout quiesce system:update restart → committed
    halted must be False — an update-interrupted agent wakes after restart, not
    silently idle."""
    tid = running_agent()
    _set_agent_status(db_conn, tid, "running")
    _insert_inbound_kind(db_conn, tid, "", "restart", source="system:update")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True, update_initiated=True),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    assert cmd.goto == END
    assert cmd.update["halted"] is False  # type: ignore[index]


async def test_claim_restart_preserves_chat_for_successor(
    running_agent: Callable[[], int], db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """Only the accepted lifecycle command dispatches; chat remains durable pending work."""
    tid = running_agent()
    _set_agent_status(db_conn, tid, "running")
    insert_inbound_message(db_conn, tid, "hello", source="user")
    _insert_inbound_kind(db_conn, tid, "", "restart", source="system:update")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    assert cmd.goto == END
    assert cmd.update["halted"] is True  # type: ignore[index]
    msgs = cmd.update["messages"]  # type: ignore[index]
    restart_messages = cast(list[AnyMessage], msgs)
    assert (
        len(restart_messages) == 1
        and "Restart was accepted" in restart_messages[0].model_dump()["content"]
    )
    assert db_conn.execute(
        "SELECT status,content FROM inbound_messages WHERE agent_id=%s AND kind='chat'", (tid,)
    ).fetchone() == ("pending", "hello")


async def test_claim_restart_before_terminate_preserves_serial_order(
    running_agent: Callable[[], int], db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """The active restart is not discarded by a later termination request."""
    tid = running_agent()
    _set_agent_status(db_conn, tid, "running")
    # Later termination remains pending for the admitted successor.
    _insert_inbound_kind(db_conn, tid, "", "restart", source="user")
    terminate_id = _insert_inbound_kind(db_conn, tid, "", "terminate", source="user")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    assert cmd.goto == END
    await _await_status(aops_pool, tid, "running")
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (terminate_id,)
    ).fetchone() == ("pending",)


async def test_claim_terminate_before_restart_completed_still_exits(
    running_agent: Callable[[], int], db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """terminate arrives first in the agent downtime window (smaller id, ordered earlier in batch),
    boot batch [terminate, restart_completed] → goto must be END. Lock down 'boot marker must not
    override the stronger END back to wake' — the marker arm once unconditionally set
    next_goto=BEFORE_LLM; this ordering would consume the terminate but the agent wakes up alive
    (ordering-dependent bug)."""
    tid = running_agent()
    _insert_inbound_kind(db_conn, tid, "", "terminate", source="user")
    _insert_inbound_kind(db_conn, tid, "", "restart_completed", source="user")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    assert cmd.goto == END
    # The accepted command does not consume an unrelated completion marker.
    contents = [m.content for m in cmd.update["messages"]]  # type: ignore[index]
    assert any("Termination was accepted from user" in c for c in contents)
    assert not any("You have been restarted" in c for c in contents)
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE agent_id=%s AND kind='restart_completed'", (tid,)
    ).fetchone() == ("pending",)


@pytest.mark.flaky  # poll _await_status for claim_node status transition
async def test_claim_cancel_batched_with_restart_idle_restart_silent(
    running_agent: Callable[[], int], db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """cancel + restart in the same batch (user clicks Stop then Restart) → restart wins over cancel:
    exits normally via restart; the Cancelled event for cancel is still emitted (frontend clears
    turn-active), the pause intent is absorbed into 'silent idle after restart' (halted=True preserved)."""
    tid = running_agent()
    _set_agent_status(db_conn, tid, "running")
    _insert_inbound_kind(db_conn, tid, "", "cancel", source="user")
    restart_id = _insert_inbound_kind(db_conn, tid, "", "restart", source="user")
    await _await_inbound_visible(aops_pool, restart_id)
    pub = MagicMock()

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        _make_runtime(ops_pool=aops_pool, event_publisher=pub),
        _config(
            tid,
        ),
    )

    # restart exit, not the cancel pause branch
    assert cmd.goto == END
    # idle before restart (halted=True) + external → silent after restart
    assert cmd.update["halted"] is True  # type: ignore[index]
    # No phantom cancellation: the successor will consume the still-pending command.
    assert not any("cancelled" in str(c.args[0]).lower() for c in pub.emit.call_args_list)
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE agent_id=%s AND kind='cancel'", (tid,)
    ).fetchone() == ("pending",)
    await _await_status(aops_pool, tid, "running")


@pytest.mark.flaky  # poll _await_status for claim_node status transition
async def test_claim_second_restart_batched_with_restart_completed_exits_again(
    running_agent: Callable[[], int], db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """boot batch [restart_completed, restart] (user clicked restart again within the restart window) →
    the second restart wins over wake: after committing marker, exits again via restart, idle preserved
    (external + halted=True) → second restart remains silent."""
    tid = running_agent()
    _set_agent_status(db_conn, tid, "running")
    _insert_inbound_kind(db_conn, tid, "", "restart_completed", source="user")
    restart_id = _insert_inbound_kind(db_conn, tid, "", "restart", source="user")
    await _await_inbound_visible(aops_pool, restart_id)

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    assert cmd.goto == END
    assert cmd.update["halted"] is True  # type: ignore[index]
    msgs = cmd.update["messages"]  # type: ignore[index]
    restart_messages = cast(list[AnyMessage], msgs)
    assert (
        len(restart_messages) == 1
        and "Restart was accepted" in restart_messages[0].model_dump()["content"]
    )
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE agent_id=%s AND kind='restart_completed'", (tid,)
    ).fetchone() == ("pending",)
    await _await_status(aops_pool, tid, "running")


@pytest.mark.flaky  # poll _await_status for claim_node status transition
async def test_claim_compact_request_batched_with_restart_is_dropped(
    running_agent: Callable[[], int], db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """compact_request + restart in same batch → compact_request is the discarded loser:
    does **not** run the backend Compaction LLM (if it raised, the already consumed restart row
    would be lost before the RESTARTING marker), restart exits normally. Re-trigger /compact afterwards."""
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


async def test_claim_restart_completed_while_idle_stays_silent(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """halted=True (idle before restart, preserved from RESTART case) + batch has only
    restart_completed → after committing lifecycle marker, goto CLAIM returns to waiting,
    does **not** enter before_llm — idle agent does not burn an LLM call for 'knowing I was restarted'."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "restart_completed", source="system:update")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
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
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """halted=True but batch has chat in addition to restart_completed (user message that arrived
    during agent downtime window) → wakes normally to before_llm, must not silently swallow the chat."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "restart_completed", source="system:update")
    insert_inbound_message(db_conn, tid, "are you back?", source="user")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    assert cmd.goto == "before_llm"
    assert cmd.update["halted"] is False  # type: ignore[index]
    msgs = cmd.update["messages"]  # type: ignore[index]
    assert len(msgs) == 2  # pyright: ignore[reportUnknownArgumentType]
    assert "updated and restarted" in msgs[0].content  # pyright: ignore[reportUnknownMemberType]
    assert "are you back?" in msgs[1].content  # pyright: ignore[reportUnknownMemberType]


async def test_claim_resurrect_kind_appends_marker_and_continues(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """resurrect inbound (delivered to the new process by resurrect_agent) → claim appends
    lifecycle marker 'You have been resurrected by {source}' + goto BEFORE_LLM."""
    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "resurrect", source="user")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
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
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """Repeated failed recoveries are consumed together but render one marker."""
    tid = spawn_agent()
    first = _insert_inbound_kind(db_conn, tid, "", "resurrect", source="system:retry")
    latest = _insert_inbound_kind(db_conn, tid, "", "resurrect", source="user")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
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
# Lifecycle commands bind to an admitted incarnation and dispatch serially.
# A notification's recency cannot stand in for completed termination or actual
# successor admission. Unaccepted work survives for the correct consumer.


async def test_unowned_resurrect_notification_cannot_cancel_pending_terminate(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """A newer notification is not admission or proof that prior intent completed."""
    tid = spawn_agent()
    # id == insertion order: terminate older than the resurrect that follows it
    _insert_inbound_kind(db_conn, tid, "", "cancel", source="user")
    _insert_inbound_kind(db_conn, tid, "", "cancel", source="user")
    _insert_inbound_kind(db_conn, tid, "", "terminate", source="user")
    _insert_inbound_kind(db_conn, tid, "", "resurrect", source="user")

    with pytest.raises(RuntimeError, match="lifecycle claim requires an admitted"):
        await claim_node(
            AgentState(messages=[SystemMessage(content="sys")]),
            _make_runtime(ops_pool=aops_pool),
            _config(
                tid,
            ),
        )
    assert db_conn.execute(
        "SELECT kind,status,claimed_at FROM inbound_messages WHERE agent_id=%s ORDER BY id", (tid,)
    ).fetchall() == [
        (kind, "pending", None) for kind in ["cancel", "cancel", "terminate", "resurrect"]
    ]


async def test_claim_resurrect_then_terminate_still_dies(
    running_agent: Callable[[], int], db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """An owned terminate dispatches alone; an older notification is not lost."""
    tid = running_agent()
    notice = _insert_inbound_kind(db_conn, tid, "", "resurrect", source="user")
    _insert_inbound_kind(db_conn, tid, "", "terminate", source="user")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    assert cmd.goto == END
    contents = [m.content for m in cmd.update["messages"]]  # type: ignore[index]
    assert any("Termination was accepted from user" in c for c in contents)
    assert not any("resurrected" in c for c in contents)
    assert db_conn.execute(
        "SELECT status,claimed_at FROM inbound_messages WHERE id=%s", (notice,)
    ).fetchone() == ("pending", None)


@pytest.mark.flaky  # poll _await_status for claim_node status transition
async def test_claim_terminate_then_restart_preserves_serial_order(
    running_agent: Callable[[], int], db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """An owned runtime accepts the first lifecycle command, not latest-wins.

    A following explicit restart remains pending for cold acceptance after exit;
    it must not silently discard the accepted termination.
    """
    tid = running_agent()
    _set_agent_status(db_conn, tid, "running")
    # A later restart cannot replace the active command pointer.
    _insert_inbound_kind(db_conn, tid, "", "terminate", source="user")
    restart_id = _insert_inbound_kind(db_conn, tid, "", "restart", source="user")
    await _await_inbound_visible(aops_pool, restart_id)

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    assert cmd.goto == END
    await _await_status(aops_pool, tid, "running")
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (restart_id,)
    ).fetchone() == ("pending",)


async def test_claim_auto_resurrect_chat_batch_wakes_and_keeps_chat(
    running_agent: Callable[[], int],
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
):
    """A settled prior command, not marker recency, protects the real successor."""

    from uuid import uuid4

    from agent.hosted_ownership import admit_hosted_runtime, apply_hosted_lifecycle
    from ops.agent_wake import resurrect_agent
    from shared.machine import machine_name
    from shared.runtime_incarnation import current_incarnation
    from shared.turn_identity import bind_turn_identity

    tid = running_agent()
    stop = _insert_inbound_kind(db_conn, tid, "", "terminate", source="user")
    assert (
        await claim_node(
            AgentState(messages=[SystemMessage(content="sys")]),
            _make_runtime(ops_pool=aops_pool),
            _config(
                tid,
            ),
        )
    ).goto == END

    old = current_incarnation(
        tid,
    )
    assert old is not None
    assert await apply_hosted_lifecycle(aops_pool, old) == "terminate"
    assert db_conn.execute(
        "SELECT status,observed_at IS NOT NULL FROM inbound_messages WHERE id=%s", (stop,)
    ).fetchone() == ("done", True)
    db_conn.commit()
    insert_inbound_message(db_conn, tid, "are you there?", source="user")
    resurrect_agent(tid, resurrected_by="user")
    launch = db_conn.execute(
        "SELECT id FROM inbound_messages WHERE agent_id=%s AND kind='resurrect' "
        "AND status='pending'",
        (tid,),
    ).fetchone()
    assert launch is not None
    db_conn.commit()
    successor = await admit_hosted_runtime(
        aops_pool, tid, machine_name(), uuid4(), expected_from="idling"
    )
    assert successor is not None

    with bind_turn_identity(tid, incarnation=successor):
        cmd = await claim_node(
            AgentState(messages=[SystemMessage(content="sys")]),
            _make_runtime(ops_pool=aops_pool),
            _config(
                tid,
            ),
        )

    assert cmd.goto == "before_llm"
    assert cmd.update["halted"] is False  # type: ignore[index]
    contents = [m.content for m in cmd.update["messages"]]  # type: ignore[index]
    assert any("You have been resurrected by user" in c for c in contents)
    assert any("are you there?" in c for c in contents)  # chat not swallowed
    assert not any("Termination was accepted" in c for c in contents)


async def test_claim_auto_resurrect_compact_request_batch_compacts_and_wakes(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
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
        _make_runtime(ops_pool=aops_pool, llm=fake_llm),
        _config(
            tid,
        ),
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
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
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
            _make_runtime(ops_pool=aops_pool),
            _config(
                tid,
            ),
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
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """The fork strip (issue #1320): inherited head notes that name the SOURCE —
    its agent id, its per-agent memory, its preloaded skills — are removed, and
    the new agent's own copies are grafted. The cluster memory index is
    cluster-wide: the inherited copy is kept and NOT re-grafted."""
    from langchain_core.messages import RemoveMessage

    tid = spawn_agent()
    _insert_inbound_kind(db_conn, tid, "", "fork", source="agent:7")

    def _tagged(tag: NoteTag, content: str, id: str) -> HumanMessage:
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
            _make_runtime(ops_pool=aops_pool),
            _config(
                tid,
            ),
        )
    finally:
        monkeypatch.undo()

    assert cmd.goto == "before_llm"
    msgs = cast(list[BaseMessage], (cmd.update or {})["messages"])
    # The rebuild: one full-wipe marker, then the inherited history re-listed
    # with the three source-identity notes dropped (the cluster index — the
    # SYSTEM_NOTE not owned by the source — survives the rebuild).
    assert isinstance(msgs[0], RemoveMessage) and msgs[0].id == REMOVE_ALL_MESSAGES
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
    grafted_content: object = tail[-1].content  # pyright: ignore[reportUnknownMemberType]
    assert isinstance(grafted_content, str) and "source's memory" not in grafted_content


async def test_claim_multiple_chat_inbounds_all_appended_in_fifo_order(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """multiple chat inbounds in same batch → all appended in FIFO order by created_at (none lost)."""
    tid = spawn_agent()
    insert_inbound_message(db_conn, tid, "first", source="user")
    insert_inbound_message(db_conn, tid, "second", source="agent:5")
    insert_inbound_message(db_conn, tid, "third", source="user")

    cmd = await claim_node(
        AgentState(),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
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
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """chat inbound uses two-phase commit (since 2026-05-27): claim UPDATE pending → claimed;
    a subsequent startup reconcile will move claimed → done; if the process dies midway,
    claimed rows will be reset back to pending by the new process for re-delivery."""
    tid = spawn_agent()
    iid = insert_inbound_message(db_conn, tid, "msg", source="user")

    await claim_node(
        AgentState(),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM inbound_messages WHERE id = %s", (iid,))
        status = cur.fetchone()[0]  # type: ignore[index]
    assert status == "claimed"


async def test_claim_short_path_does_not_enter_idling(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, running_agent: Callable[[], int]
) -> None:
    """first SELECT already has inbound → does not enter wait branch, status not switched to idling.

    Anti-regression: if someone changes to 'unconditionally mark idling then mark running',
    it would wrongly touch the caller's expected state machine; this test verifies that the
    non-wait path does NOT enter IDLING — by starting status 'running' and still 'running'
    after completion (not idling).
    """
    tid = running_agent()
    insert_inbound_message(db_conn, tid, "preexisting", source="user")

    cmd = await claim_node(
        AgentState(),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    assert isinstance(cmd, Command)
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        row = cur.fetchone()
    # short path: did not enter _wait_for_batch, no mark idling/running switch, status remains 'running'
    assert row is not None and row[0] == "running"


async def test_claim_chat_publishes_inbound_committed_per_id(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
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
        _make_runtime(ops_pool=aops_pool, event_publisher=pub),
        _config(
            tid,
        ),
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
    running_agent: Callable[[], int],
    kind: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
) -> None:
    """lifecycle kind (terminate / restart_completed / resurrect) does **not** publish
    InboundCommitted — they are lifecycle markers not user conversations; frontend does not
    depend on reload trigger (timeline renders lifecycle HumanMessage via system_marker, not
    part of the inbound_chat anchor sequence).

    The restart test below (claim does not append message nor publish)."""
    tid = running_agent()
    _insert_inbound_kind(db_conn, tid, "", kind, source="user")

    pub = MagicMock()
    await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool, event_publisher=pub),
        _config(
            tid,
        ),
    )
    assert _committed_publishes(pub) == []


async def test_claim_restart_kind_does_not_publish_committed(
    running_agent: Callable[[], int],
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """restart kind does not publish InboundCommitted (appends no message and no chat
    inbound id enters committed list)."""
    tid = running_agent()
    _set_agent_status(db_conn, tid, "running")
    _insert_inbound_kind(db_conn, tid, "", "restart", source="user")

    pub = MagicMock()
    await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool, event_publisher=pub),
        _config(
            tid,
        ),
    )
    assert _committed_publishes(pub) == []


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


async def test_claim_mixed_batch_publishes_only_chat_ids(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
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
        _make_runtime(ops_pool=aops_pool, event_publisher=pub),
        _config(
            tid,
        ),
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
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, running_agent: Callable[[], int]
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
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )
    assert isinstance(cmd, Command)
    assert cmd.goto == "before_llm"
    assert cmd.update["halted"] is False  # type: ignore[index]
    # key: messages should not be injected with anything (multi-step continue does not touch messages)
    assert cmd.update.get("messages") in ([], None) or "messages" not in (cmd.update or {})  # type: ignore[union-attr]
    # status should remain running (did not enter _wait_for_batch to switch to idling)
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        row = cur.fetchone()
    assert row is not None and row[0] == "running"


# ───────────── _claim_node_impl: dispatch details ─────────────


async def test_claim_terminate_external_source_renders_source_verbatim(
    running_agent: Callable[[], int], db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """source='agent:42' (another agent triggering terminate) → marker uses 'by agent:42'
    as-is, does not go through _by_who's self special case."""
    tid = running_agent()
    _insert_inbound_kind(db_conn, tid, "", "terminate", source="agent:42")
    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )
    assert cmd.goto == END
    msgs = cmd.update["messages"]  # type: ignore[index]
    assert isinstance(msgs[0].content, str)  # pyright: ignore[reportUnknownMemberType]
    assert "Termination was accepted from agent:42" in msgs[0].content
    # anti-regression: must not contain 'yourself' (mutation that changed != to == made all sources go through self)
    assert "yourself" not in msgs[0].content


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


async def test_claim_chat_only_publishes_chat_id_not_lifecycle_in_mixed_batch(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
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
        _make_runtime(ops_pool=aops_pool, event_publisher=pub),
        _config(
            tid,
        ),
    )

    committed = _committed_publishes(pub)
    assert len(committed) == 1, f"should emit only 1 InboundCommitted (chat), got {len(committed)}"  # pyright: ignore[reportUnknownArgumentType]
    assert committed[0]["inbound_id"] == chat_id


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


async def test_claim_chat_message_carries_source_metadata(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """chat dispatch passes source=item.source to inbound_message helper, cannot be None.

    Lock down mutant_76: `source=item.source` → `source=None`. Verify message's
    additional_kwargs.ava_source equals original item.source ('user'), preventing None leak."""
    tid = spawn_agent()
    insert_inbound_message(db_conn, tid, "hello", source="user")

    cmd = await claim_node(
        AgentState(),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )
    msgs = cmd.update["messages"]  # type: ignore[index]
    chat_msg = msgs[0]
    assert isinstance(chat_msg, HumanMessage)
    assert chat_msg.additional_kwargs.get("ava_source") == "user"  # pyright: ignore[reportUnknownMemberType]


async def test_claim_restart_completed_with_payload_overlay_preserves_args(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
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
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
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
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """`claim_node` is a thin wrapper around `node_lifecycle` enter/exit, **must** return the
    inner `_claim_node_impl`'s Command (cannot drop / change goto / wrap into something else).
    Lock down mutation that removes `await` / `return` from `return await _claim_node_impl(...)`."""
    tid = spawn_agent()
    insert_inbound_message(db_conn, tid, "wrapper test", source="user")

    cmd = await claim_node(
        AgentState(),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
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


async def test_claim_restart_self_source_sets_update_initiated(
    running_agent: Callable[[], int], db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """self source RESTART → update_initiated stays True, writing 'this agent
    is in an update session' into Checkpoint (historical: only the removed
    self:update path set it; self.restart preserves whatever was there)."""
    tid = running_agent()
    _set_agent_status(db_conn, tid, "running")
    _insert_inbound_kind(db_conn, tid, "", "restart", source="self")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    assert cmd.goto == END
    assert cmd.update["update_initiated"] is True  # type: ignore[index]


async def test_claim_restart_completed_system_update_clears_update_initiated(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """system:update RESTART_COMPLETED with current update_initiated=True → return
    update_initiated becomes False, update session ends."""
    tid = spawn_agent()
    _set_agent_status(db_conn, tid, "running")
    _insert_inbound_kind(db_conn, tid, "", "restart_completed", source="system:update")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], update_initiated=True),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    assert cmd.goto == "before_llm"
    assert cmd.update["update_initiated"] is False  # type: ignore[index]


async def test_claim_restart_completed_non_system_update_preserves_update_initiated(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """non-system:update RESTART_COMPLETED → update_initiated unchanged, flag not cleared."""
    tid = spawn_agent()
    _set_agent_status(db_conn, tid, "running")
    _insert_inbound_kind(db_conn, tid, "", "restart_completed", source="self")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], update_initiated=True),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    assert cmd.goto == "before_llm"
    assert cmd.update["update_initiated"] is True  # type: ignore[index]


async def test_claim_node_idle_enter_publishes_full_window_snapshot(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
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
        _config(
            tid,
        ),
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
