"""The claim node's restart, restart-completed and system-note inbounds and their serial order with terminate."""

from collections.abc import Callable
from typing import cast
from unittest.mock import MagicMock

import psycopg
import pytest
from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage
from langgraph.graph import END
from psycopg_pool import AsyncConnectionPool

from agent.graph import claim_node
from agent.graph.tests.cursor_fixture import _fresh_snapshot_cursor as _fresh_snapshot_cursor
from agent.state import AgentState
from agent.tests.claim.claim_status_support import (
    _await_status,
    _committed_publishes,
    _set_agent_status,
)
from agent.tests.claim.claim_status_support import running_agent as running_agent
from agent.tests.claim.claim_support import (
    _await_inbound_visible,
    _config,
    _insert_inbound_kind,
    _make_runtime,
)
from base.config.service_read import ConfigAuthority
from base.db import Database, insert_inbound_message
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from tests.fixtures.units import spawn_agent


async def test_claim_restart_completed_kind_appends_marker_and_continues(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
):
    """Historical restart_completed inbound → claim appends
    lifecycle marker 'You have been restarted by {source}' + goto BEFORE_LLM.
    halted=False (mid-task before restart) → wakes up to resume interrupted work."""
    tid = spawn_agent(
        catalog=model_catalog, authority=config_authority, database_gate=database_gate
    )
    _insert_inbound_kind(db_conn, tid, "", "restart_completed", source="user")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool, database_gate=database_gate),
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
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
):
    """system_note inbound (task assign/update/reminder delivery) → claim appends
    a system note (NoteTag 'task') + goto BEFORE_LLM — never a chat peer message."""
    tid = spawn_agent(
        catalog=model_catalog, authority=config_authority, database_gate=database_gate
    )
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
        _make_runtime(ops_pool=aops_pool, database_gate=database_gate),
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
    # The inbound row is consumed (done at claim, like other lifecycle kinds).
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM inbound_messages WHERE id = %s", (inbound_id,))
        status = cur.fetchone()
        assert status is not None and status[0] == "done"


async def test_claim_co_batched_task_notes_preserve_task_links(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
):
    """Co-batched task notes retain their independent timeline links."""
    tid = spawn_agent(
        catalog=model_catalog, authority=config_authority, database_gate=database_gate
    )
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
        _make_runtime(ops_pool=aops_pool, database_gate=database_gate),
        _config(
            tid,
        ),
    )

    assert cmd.goto == "before_llm"
    assert cmd.update is not None
    notes = cast(list[AnyMessage], cmd.update["messages"])
    assert [note.additional_kwargs.get("ava_task_id") for note in notes] == [1, 2]


async def test_claim_system_note_unknown_tag_fails_loud(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
):
    """A system_note inbound carrying a non-NoteTag payload fails loud — a
    writer bug must not silently render as the wrong timeline chip."""
    tid = spawn_agent(
        catalog=model_catalog, authority=config_authority, database_gate=database_gate
    )
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
            _make_runtime(ops_pool=aops_pool, database_gate=database_gate),
            _config(
                tid,
            ),
        )


async def test_claim_restart_while_idle_commits_halted_true(
    running_agent: Callable[[], int],
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    database_gate: ProcessDbGate,
):
    """external restart hits idle agent (halted=True) → committed halted=True,
    carrying 'no in-flight work before restart' across the restart boundary for the new process to read."""
    tid = running_agent()
    _set_agent_status(db_conn, tid, "running")
    _insert_inbound_kind(db_conn, tid, "", "restart", source="system:update")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        _make_runtime(ops_pool=aops_pool, database_gate=database_gate),
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
    *,
    database_gate: ProcessDbGate,
):
    """agent-initiated restart (ava.self.restart) → even if the exec path
    set halted to True (turn-end semantics), committed halted must be False — agent has in-flight
    intent, must wake after restart to confirm result and continue."""
    tid = running_agent()
    _set_agent_status(db_conn, tid, "running")
    _insert_inbound_kind(db_conn, tid, "", "restart", source=source)

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        _make_runtime(ops_pool=aops_pool, database_gate=database_gate),
        _config(
            tid,
        ),
    )

    assert cmd.goto == END
    assert cmd.update["halted"] is False  # type: ignore[index]


async def test_claim_restart_system_update_after_self_update_wakes(
    running_agent: Callable[[], int],
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    database_gate: ProcessDbGate,
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
        _make_runtime(ops_pool=aops_pool, database_gate=database_gate),
        _config(
            tid,
        ),
    )

    assert cmd.goto == END
    assert cmd.update["halted"] is False  # type: ignore[index]


async def test_claim_restart_preserves_chat_for_successor(
    running_agent: Callable[[], int],
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
):
    """Only the accepted lifecycle command dispatches; chat remains durable pending work."""
    tid = running_agent()
    _set_agent_status(db_conn, tid, "running")
    insert_inbound_message(db_conn, tid, "hello", source="user", bus=event_bus, database=database)
    _insert_inbound_kind(db_conn, tid, "", "restart", source="system:update")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        _make_runtime(ops_pool=aops_pool, database_gate=database_gate),
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


@pytest.mark.flaky  # poll _await_status for claim_node status transition
async def test_claim_second_restart_batched_with_restart_completed_exits_again(
    running_agent: Callable[[], int],
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    database_gate: ProcessDbGate,
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
        _make_runtime(ops_pool=aops_pool, database_gate=database_gate),
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


async def test_claim_restart_completed_while_idle_stays_silent(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
):
    """halted=True (idle before restart, preserved from RESTART case) + batch has only
    restart_completed → after committing lifecycle marker, goto CLAIM returns to waiting,
    does **not** enter before_llm — idle agent does not burn an LLM call for 'knowing I was restarted'."""
    tid = spawn_agent(
        catalog=model_catalog, authority=config_authority, database_gate=database_gate
    )
    _insert_inbound_kind(db_conn, tid, "", "restart_completed", source="system:update")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        _make_runtime(ops_pool=aops_pool, database_gate=database_gate),
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
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
):
    """halted=True but batch has chat in addition to restart_completed (user message that arrived
    during agent downtime window) → wakes normally to before_llm, must not silently swallow the chat."""
    tid = spawn_agent(
        catalog=model_catalog, authority=config_authority, database_gate=database_gate
    )
    _insert_inbound_kind(db_conn, tid, "", "restart_completed", source="system:update")
    insert_inbound_message(
        db_conn, tid, "are you back?", source="user", bus=event_bus, database=database
    )

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        _make_runtime(ops_pool=aops_pool, database_gate=database_gate),
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


async def test_claim_restart_kind_does_not_publish_committed(
    running_agent: Callable[[], int],
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_gate: ProcessDbGate,
) -> None:
    """restart kind does not publish InboundCommitted (appends no message and no chat
    inbound id enters committed list)."""
    tid = running_agent()
    _set_agent_status(db_conn, tid, "running")
    _insert_inbound_kind(db_conn, tid, "", "restart", source="user")

    pub = MagicMock()
    await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        _make_runtime(ops_pool=aops_pool, event_publisher=pub, database_gate=database_gate),
        _config(
            tid,
        ),
    )
    assert _committed_publishes(pub) == []


async def test_claim_restart_completed_with_payload_overlay_preserves_args(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
):
    """restart_completed inbound contains payload → claim passes both source + payload arguments
    to _render_restart_completed_marker; source determines the marker wording, payload determines
    'with config {...}' segment.

    Lock down mutation that replaces source / payload args with None or skips them: losing source
    misses the 'restarted by' wording, losing payload misses overlay diff segment."""
    tid = spawn_agent(
        catalog=model_catalog, authority=config_authority, database_gate=database_gate
    )
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
        _make_runtime(ops_pool=aops_pool, database_gate=database_gate),
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


async def test_claim_restart_self_source_sets_update_initiated(
    running_agent: Callable[[], int],
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    database_gate: ProcessDbGate,
):
    """self source RESTART → update_initiated stays True, writing 'this agent
    is in an update session' into Checkpoint (historical: only the removed
    self:update path set it; self.restart preserves whatever was there)."""
    tid = running_agent()
    _set_agent_status(db_conn, tid, "running")
    _insert_inbound_kind(db_conn, tid, "", "restart", source="self")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        _make_runtime(ops_pool=aops_pool, database_gate=database_gate),
        _config(
            tid,
        ),
    )

    assert cmd.goto == END
    assert cmd.update["update_initiated"] is True  # type: ignore[index]


async def test_claim_restart_completed_system_update_clears_update_initiated(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
):
    """system:update RESTART_COMPLETED with current update_initiated=True → return
    update_initiated becomes False, update session ends."""
    tid = spawn_agent(
        catalog=model_catalog, authority=config_authority, database_gate=database_gate
    )
    _set_agent_status(db_conn, tid, "running")
    _insert_inbound_kind(db_conn, tid, "", "restart_completed", source="system:update")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], update_initiated=True),
        _make_runtime(ops_pool=aops_pool, database_gate=database_gate),
        _config(
            tid,
        ),
    )

    assert cmd.goto == "before_llm"
    assert cmd.update["update_initiated"] is False  # type: ignore[index]


async def test_claim_restart_completed_non_system_update_preserves_update_initiated(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
):
    """non-system:update RESTART_COMPLETED → update_initiated unchanged, flag not cleared."""
    tid = spawn_agent(
        catalog=model_catalog, authority=config_authority, database_gate=database_gate
    )
    _set_agent_status(db_conn, tid, "running")
    _insert_inbound_kind(db_conn, tid, "", "restart_completed", source="self")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], update_initiated=True),
        _make_runtime(ops_pool=aops_pool, database_gate=database_gate),
        _config(
            tid,
        ),
    )

    assert cmd.goto == "before_llm"
    assert cmd.update["update_initiated"] is True  # type: ignore[index]
