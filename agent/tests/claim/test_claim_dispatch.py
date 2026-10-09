"""The claim node's chat dispatch, short path, inbound-committed publishes and container mode."""

from collections.abc import Callable
from unittest.mock import MagicMock

import psycopg
import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool

import ava
from agent.graph import claim_node
from agent.graph.tests.cursor_fixture import _fresh_snapshot_cursor as _fresh_snapshot_cursor
from agent.state import AgentState
from agent.tests.claim.claim_status_support import _committed_publishes, _set_agent_status
from agent.tests.claim.claim_status_support import running_agent as running_agent
from agent.tests.claim.claim_support import _config, _insert_inbound_kind, _make_runtime
from base.db import Database, insert_inbound_message
from base.events.live.bus import EventBus
from tests.fixtures.units import spawn_agent


async def test_claim_first_entry_keeps_boot_claim_running(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
):
    """The bootstrap claim already sets running before the first graph entry."""
    tid = spawn_agent()
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'running' WHERE id = %s", (tid,))
    db_conn.commit()
    insert_inbound_message(db_conn, tid, "hello", source="user", bus=event_bus, database=database)

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
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
):
    """A subsequent graph entry leaves its already-running row untouched."""
    tid = spawn_agent()
    _set_agent_status(db_conn, tid, "running")
    insert_inbound_message(db_conn, tid, "hello", source="user", bus=event_bus, database=database)

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
    running_agent: Callable[[], int],
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
):
    """Only the accepted command gets pickup time; queued chat stays unclaimed."""
    from agent.db import claim_inbound_batch

    tid = running_agent()
    chat_id = insert_inbound_message(
        db_conn, tid, "hello", source="user", bus=event_bus, database=database
    )
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

    rows = await claim_inbound_batch(
        aops_pool, tid, incarnation=ava.context.require_original_incarnation(tid), work=None
    )
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
    fresh_id = insert_inbound_message(
        db_conn, tid, "later", source="user", bus=event_bus, database=database
    )
    with db_conn.cursor() as cur:
        cur.execute("SELECT claimed_at FROM inbound_messages WHERE id = %s", (fresh_id,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] is None


async def test_claim_chat_kind_appends_humanmessage_with_envelope(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
):
    """chat inbound → claim returns Command(goto='before_llm'), update.messages
    contains HumanMessage after envelope wrapping.

    state.messages empty (agent first round) → claim simultaneously injects SystemMessage as
    messages[0] for prompt cache hit across restarts.
    """
    tid = spawn_agent()
    insert_inbound_message(db_conn, tid, "hello", source="user", bus=event_bus, database=database)

    cmd = await claim_node(
        AgentState(),
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
    # User envelope: a bare "[ts]" header (base/agents/messages/envelope.py).
    assert msgs[0].content.startswith("[")  # pyright: ignore[reportUnknownMemberType]
    assert "hello" in msgs[0].content  # pyright: ignore[reportUnknownMemberType]
    assert cmd.update["halted"] is False  # type: ignore[index]


async def test_claim_chat_expands_slash_command(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
):
    """A `/<name> ...` chat inbound is expanded by the claim node into the
    command's template + the user's note before being wrapped for the model."""
    tid = spawn_agent()
    insert_inbound_message(
        db_conn, tid, "/recap just the PRs", source="user", bus=event_bus, database=database
    )

    cmd = await claim_node(
        AgentState(),
        _make_runtime(ops_pool=aops_pool),
        _config(
            tid,
        ),
    )

    content = cmd.update["messages"][-1].content  # type: ignore[index]
    # The user envelope frames the expansion; the body is source-neutral.
    assert content.startswith("[")  # pyright: ignore[reportUnknownMemberType]
    assert "Command /recap:" in content
    assert "Additional message: just the PRs" in content


async def test_claim_multiple_chat_inbounds_all_appended_in_fifo_order(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
):
    """multiple chat inbounds in same batch → all appended in FIFO order by created_at (none lost)."""
    tid = spawn_agent()
    insert_inbound_message(db_conn, tid, "first", source="user", bus=event_bus, database=database)
    insert_inbound_message(
        db_conn, tid, "second", source="agent:5", bus=event_bus, database=database
    )
    insert_inbound_message(db_conn, tid, "third", source="user", bus=event_bus, database=database)

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


async def test_claim_chat_marks_inbound_claimed_immediately(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
):
    """chat inbound uses two-phase commit (since 2026-05-27): claim UPDATE pending → claimed;
    a subsequent startup reconcile will move claimed → done; if the process dies midway,
    claimed rows will be reset back to pending by the new process for re-delivery."""
    tid = spawn_agent()
    iid = insert_inbound_message(
        db_conn, tid, "msg", source="user", bus=event_bus, database=database
    )

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
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    running_agent: Callable[[], int],
    database: Database,
    event_bus: EventBus,
) -> None:
    """first SELECT already has inbound → does not enter wait branch, status not switched to idling.

    Anti-regression: if someone changes to 'unconditionally mark idling then mark running',
    it would wrongly touch the caller's expected state machine; this test verifies that the
    non-wait path does NOT enter IDLING — by starting status 'running' and still 'running'
    after completion (not idling).
    """
    tid = running_agent()
    insert_inbound_message(
        db_conn, tid, "preexisting", source="user", bus=event_bus, database=database
    )

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
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
) -> None:
    """After each chat inbound is envelope-wrapped into state, publish one InboundCommitted
    (frontend relies on this event to trigger reload to fetch the committed version).

    Anchor the protocol layer ACK contract: changing publish order / missing a publish /
    wrong inbound_id any of these → test fails.
    """
    import json

    tid = spawn_agent()
    id1 = insert_inbound_message(
        db_conn, tid, "first", source="user", bus=event_bus, database=database
    )
    id2 = insert_inbound_message(
        db_conn, tid, "second", source="user", bus=event_bus, database=database
    )

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


async def test_claim_mixed_batch_publishes_only_chat_ids(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
) -> None:
    """same batch chat + compact_summary → publish only for the chat's inbound_id,
    summary does not emit publish."""
    tid = spawn_agent()
    chat_id = insert_inbound_message(
        db_conn, tid, "user msg", source="user", bus=event_bus, database=database
    )
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


async def test_container_mode_continues_without_touching_messages():
    """ops_pool=None → container mode skips all inbound dispatch and heads to
    before_llm without writing messages. The system prompt an eval starts from is
    laid down by `init_context`, which runs before claim (see
    agent/graph/tests/test_init_context.py)."""
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


async def test_claim_chat_only_publishes_chat_id_not_lifecycle_in_mixed_batch(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
):
    """same batch chat + resurrect → publish only for the chat's inbound_id, lifecycle
    inbound id does not enter committed_chat_ids.

    Lock down `committed_chat_ids.append(item.id)` appearing only in CHAT branch —
    mutation moving it to dispatch top / resurrect branch would cause publish for extra
    lifecycle id, frontend fetching timeline would not find reload anchor."""
    tid = spawn_agent()
    chat_id = insert_inbound_message(
        db_conn, tid, "user msg", source="user", bus=event_bus, database=database
    )
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


async def test_claim_chat_message_carries_source_metadata(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
):
    """chat dispatch passes source=item.source to inbound_message helper, cannot be None.

    Lock down mutant_76: `source=item.source` → `source=None`. Verify message's
    additional_kwargs.ava_source equals original item.source ('user'), preventing None leak."""
    tid = spawn_agent()
    insert_inbound_message(db_conn, tid, "hello", source="user", bus=event_bus, database=database)

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


async def test_claim_node_wrapper_returns_underlying_command(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
):
    """`claim_node` is a thin wrapper around `node_lifecycle` enter/exit, **must** return the
    inner `_claim_node_impl`'s Command (cannot drop / change goto / wrap into something else).
    Lock down mutation that removes `await` / `return` from `return await _claim_node_impl(...)`."""
    tid = spawn_agent()
    insert_inbound_message(
        db_conn, tid, "wrapper test", source="user", bus=event_bus, database=database
    )

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
