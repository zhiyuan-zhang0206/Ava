"""Real PostgreSQL + compiled graph + exec child cooperative handoff."""

from typing import Annotated, Any
from unittest.mock import MagicMock
from uuid import uuid4

import psycopg
import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import START, StateGraph
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from agent import state as states
from agent.graph._claim import claim_node
from agent.graph._exec import exec_node
from agent.hosted_ownership import admit_hosted_runtime
from agent.impersonation import flush_checkpoint, protect_native_hooks, settle_checkpoint
from agent.startup import _wrap_saver_writes_with_nstep_interval
from ava._external_state import encode_plugin_delta
from shared import impersonation as leases
from shared.caller_identity import CallerIdentity
from shared.context import AvaContext
from shared.db import create_agent, insert_inbound_message
from shared.machine import machine_name
from shared.plugin_context import PluginContext
from shared.runtime_incarnation import RuntimeIncarnation
from shared.turn_identity import bind_turn_identity


def _add(left: int, right: int) -> int:
    return left + right


class HandoffState(BaseModel):
    total: Annotated[int, _add] = 0


async def _prepare_graph(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    Any,
    AsyncPostgresSaver,
    AvaContext,
    RunnableConfig,
    dict[str, Any],
    RuntimeIncarnation,
    dict[str, Any],
    list[Any],
]:
    empty_plugin_registry: tuple[tuple[str, Any], ...] = (
        ("_EXTRA_FIELDS", {}),
        ("_PLUGIN_NAMESPACE_FIELDS", {}),
        ("_PLUGIN_STATE_CLASSES", set[type[BaseModel]]()),
        ("_BASE_FIELD_DECLARED", set[str]()),
    )
    for name, value in empty_plugin_registry:
        monkeypatch.setattr(states, name, value)
    monkeypatch.setattr(states, "AgentState", states.AgentState)
    with PluginContext("handoff"):
        states.register_plugin_state(HandoffState)
    state_cls = states.build_agent_state()
    agent_id = create_agent(db_conn)
    machine = machine_name()
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine) VALUES(%s,'idling',%s)", (agent_id, machine)
    )
    db_conn.commit()
    owner = await admit_hosted_runtime(
        aops_pool, agent_id, machine, uuid4(), expected_from="idling"
    )
    assert owner is not None
    requested = leases.request(
        agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex"),
        reason="Do the task",
    )
    model_calls: list[Any] = []

    async def model(state: Any) -> Command[str]:
        model_calls.append(state)
        if len(model_calls) == 1:
            code = f"import ava\nava.impersonation.accept({requested['id']!r})"
            return Command(
                update={
                    "messages": [
                        AIMessage(
                            content="I accept the handoff",
                            tool_calls=[
                                {"id": "consent", "name": "execute_code", "args": {"code": code}}
                            ],
                        )
                    ]
                },
                goto="exec",
            )
        return Command(update={"halted": True}, goto="claim")

    async def before_llm(*_args: Any) -> Command[Any]:
        return Command(goto="llm")

    saver = AsyncPostgresSaver(aops_pool)
    await saver.setup()
    _wrap_saver_writes_with_nstep_interval(saver, 100)
    # The registered plugin fields are a dynamically constructed state schema.
    builder: Any = StateGraph(state_cls, context_schema=AvaContext)
    builder.add_node("claim", claim_node, destinations=("before_llm", "__end__", "claim"))
    builder.add_node(
        "before_llm", protect_native_hooks(before_llm), destinations=("llm", "__end__")
    )
    builder.add_node("llm", model, destinations=("exec", "claim"))
    builder.add_node("exec", exec_node, destinations=("after_exec",))

    def after_exec(_state: Any) -> Command[str]:
        return Command(goto="claim")

    builder.add_node("after_exec", after_exec, destinations=("claim",))
    builder.add_edge(START, "claim")
    graph = builder.compile(checkpointer=saver)
    ctx = AvaContext(ops_pool=aops_pool, event_publisher=MagicMock())
    config: RunnableConfig = {"configurable": {"thread_id": str(agent_id)}, "recursion_limit": 100}
    reset: dict[str, Any] = {
        "turn_active": False,
        "turn_idle": False,
        "exit_requested": False,
        "restart_requested": False,
    }
    return graph, saver, ctx, config, reset, owner, requested, model_calls


@pytest.mark.parametrize("finish", ["release", "expire"])
async def test_consent_exec_inbox_release_and_resume(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    finish: str,
) -> None:
    graph, saver, ctx, config, reset, owner, requested, model_calls = await _prepare_graph(
        db_conn, aops_pool, monkeypatch
    )
    agent_id = owner.agent_id

    with bind_turn_identity(agent_id, incarnation=owner):
        first = await graph.ainvoke(reset, config, context=ctx)
        assert first["turn_idle"]
        assert leases.get(requested["id"], requested["token"])["status"] == "accepted"
        # Merely returning from exec/graph has NOT issued the external lease.
        await flush_checkpoint(saver, agent_id)
        assert await settle_checkpoint(graph, agent_id)
        assert leases.require_active(requested["id"], requested["token"])["status"] == "active"
        checkpoint = await saver.aget(config)
        assert checkpoint is not None
        assert any(
            getattr(message, "tool_call_id", None) == "consent"
            for message in checkpoint["channel_values"]["messages"]
        )

        first_peer = insert_inbound_message(
            db_conn, agent_id, "Peer message acknowledged", source="agent:99"
        )
        second_peer = insert_inbound_message(
            db_conn, agent_id, "Peer message still pending", source="agent:99"
        )
        inbox = leases.inbox(requested["id"], requested["token"])
        assert {row["id"] for row in inbox} == {first_peer, second_peer}
        leases.ack(requested["id"], requested["token"], [first_peer])
        await graph.ainvoke(reset, config, context=ctx)
        await flush_checkpoint(saver, agent_id)
        assert len(model_calls) == 1
        assert db_conn.execute(
            "SELECT status FROM inbound_messages WHERE id=%s", (second_peer,)
        ).fetchone() == ("pending",)
        db_conn.commit()

        leases.merge_plugin_delta(
            requested["id"],
            requested["token"],
            encode_plugin_delta({"handoff__total": 7}),
            expected_version=0,
        )
        if finish == "release":
            leases.release(requested["id"], requested["token"], "External work complete")
        else:
            db_conn.execute(
                "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
                (requested["id"],),
            )
            db_conn.commit()
            with pytest.raises(leases.ImpersonationError, match="expired"):
                leases.require_active(requested["id"], requested["token"])
        assert not await settle_checkpoint(graph, agent_id)
        resumed = await graph.ainvoke(reset, config, context=ctx)
        await flush_checkpoint(saver, agent_id)
        assert resumed["handoff__total"] == 7
        assert len(model_calls) == 2
        transcript = "\n".join(str(message.content) for message in resumed["messages"])
        assert ("External work complete" if finish == "release" else "expired") in transcript
        assert "Peer message still pending" in transcript
        assert "Peer message acknowledged" not in transcript
        # A second resume-boundary pass cannot double an additive reducer.
        assert not await settle_checkpoint(graph, agent_id)
        assert (await graph.aget_state(config)).values["handoff__total"] == 7


@pytest.mark.parametrize("control", ["restart", "terminate", "cancel"])
async def test_replacement_host_adopts_held_agent_without_model(
    db_conn: psycopg.Connection[Any], aops_pool: AsyncConnectionPool[Any], control: str
) -> None:
    from services.agent_host.host import AgentHost

    agent_id = create_agent(db_conn)
    machine = machine_name()
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine) VALUES(%s,'idling',%s)", (agent_id, machine)
    )
    db_conn.commit()
    owner = await admit_hosted_runtime(
        aops_pool, agent_id, machine, uuid4(), expected_from="idling"
    )
    assert owner is not None
    lease = leases.request(agent_id, caller=CallerIdentity(kind="external_agent", subject="codex"))
    leases.accept(lease["id"], agent_id, owner)
    leases.activate(lease["id"], owner)
    graph = MagicMock()
    host = AgentHost(pool=aops_pool, checkpointer=MagicMock(), graph=graph, machine=machine)
    assert agent_id in {wake.agent_id for wake in await host.pending_inbound_wakes(180)}
    # Original host has stopped renewing; admission still uses its ordinary
    # ownership fence, while the longer external decision lease remains live.
    db_conn.execute(
        "UPDATE agents_meta SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
        (agent_id,),
    )
    db_conn.commit()
    await host.run_turn(agent_id)
    assert db_conn.execute(
        "SELECT runtime_owner,status,lease_expires_at>clock_timestamp() FROM agents_meta WHERE id=%s",
        (agent_id,),
    ).fetchone() == (host._owner, "idling", True)
    db_conn.commit()
    assert leases.require_active(lease["id"], lease["token"])["status"] == "active"
    assert agent_id not in {wake.agent_id for wake in await host.pending_inbound_wakes(180)}
    db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source) VALUES(%s,'',%s,'user')",
        (agent_id, control),
    )
    db_conn.commit()
    await host.run_turn(agent_id)
    expected = "expired" if control == "terminate" else "active"
    assert leases.get(lease["id"], lease["token"])["status"] == expected
    if control == "cancel":
        assert db_conn.execute(
            "SELECT status FROM inbound_messages WHERE agent_id=%s AND kind='cancel'",
            (agent_id,),
        ).fetchone() == ("pending",)
        db_conn.commit()
        assert agent_id not in {wake.agent_id for wake in await host.pending_inbound_wakes(180)}
    if control == "restart":
        # The replacement logical incarnation also adopts without boot hooks.
        await host.run_turn(agent_id)
        assert leases.require_active(lease["id"], lease["token"])["status"] == "active"
    graph.ainvoke.assert_not_called()
