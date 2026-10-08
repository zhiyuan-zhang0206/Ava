"""Compact cases: compact reminder fires in band."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import psycopg
import pytest
from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent.graph.claim.node import BEFORE_LLM, END, claim_node
from agent.hooks.compact import COMPACTION_INSTRUCTION, compose_summary_message, generate_summary
from agent.messages import inbound_message
from agent.state import AgentState
from agent.tests.test_compact import (
    _COMPACT_SECTIONS,
    _LONG_SUMMARY,
    _compact_tail,
    _compaction_ainvoke,
    _config,
    _fake_config,
    _fake_llm,
    _insert_compact_summary,
    _make_runtime,
    _patch_compact_config,
    _reminder_state,
    _runtime_with_llm,
)
from agent.tests.test_compact import (
    _ava_compact_loaded as _ava_compact_loaded,
)
from base.agents.context import AvaContext
from base.db import Database
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.packages.plugins.extensions import EMPTY
from tests.fixtures.units import spawn_agent


async def test_compact_reminder_fires_in_band(_ava_compact_loaded, monkeypatch: pytest.MonkeyPatch):
    """reminder_tokens < est <= ceiling, fresh window, no agent inbound →
    inject the qualitative system_note + mark reminder_shown; not a force
    (compact.version unchanged, no history replacement)."""
    from agent.hooks import compact as _p

    state_cls, wrap_fn = _ava_compact_loaded
    _patch_compact_config(monkeypatch, compact_reminder_tokens=1, auto_compact_tokens=1_000_000)
    result = await wrap_fn(
        _reminder_state(state_cls),  # pyright: ignore[reportUnknownArgumentType]
        _runtime_with_llm(_fake_llm()),
        _fake_config(),
    )

    assert result is not None
    note = result["messages"][0]
    assert note.additional_kwargs["ava_msg_type"] == "system_note"  # pyright: ignore[reportUnknownMemberType]
    assert note.additional_kwargs["ava_note_tag"] == "compact_reminder"  # pyright: ignore[reportUnknownMemberType]
    assert note.content == f"[system] {_p.COMPACT_REMINDER_NOTE}"  # pyright: ignore[reportUnknownMemberType]
    assert result["compact"].reminder_shown is True  # pyright: ignore[reportUnknownMemberType]
    assert (
        result["compact"].version == 0  # pyright: ignore[reportUnknownMemberType]
    )  # reminder != force: version untouched


async def test_compact_reminder_silent_below_threshold(
    _ava_compact_loaded, monkeypatch: pytest.MonkeyPatch
):
    """est <= reminder_tokens → no note (and no force, est under ceiling)."""
    state_cls, wrap_fn = _ava_compact_loaded
    _patch_compact_config(
        monkeypatch, compact_reminder_tokens=1_000_000, auto_compact_tokens=2_000_000
    )
    result = await wrap_fn(
        _reminder_state(state_cls),  # pyright: ignore[reportUnknownArgumentType]
        _runtime_with_llm(_fake_llm()),
        _fake_config(),
    )
    assert result is None


async def test_compact_reminder_yields_to_force_above_ceiling(
    _ava_compact_loaded, monkeypatch: pytest.MonkeyPatch
):
    """Above the ceiling, defer all model work and history changes to the LLM node."""
    state_cls, wrap_fn = _ava_compact_loaded
    _patch_compact_config(monkeypatch, compact_reminder_tokens=0, auto_compact_tokens=1)
    result = await wrap_fn(
        _reminder_state(state_cls),  # pyright: ignore[reportUnknownArgumentType]
        _runtime_with_llm(_fake_llm(_LONG_SUMMARY)),
        _fake_config(),
    )

    assert result is None  # The hook never calls the model; the LLM node owns compaction.


async def test_compact_reminder_once_per_window(
    _ava_compact_loaded, monkeypatch: pytest.MonkeyPatch
):
    """already reminded this window (shown=True, no compaction since) → silent."""
    state_cls, wrap_fn = _ava_compact_loaded
    _patch_compact_config(monkeypatch, compact_reminder_tokens=1, auto_compact_tokens=1_000_000)
    state = _reminder_state(state_cls, version=0, shown=True, seen=0)  # pyright: ignore[reportUnknownArgumentType]
    result = await wrap_fn(state, _runtime_with_llm(_fake_llm()), _fake_config())
    assert result is None


async def test_compact_reminder_rearms_after_compaction(
    _ava_compact_loaded, monkeypatch: pytest.MonkeyPatch
):
    """shown=True but a compaction advanced compact.version past the bookmark →
    the old note was summarized away, so the reminder re-arms and fires again,
    catching the bookmark up to the new version."""
    state_cls, wrap_fn = _ava_compact_loaded
    _patch_compact_config(monkeypatch, compact_reminder_tokens=1, auto_compact_tokens=1_000_000)
    state = _reminder_state(state_cls, version=1, shown=True, seen=0)  # pyright: ignore[reportUnknownArgumentType]
    result = await wrap_fn(state, _runtime_with_llm(_fake_llm()), _fake_config())

    assert result is not None
    assert result["compact"].reminder_shown is True  # pyright: ignore[reportUnknownMemberType]
    assert (
        result["compact"].reminder_seen_version == 1  # pyright: ignore[reportUnknownMemberType]
    )  # bookmark caught up


async def test_compact_reminder_defers_to_agent_reply(
    _ava_compact_loaded, monkeypatch: pytest.MonkeyPatch
):
    """in band, but the turn was woken by an agent inbound → defer (return None)
    so the agent-reply note owns the single messages-write this pass allows."""
    state_cls, wrap_fn = _ava_compact_loaded
    _patch_compact_config(monkeypatch, compact_reminder_tokens=1, auto_compact_tokens=1_000_000)
    msgs = [
        SystemMessage(content="<sys>"),
        *(HumanMessage(content="x" * 1000) for _ in range(5)),
        inbound_message(content="ping", source="agent:5", inbound_id=1),
    ]
    state = _reminder_state(state_cls, messages=msgs)  # pyright: ignore[reportUnknownArgumentType]
    result = await wrap_fn(state, _runtime_with_llm(_fake_llm()), _fake_config())
    assert result is None


async def test_compact_reminder_silent_when_no_conversation(
    _ava_compact_loaded, monkeypatch: pytest.MonkeyPatch
):
    """est over the reminder threshold but only a SystemMessage (nothing to
    compact) → no point reminding, return None."""
    state_cls, wrap_fn = _ava_compact_loaded
    _patch_compact_config(monkeypatch, compact_reminder_tokens=1, auto_compact_tokens=1_000_000)
    state = _reminder_state(state_cls, messages=[SystemMessage(content="x" * 100_000)])  # pyright: ignore[reportUnknownArgumentType]
    result = await wrap_fn(state, _runtime_with_llm(_fake_llm()), _fake_config())
    assert result is None


async def test_compact_contract_reaches_request_without_resident_self(
    _ava_compact_loaded, monkeypatch: pytest.MonkeyPatch
):
    """The one compaction contract — the summary's sections + how to write it —
    lives in the SDK docstring and reaches the forced request even when the
    standing P95 reference omits `self`. The cached conversation prefix stays
    unchanged; the contract is disclosed in the final instruction."""
    from agent.graph.prompt.system_prompt import build_system_prompt
    from ava.self import compact
    from base.config import settings

    monkeypatch.setattr(
        settings.agent, "sdk_expand_in_system_prompt", ["shell", "files", "agents", "tasks"]
    )

    contract = compact.__doc__
    assert contract is not None
    for section in _COMPACT_SECTIONS:
        assert section in contract, f"section {section!r} missing from the compact contract"

    system_prompt = build_system_prompt(EMPTY, AgentSlices.resolve(), agent_id=1)
    assert "## ava.self\n" not in system_prompt
    head = SystemMessage(content=system_prompt)
    llm = _fake_llm()
    await generate_summary([head, HumanMessage(content="Keep my work")], llm, AgentSlices.resolve())
    [call] = _compaction_ainvoke(llm).call_args_list
    [request] = call.args
    assert request[0] is head
    from inspect import cleandoc

    assert request[-1].content.startswith(COMPACTION_INSTRUCTION)
    assert cleandoc(contract) in request[-1].content
    for section in _COMPACT_SECTIONS:
        assert section in request[-1].content, (
            f"section {section!r} missing from compaction request"
        )


async def test_compact_summary_preserves_agent_continuity(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """After processing compact_summary the batch resumes at BEFORE_LLM (not END) —
    the agent continues its conversation rather than being terminated. The goto
    itself is the init_context detour that rebuilds the standing head; where the
    batch was actually headed rides in `context_reset.resume`."""
    tid = spawn_agent()
    _insert_compact_summary(db_conn, tid, "summary after compact")

    sys_msg = SystemMessage(content="<test sys prompt>")
    initial_msgs: list[AnyMessage] = [
        sys_msg,
        *(HumanMessage(content=f"history-{i}") for i in range(8)),
    ]
    state = AgentState(messages=initial_msgs)

    cmd = await claim_node(state, _make_runtime(aops_pool), _config(tid))
    assert cmd.goto == "init_context"
    resume = cmd.update["context_reset"].resume  # type: ignore[index]
    assert resume == BEFORE_LLM, (
        f"after compact should continue conversation (resume=BEFORE_LLM), actual {resume}"
    )


async def test_compact_summary_replaces_whole_history_no_tail(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """compact_summary → the whole history is cleared and the parked tail is the
    summary alone; not a single original message survives (the summary is the
    complete memory, no raw tail)."""
    tid = spawn_agent()
    sys_msg = SystemMessage(content="<test sys prompt>")
    initial_msgs: list[AnyMessage] = [
        sys_msg,
        *(HumanMessage(content=f"history-{i}") for i in range(8)),
    ]
    state = AgentState(messages=initial_msgs)

    _insert_compact_summary(db_conn, tid, "the whole memory")
    cmd = await claim_node(state, _make_runtime(aops_pool), _config(tid))

    tail = _compact_tail(cmd.update)
    assert [m.content for m in tail] == [compose_summary_message("the whole memory")]  # pyright: ignore[reportUnknownMemberType]
    assert cmd.goto == "init_context"


async def test_compact_summary_emits_compact_done(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    loguru_records: list[dict[str, Any]],
):
    """When claim processes compact_summary (agent-written summary), emit CompactDone
    at the same place where history is replaced — so UI refreshes, aligning with auto path (agent/hooks/compact.py).
    User-triggered compact_request goes through the same compact_payload block, emit is path-agnostic."""
    tid = spawn_agent()
    _insert_compact_summary(db_conn, tid, "summary after compact")
    state = AgentState(
        messages=[
            SystemMessage(content="<sys>"),
            *(HumanMessage(content=f"history-{i}") for i in range(4)),
        ]
    )
    # Explicit publisher MagicMock (not via runtime.context, which is Optional)
    # so the emit assertion types cleanly — mirrors the auto-path emit test.
    publisher = MagicMock()
    ctx = AvaContext(
        ops_pool=aops_pool,
        llm=AsyncMock(),
        event_publisher=publisher,
        agent=AgentSlices.resolve(),
        db=Database.from_settings(),
        bus=EventBus.from_settings(),
    )
    runtime = Runtime(context=ctx)

    await claim_node(state, runtime, _config(tid))

    emitted = [call.args[0] for call in publisher.emit.call_args_list]
    assert any('"compact_done"' in e for e in emitted), f"no CompactDone emitted; saw {emitted}"
    [monitoring] = [
        record
        for record in loguru_records
        if record["extra"].get("event") == "compaction_completed"
    ]
    assert monitoring["extra"] | {"msg": None} == {
        "agent_id": tid,
        "compact_kind": "compact_summary",
        "compactions": 1,
        "history_chars": len("history-0") * 4,
        "summary_chars": len("summary after compact"),
        "summary_history_ratio": pytest.approx(  # pyright: ignore[reportUnknownMemberType]
            len("summary after compact") / (len("history-0") * 4)
        ),
        "event": "compaction_completed",
        "msg": None,
    }


async def test_consecutive_compacts_both_processed(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """Two consecutive compact_summary → first replaces with [sys, summary1]; second on that
    state replaces again with [sys, summary2]."""
    tid = spawn_agent()
    sys_msg = SystemMessage(content="<test sys prompt>")

    # ---- first compact ----
    _insert_compact_summary(db_conn, tid, "first compact summary")
    initial_msgs: list[AnyMessage] = [
        sys_msg,
        *(HumanMessage(content=f"gen1-msg-{i}") for i in range(8)),
    ]
    state = AgentState(messages=initial_msgs)

    cmd1 = await claim_node(state, _make_runtime(aops_pool), _config(tid))
    tail1 = _compact_tail(cmd1.update)
    assert tail1[0].content == compose_summary_message("first compact summary")  # pyright: ignore[reportUnknownMemberType]

    # ---- second compact (on state after compact) ----
    # state after first compact (RemoveMessage already processed by reducer) = [sys, summary1]
    compacted_state = AgentState(messages=[sys_msg, HumanMessage(content="first compact summary")])
    _insert_compact_summary(db_conn, tid, "second compact summary")
    cmd2 = await claim_node(compacted_state, _make_runtime(aops_pool), _config(tid))
    tail2 = _compact_tail(cmd2.update)
    assert [m.content for m in tail2] == [compose_summary_message("second compact summary")]  # pyright: ignore[reportUnknownMemberType]
    assert cmd2.update["context_reset"].resume != END  # type: ignore[index]  # can continue


async def test_compact_with_empty_state_injects_system_message_and_summary(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """A compact_summary arriving on an empty window behaves like any other: the
    window is cleared and the summary parked, exactly as when there was history
    to clear. Claim used to lay down a cold-start head here and then pop it back
    off — the head is `init_context`'s now, so there is nothing to undo and the
    two cases stopped differing."""
    tid = spawn_agent()
    _insert_compact_summary(db_conn, tid, "compact before any chat")

    state = AgentState()  # empty messages

    cmd = await claim_node(state, _make_runtime(aops_pool), _config(tid))

    tail = _compact_tail(cmd.update)
    assert [m.content for m in tail] == [compose_summary_message("compact before any chat")]  # pyright: ignore[reportUnknownMemberType]
    assert cmd.goto == "init_context"


async def test_compact_with_super_long_summary_in_claim(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
):
    """claim_node processes compact_summary with super-long summary (50K chars) —
    no truncation, no error thrown."""
    tid = spawn_agent()
    long_summary = "LONG_" * 10_000  # 50K chars

    sys_msg = SystemMessage(content="<test sys prompt>")
    initial_msgs: list[AnyMessage] = [
        sys_msg,
        *(HumanMessage(content=f"m{i}") for i in range(6)),
    ]
    state = AgentState(messages=initial_msgs)

    _insert_compact_summary(db_conn, tid, long_summary)
    cmd = await claim_node(state, _make_runtime(aops_pool), _config(tid))

    tail = _compact_tail(cmd.update)
    assert tail[0].content == compose_summary_message(long_summary)  # pyright: ignore[reportUnknownMemberType]
    # the 50K summary rides through untruncated (only the fixed header is added)
    assert len(tail[0].content) == len(compose_summary_message(long_summary))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert long_summary in tail[0].content  # pyright: ignore[reportUnknownMemberType]


async def test_terminate_preserves_pending_summary_without_wiping_history(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, event_bus: EventBus
):
    """Lifecycle acceptance is serial; a summary cannot run in the exiting owner."""
    from agent.ownership.hosted import apply_hosted_lifecycle
    from agent.tests.claim.test_inbound_ownership import _admit, _agent
    from base.native_process.turn_identity import bind_turn_identity

    tid = _agent(db_conn)
    old = await _admit(aops_pool, tid)
    summary_text = "summary before terminate"
    _insert_compact_summary(db_conn, tid, summary_text)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind) VALUES (%s, '', 'terminate')",
            (tid,),
        )
    db_conn.commit()

    sys_msg = SystemMessage(content="<test sys prompt>")
    initial_msgs: list[AnyMessage] = [
        sys_msg,
        *(HumanMessage(content=f"h-{i}") for i in range(8)),
    ]
    state = AgentState(messages=initial_msgs)

    with bind_turn_identity(tid, incarnation=old):
        cmd = await claim_node(state, _make_runtime(aops_pool), _config(tid))
        await apply_hosted_lifecycle(aops_pool, old, bus=event_bus)

    assert cmd.goto == END
    assert "context_reset" not in cmd.update  # type: ignore[operator]
    assert state.messages == initial_msgs
    assert db_conn.execute(
        "SELECT kind,status,applied_at IS NOT NULL,observed_at IS NOT NULL FROM inbound_messages "
        "WHERE agent_id=%s ORDER BY id",
        (tid,),
    ).fetchall() == [
        ("compact_summary", "pending", False, False),
        ("terminate", "done", True, True),
    ]


async def test_compact_in_same_batch_as_restart(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, event_bus: EventBus
):
    """The admitted successor, not the exiting owner, consumes the same summary."""
    from agent.ownership.hosted import apply_hosted_lifecycle
    from agent.tests.claim.test_inbound_ownership import _admit, _agent
    from base.native_process.turn_identity import bind_turn_identity

    tid = _agent(db_conn)
    old = await _admit(aops_pool, tid)

    summary_text = "summary pre-restart"
    _insert_compact_summary(db_conn, tid, summary_text)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind) VALUES (%s, '', 'restart')",
            (tid,),
        )
    db_conn.commit()

    sys_msg = SystemMessage(content="<test sys prompt>")
    initial_msgs: list[AnyMessage] = [
        sys_msg,
        *(HumanMessage(content=f"h-{i}") for i in range(8)),
    ]
    state = AgentState(messages=initial_msgs)

    with bind_turn_identity(tid, incarnation=old):
        cmd = await claim_node(state, _make_runtime(aops_pool), _config(tid))
        await apply_hosted_lifecycle(aops_pool, old, bus=event_bus)

    assert cmd.goto == END
    assert "context_reset" not in cmd.update  # type: ignore[operator]
    assert state.messages == initial_msgs
    rows = db_conn.execute(
        "SELECT id,kind,status,applied_at IS NOT NULL,observed_at FROM inbound_messages "
        "WHERE agent_id=%s ORDER BY id",
        (tid,),
    ).fetchall()
    assert [row[1:] for row in rows] == [
        ("compact_summary", "pending", False, None),
        ("restart", "claimed", True, None),
    ]
    summary_id, restart_id = rows[0][0], rows[1][0]
    successor = await _admit(aops_pool, tid)
    assert successor.generation != old.generation
    assert db_conn.execute(
        "SELECT status,observed_at IS NOT NULL FROM inbound_messages WHERE id=%s", (restart_id,)
    ).fetchone() == ("done", True)
    with bind_turn_identity(tid, incarnation=successor):
        resumed = await claim_node(state, _make_runtime(aops_pool), _config(tid))
    tail = _compact_tail(resumed.update)
    assert tail[0].content == compose_summary_message(summary_text)  # pyright: ignore[reportUnknownMemberType]
    assert resumed.goto == "init_context"
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (summary_id,)
    ).fetchone() == ("done",)
