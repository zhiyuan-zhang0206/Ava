from __future__ import annotations

from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import psycopg
import pytest
from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent.graph.claim.node import BEFORE_LLM, END, claim_node
from agent.graph.hooks.compact import (
    COMPACTION_INSTRUCTION,
    compose_summary_message,
    generate_summary,
)
from agent.hooks import Hook
from agent.messages import inbound_message
from agent.state import AgentState, CompactState, ContextReset
from agent.tests.test_compact import (
    COMPACT_SECTIONS,
    LONG_SUMMARY,
    _config,
    compact_tail,
    compaction_ainvoke,
    fake_config,
    insert_compact_summary,
    make_fake_llm,
    make_runtime,
    patch_compact_config,
    reminder_state,
    runtime_with_llm,
)
from agent.tests.test_compact import meiva_compact_loaded as meiva_compact_loaded
from base.agents.context import MeivaContext
from base.agents.tests.units import spawn_agent
from base.clock import Clock
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.config.tests.configuration import defaults
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.packages.plugin_providers import build_model_catalog
from base.packages.plugins.extensions import EMPTY


async def test_compact_reminder_fires_in_band(
    meiva_compact_loaded: tuple[type[AgentState], Hook],
    monkeypatch: pytest.MonkeyPatch,
    database_gate: ProcessDbGate,
):
    from agent.graph.hooks import compact as _p

    state_cls, wrap_fn = meiva_compact_loaded
    patch_compact_config(monkeypatch, compact_reminder_tokens=1, auto_compact_tokens=1_000_000)
    result = await wrap_fn(
        reminder_state(state_cls),
        runtime_with_llm(make_fake_llm(), database_gate=database_gate),
        fake_config(),
    )

    assert result is not None
    note = result["messages"][0]
    assert isinstance(note, HumanMessage)
    assert note.additional_kwargs["meiva_msg_type"] == "system_note"
    assert note.additional_kwargs["meiva_note_tag"] == "compact_reminder"
    assert note.text.startswith(f"[system] {_p.COMPACT_REMINDER_NOTE}")
    compact = result["compact"]
    assert isinstance(compact, CompactState)
    assert compact.reminder_shown is True
    assert compact.version == 0


async def test_compact_reminder_silent_below_threshold(
    meiva_compact_loaded: tuple[type[AgentState], Hook],
    monkeypatch: pytest.MonkeyPatch,
    database_gate: ProcessDbGate,
):
    state_cls, wrap_fn = meiva_compact_loaded
    patch_compact_config(
        monkeypatch, compact_reminder_tokens=1_000_000, auto_compact_tokens=2_000_000
    )
    result = await wrap_fn(
        reminder_state(state_cls),
        runtime_with_llm(make_fake_llm(), database_gate=database_gate),
        fake_config(),
    )
    assert result is None


async def test_compact_reminder_yields_to_force_above_ceiling(
    meiva_compact_loaded: tuple[type[AgentState], Hook],
    monkeypatch: pytest.MonkeyPatch,
    database_gate: ProcessDbGate,
):
    state_cls, wrap_fn = meiva_compact_loaded
    patch_compact_config(monkeypatch, compact_reminder_tokens=0, auto_compact_tokens=1)
    result = await wrap_fn(
        reminder_state(state_cls),
        runtime_with_llm(make_fake_llm(LONG_SUMMARY), database_gate=database_gate),
        fake_config(),
    )

    assert result is None


async def test_compact_reminder_once_per_window(
    meiva_compact_loaded: tuple[type[AgentState], Hook],
    monkeypatch: pytest.MonkeyPatch,
    database_gate: ProcessDbGate,
):
    state_cls, wrap_fn = meiva_compact_loaded
    patch_compact_config(monkeypatch, compact_reminder_tokens=1, auto_compact_tokens=1_000_000)
    state = reminder_state(state_cls, version=0, shown=True, seen=0)
    result = await wrap_fn(
        state, runtime_with_llm(make_fake_llm(), database_gate=database_gate), fake_config()
    )
    assert result is None


async def test_compact_reminder_rearms_after_compaction(
    meiva_compact_loaded: tuple[type[AgentState], Hook],
    monkeypatch: pytest.MonkeyPatch,
    database_gate: ProcessDbGate,
):
    state_cls, wrap_fn = meiva_compact_loaded
    patch_compact_config(monkeypatch, compact_reminder_tokens=1, auto_compact_tokens=1_000_000)
    state = reminder_state(state_cls, version=1, shown=True, seen=0)
    result = await wrap_fn(
        state, runtime_with_llm(make_fake_llm(), database_gate=database_gate), fake_config()
    )

    assert result is not None
    compact = result["compact"]
    assert isinstance(compact, CompactState)
    assert compact.reminder_shown is True
    assert compact.reminder_seen_version == 1


async def test_compact_reminder_is_not_starved_by_agent_inbounds(
    meiva_compact_loaded: tuple[type[AgentState], Hook],
    monkeypatch: pytest.MonkeyPatch,
    database_gate: ProcessDbGate,
):
    state_cls, wrap_fn = meiva_compact_loaded
    patch_compact_config(monkeypatch, compact_reminder_tokens=1, auto_compact_tokens=1_000_000)
    msgs = [
        SystemMessage(content="<sys>"),
        *(HumanMessage(content="x" * 1000) for _ in range(5)),
        inbound_message(content="ping", source="agent:5", inbound_id=1, body_start=0),
    ]
    state = reminder_state(state_cls, messages=msgs)
    result = await wrap_fn(
        state, runtime_with_llm(make_fake_llm(), database_gate=database_gate), fake_config()
    )
    assert result is not None
    note = result["messages"][0]
    assert isinstance(note, HumanMessage)
    assert "meiva.self.compact(summary)" in note.text


async def test_compact_reminder_silent_when_no_conversation(
    meiva_compact_loaded: tuple[type[AgentState], Hook],
    monkeypatch: pytest.MonkeyPatch,
    database_gate: ProcessDbGate,
):
    state_cls, wrap_fn = meiva_compact_loaded
    patch_compact_config(monkeypatch, compact_reminder_tokens=1, auto_compact_tokens=1_000_000)
    state = reminder_state(state_cls, messages=[SystemMessage(content="x" * 100_000)])
    result = await wrap_fn(
        state, runtime_with_llm(make_fake_llm(), database_gate=database_gate), fake_config()
    )
    assert result is None


async def test_compact_contract_reaches_request_without_resident_self(meiva_compact_loaded):
    from agent.graph.prompt.system_prompt import build_system_prompt
    from meiva.self import compact

    contract = compact.__doc__
    assert contract is not None
    for section in COMPACT_SECTIONS:
        assert section in contract, f"section {section!r} missing from the compact contract"

    system_prompt = build_system_prompt(
        EMPTY,
        AgentSlices.resolve(
            default_reader=defaults(
                agent__sdk_expand_in_system_prompt=["shell", "files", "agents", "tasks"]
            )
        ),
        agent_id=1,
        catalog=build_model_catalog(),
    )
    assert "## meiva.self\n" not in system_prompt
    head = SystemMessage(content=system_prompt)
    llm = make_fake_llm()
    await generate_summary(
        [head, HumanMessage(content="Keep my work")],
        llm,
        AgentSlices.resolve(
            default_reader=defaults(
                agent__sdk_expand_in_system_prompt=["shell", "files", "agents", "tasks"]
            )
        ),
        catalog=build_model_catalog(),
    )
    [call] = compaction_ainvoke(llm).call_args_list
    [request] = call.args
    assert request[0] is head
    from inspect import cleandoc

    assert request[-1].content.startswith(COMPACTION_INSTRUCTION)
    assert cleandoc(contract) in request[-1].content
    for section in COMPACT_SECTIONS:
        assert section in request[-1].content, (
            f"section {section!r} missing from compaction request"
        )


async def test_compact_summary_preserves_agent_continuity(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
):
    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    insert_compact_summary(db_conn, tid, "summary after compact")

    sys_msg = SystemMessage(content="<test sys prompt>")
    initial_msgs: list[AnyMessage] = [
        sys_msg,
        *(HumanMessage(content=f"history-{i}") for i in range(8)),
    ]
    state = AgentState(messages=initial_msgs)

    cmd = await claim_node(
        state, make_runtime(aops_pool, database_gate=database_gate), _config(tid)
    )
    assert cmd.goto == "init_context"
    assert cmd.update is not None
    reset = cmd.update["context_reset"]
    assert isinstance(reset, ContextReset)
    resume = reset.resume
    assert resume == BEFORE_LLM, (
        f"after compact should continue conversation (resume=BEFORE_LLM), actual {resume}"
    )


async def test_compact_summary_replaces_whole_history_no_tail(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
):
    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    sys_msg = SystemMessage(content="<test sys prompt>")
    initial_msgs: list[AnyMessage] = [
        sys_msg,
        *(HumanMessage(content=f"history-{i}") for i in range(8)),
    ]
    state = AgentState(messages=initial_msgs)

    insert_compact_summary(db_conn, tid, "the whole memory")
    cmd = await claim_node(
        state, make_runtime(aops_pool, database_gate=database_gate), _config(tid)
    )

    tail = compact_tail(cmd.update)
    assert [m.content for m in tail] == [compose_summary_message("the whole memory")]
    assert cmd.goto == "init_context"


@pytest.mark.parametrize("reminded", [False, True])
async def test_compact_summary_emits_compact_done(
    reminded: bool,
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    loguru_records: list[dict[str, Any]],
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
):
    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    insert_compact_summary(db_conn, tid, "summary after compact")
    state = AgentState(
        messages=[
            SystemMessage(content="<sys>"),
            *(HumanMessage(content=f"history-{i}") for i in range(4)),
        ],
        compact=CompactState(reminder_shown=reminded),
    )
    publisher = MagicMock()
    ctx = MeivaContext(
        ops_pool=aops_pool,
        llm=AsyncMock(),
        event_publisher=publisher,
        agent=AgentSlices.resolve(default_reader=defaults()),
        db=Database.from_settings(gate=database_gate),
        bus=EventBus.from_settings(),
        catalog=build_model_catalog(),
        clock_factory=Clock.from_settings,
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
        "reminder_shown": reminded,
        "compactions": 1,
        "history_chars": len("history-0") * 4,
        "summary_chars": len("summary after compact"),
        "summary_history_ratio": pytest.approx(
            len("summary after compact") / (len("history-0") * 4)
        ),
        "event": "compaction_completed",
        "msg": None,
    }


async def test_consecutive_compacts_both_processed(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
):
    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    sys_msg = SystemMessage(content="<test sys prompt>")

    insert_compact_summary(db_conn, tid, "first compact summary")
    initial_msgs: list[AnyMessage] = [
        sys_msg,
        *(HumanMessage(content=f"gen1-msg-{i}") for i in range(8)),
    ]
    state = AgentState(messages=initial_msgs)

    cmd1 = await claim_node(
        state, make_runtime(aops_pool, database_gate=database_gate), _config(tid)
    )
    tail1 = compact_tail(cmd1.update)
    assert tail1[0].content == compose_summary_message("first compact summary")

    compacted_state = AgentState(messages=[sys_msg, HumanMessage(content="first compact summary")])
    insert_compact_summary(db_conn, tid, "second compact summary")
    cmd2 = await claim_node(
        compacted_state, make_runtime(aops_pool, database_gate=database_gate), _config(tid)
    )
    tail2 = compact_tail(cmd2.update)
    assert [m.content for m in tail2] == [compose_summary_message("second compact summary")]
    assert cmd2.update is not None
    reset = cmd2.update["context_reset"]
    assert isinstance(reset, ContextReset)
    assert reset.resume != END


async def test_compact_with_empty_state_injects_system_message_and_summary(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
):
    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    insert_compact_summary(db_conn, tid, "compact before any chat")

    state = AgentState()

    cmd = await claim_node(
        state, make_runtime(aops_pool, database_gate=database_gate), _config(tid)
    )

    tail = compact_tail(cmd.update)
    assert [m.content for m in tail] == [compose_summary_message("compact before any chat")]
    assert cmd.goto == "init_context"


async def test_compact_with_super_long_summary_in_claim(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
):
    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    long_summary = "LONG_" * 10_000

    sys_msg = SystemMessage(content="<test sys prompt>")
    initial_msgs: list[AnyMessage] = [
        sys_msg,
        *(HumanMessage(content=f"m{i}") for i in range(6)),
    ]
    state = AgentState(messages=initial_msgs)

    insert_compact_summary(db_conn, tid, long_summary)
    cmd = await claim_node(
        state, make_runtime(aops_pool, database_gate=database_gate), _config(tid)
    )

    tail = compact_tail(cmd.update)
    assert tail[0].content == compose_summary_message(long_summary)
    assert len(tail[0].content) == len(compose_summary_message(long_summary))
    assert long_summary in tail[0].content


async def test_terminate_preserves_pending_summary_without_wiping_history(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    database_gate: ProcessDbGate,
):
    from agent.ownership.hosted import apply_hosted_lifecycle
    from agent.tests.claim.test_inbound_ownership import admit_owner, agent_row

    tid = agent_row(db_conn)
    old = await admit_owner(aops_pool, tid, database_gate=database_gate)
    summary_text = "summary before terminate"
    insert_compact_summary(db_conn, tid, summary_text)
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
    cmd = await claim_node(
        state,
        Runtime(
            context=replace(
                make_runtime(aops_pool, database_gate=database_gate).context,
                original_incarnation=old,
                hosted_resources=None,
                native_work=None,
            )
        ),
        _config(tid),
    )
    await apply_hosted_lifecycle(aops_pool, old, bus=event_bus, resources=None)

    assert cmd.goto == END
    assert cmd.update is not None
    assert "context_reset" not in cmd.update
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
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    database_gate: ProcessDbGate,
):
    from agent.ownership.hosted import apply_hosted_lifecycle
    from agent.tests.claim.test_inbound_ownership import admit_owner, agent_row

    tid = agent_row(db_conn)
    old = await admit_owner(aops_pool, tid, database_gate=database_gate)

    summary_text = "summary pre-restart"
    insert_compact_summary(db_conn, tid, summary_text)
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
    cmd = await claim_node(
        state,
        Runtime(
            context=replace(
                make_runtime(aops_pool, database_gate=database_gate).context,
                original_incarnation=old,
                hosted_resources=None,
                native_work=None,
            )
        ),
        _config(tid),
    )
    await apply_hosted_lifecycle(aops_pool, old, bus=event_bus, resources=None)

    assert cmd.goto == END
    assert cmd.update is not None
    assert "context_reset" not in cmd.update
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
    successor = await admit_owner(aops_pool, tid, database_gate=database_gate)
    assert successor.generation != old.generation
    assert db_conn.execute(
        "SELECT status,observed_at IS NOT NULL FROM inbound_messages WHERE id=%s", (restart_id,)
    ).fetchone() == ("done", True)
    resumed = await claim_node(
        state,
        Runtime(
            context=replace(
                make_runtime(aops_pool, database_gate=database_gate).context,
                original_incarnation=successor,
                hosted_resources=None,
                native_work=None,
            )
        ),
        _config(tid),
    )
    tail = compact_tail(resumed.update)
    assert tail[0].content == compose_summary_message(summary_text)
    assert resumed.goto == "init_context"
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (summary_id,)
    ).fetchone() == ("done",)
