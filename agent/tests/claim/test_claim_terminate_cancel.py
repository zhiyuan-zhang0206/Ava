"""The claim node's terminate and cancel inbounds, the hosted turn boundary and the chat a terminate keeps or vetoes."""

from collections.abc import Callable
from dataclasses import replace
from unittest.mock import MagicMock

import psycopg
import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent.graph import claim_node
from agent.graph.tests.cursor_fixture import _fresh_snapshot_cursor as _fresh_snapshot_cursor
from agent.state import AgentState
from agent.tests.claim.claim_status_support import _await_status, _set_agent_status
from agent.tests.claim.claim_status_support import running_agent as running_agent
from agent.tests.claim.claim_support import (
    _await_inbound_visible,
    _config,
    _insert_inbound_kind,
    _make_runtime,
)
from base.config import settings
from base.db import Database, insert_inbound_message
from base.events.live.bus import EventBus
from tests.fixtures.units import spawn_agent


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
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
):
    """Hosted mode changes only the empty-batch branch. When the first SELECT
    finds work, dispatch is byte-for-byte the process path — the turn runs, and
    `turn_idle` is NOT set (the host must re-invoke, not end the task)."""
    tid = spawn_agent()
    insert_inbound_message(
        db_conn, tid, "hello", kind="chat", source="user", bus=event_bus, database=database
    )

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
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
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
    insert_inbound_message(
        db_conn, tid, "please also do X", source="user", bus=event_bus, database=database
    )
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
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
):
    """Same as above but the cancel is the OLDER row (user clicks Stop, then
    sends a new message while both are still pending). The wake decision is
    order-independent — a chat anywhere in the cancel batch means new intent to
    process, so goto=before_llm + halted=False regardless of insertion order."""
    tid = spawn_agent()
    # cancel first (older id), then chat
    _insert_inbound_kind(db_conn, tid, "", "cancel", source="user")
    chat_id = insert_inbound_message(
        db_conn, tid, "new instruction", source="user", bus=event_bus, database=database
    )
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
    running_agent: Callable[[], int],
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
):
    """Accepting self termination never acknowledges an unseen peer message."""
    tid = running_agent()
    insert_inbound_message(
        db_conn,
        tid,
        "peer message during suicide",
        source="agent:1",
        bus=event_bus,
        database=database,
    )
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
    running_agent: Callable[[], int],
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
):
    """An older user chat remains durable without vetoing the accepted command."""
    tid = running_agent()
    chat_id = insert_inbound_message(
        db_conn, tid, "queued before the suicide", source="user", bus=event_bus, database=database
    )
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
    running_agent: Callable[[], int],
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
):
    """Newer chat does not replace an accepted lifecycle command or get lost."""
    tid = running_agent()
    _insert_inbound_kind(db_conn, tid, "", "terminate", source="user")
    chat_id = insert_inbound_message(
        db_conn, tid, "message after the kill", source="user", bus=event_bus, database=database
    )
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
    running_agent: Callable[[], int],
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
):
    """Regression guard: a deliberate external kill is NOT vetoed by chats that
    predate it — the actor decided with the pending queue visible, so the
    terminate is the latest intent and the agent dies (END + marker). The
    pre-death chat is committed to history as before; it is visible after a
    resurrect."""
    tid = running_agent()
    chat_id = insert_inbound_message(
        db_conn, tid, "old message before the kill", source="user", bus=event_bus, database=database
    )
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


async def test_claim_restart_kind_hosted_ends_turn_and_stays_runnable(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """Hosted restart: goto END with `restart_requested` (not `exit_requested`),
    leaves lifecycle application to the host after the acceptance checkpoint
    has been flushed."""
    from agent.tests.claim.test_inbound_ownership import _admit, _agent

    tid = _agent(db_conn)
    owner = await _admit(aops_pool, tid)
    restart_id = _insert_inbound_kind(db_conn, tid, "", "restart", source="user")
    await _await_inbound_visible(aops_pool, restart_id)

    runtime = _make_runtime(ops_pool=aops_pool)
    runtime = Runtime(context=replace(runtime.context, original_incarnation=owner))
    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        runtime,
        _config(tid),
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
