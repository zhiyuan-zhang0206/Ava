"""Real PostgreSQL + compiled graph + exec child cooperative handoff."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any
from unittest.mock import MagicMock, Mock
from uuid import uuid4

import psycopg
import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import START, StateGraph
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from agent import impersonation
from agent import state as states
from agent.graph.claim.node import claim_node
from agent.graph.exec.node import exec_node
from agent.impersonation import flush_checkpoint, protect_native_hooks, settle_checkpoint
from agent.ownership.hosted import admit_hosted_runtime
from agent.startup import wrap_saver_writes_with_nstep_interval
from ava.external.state import encode_plugin_delta
from ava.sdk_surface.process_context import process_clients
from base.agents import impersonation as leases
from base.agents.context import AvaContext
from base.agents.context.identity import AgentIdentity
from base.agents.history.checkpoint_postgres_walks import (
    HistoryAsyncPostgresSaver as AsyncPostgresSaver,
)
from base.agents.history.delta_read_compat import wrap_saver_reads_with_delta_reconstruction
from base.agents.impersonation.notes import HandoffNotes
from base.agents.messages.caller_identity import CallerIdentity
from base.clock import Clock
from base.cluster.machine import machine_name
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.db import Database, create_agent, insert_inbound_message
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.lm.plugin_providers import build_model_catalog
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import HostedTurnResources
from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions
from tests.fixtures.pin_agent import hosted_resources as hosted_resources
from tests.impersonation_support import attested_caller, recorded_tree


def _notes() -> HandoffNotes:
    return HandoffNotes(Clock.from_settings, lambda: settings.general.message_timestamps)


def _relay_ready(_db: object, _bus: object, *_args: object) -> bool:
    return True


def _add(left: int, right: int) -> int:
    return left + right


class HandoffState(BaseModel):
    total: Annotated[int, _add] = 0


async def _prepare_graph(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    automatic: bool = False,
    config_authority: ConfigAuthority,
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
    registry = ExtensionRegistry((("handoff", PluginContributions(state=(HandoffState,))),))
    state_cls = states.build_agent_state(registry)
    agent_id = create_agent(db_conn)
    machine = machine_name()
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine) VALUES(%s,'idling',%s)", (agent_id, machine)
    )
    db_conn.commit()
    owner = await admit_hosted_runtime(
        aops_pool, agent_id, machine, uuid4(), expected_from="idling", db=Database.from_settings()
    )
    assert owner is not None
    requested = leases.request(
        Database.from_settings(),
        EventBus.from_settings(),
        agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex"),
        process_metadata=recorded_tree(),
        reason="Do the task",
        automatic=automatic,
        name="Integration test",
        executor_name="Codex: integration",
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
        authority=config_authority,
    )
    model_calls: list[Any] = []

    async def model(state: Any) -> Command[str]:
        model_calls.append(state)
        if len(model_calls) == 1 and not automatic:
            code = (
                "import ava\n"
                "import os, psycopg\n"
                "from base.host.env.registry import ADMIN_DATA_PLANE_ALIASES\n"
                "assert not ADMIN_DATA_PLANE_ALIASES.intersection(os.environ)\n"
                "with psycopg.connect(ava.DB_URL) as conn:\n"
                "    assert conn.execute('SELECT current_user').fetchone() == ('ava_g0_runner',)\n"
                f"ava.impersonation.accept({requested['id']!r}, "
                "'Hand the task to the external session.')"
            )
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
    wrap_saver_writes_with_nstep_interval(saver, 100)
    # Delta write model (#3180): fold delta-written messages on read (daemon parity).
    wrap_saver_reads_with_delta_reconstruction(saver)
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
    ctx = AvaContext(
        ops_pool=aops_pool,
        event_publisher=MagicMock(),
        agent=AgentSlices.resolve(
            default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
        ),
        db=Database.from_settings(),
        bus=EventBus.from_settings(),
        clients=process_clients(),
        identity=AgentIdentity(agent_id=agent_id, owns_loop=True),
        original_incarnation=owner,
        catalog=build_model_catalog(),
        clock_factory=Clock.from_settings,
    )
    config: RunnableConfig = {"configurable": {"thread_id": str(agent_id)}, "recursion_limit": 100}
    reset: dict[str, Any] = {
        "turn_active": False,
        "turn_idle": False,
        "exit_requested": False,
        "restart_requested": False,
    }
    return graph, saver, ctx, config, reset, owner, requested, model_calls


async def _assert_consent_tool_result_checkpointed(
    saver: AsyncPostgresSaver, config: RunnableConfig
) -> None:
    checkpoint = await saver.aget(config)
    assert checkpoint is not None
    assert any(
        getattr(message, "tool_call_id", None) == "consent"
        for message in checkpoint["channel_values"]["messages"]
    )


def _deliver_peers_and_ack_first(
    db_conn: psycopg.Connection[Any],
    database: Database,
    event_bus: EventBus,
    requested: dict[str, Any],
    agent_id: int,
) -> int:
    """Queue two peer messages, ACK the first through the lease; the still-pending id."""
    first_peer = insert_inbound_message(
        db_conn,
        agent_id,
        "Peer message acknowledged",
        source="agent:99",
        bus=event_bus,
        database=database,
    )
    second_peer = insert_inbound_message(
        db_conn,
        agent_id,
        "Peer message still pending",
        source="agent:99",
        bus=event_bus,
        database=database,
    )
    inbox = leases.inbox(database, requested["id"], attested_caller(requested))
    assert {row["id"] for row in inbox} == {first_peer, second_peer}
    leases.ack(database, event_bus, requested["id"], attested_caller(requested), [first_peer])
    return second_peer


def _end_external_session(
    db_conn: psycopg.Connection[Any],
    database: Database,
    event_bus: EventBus,
    requested: dict[str, Any],
    finish: str,
) -> None:
    """End the external lease by release or by letting it expire."""
    if finish == "release":
        leases.release(
            database,
            event_bus,
            requested["id"],
            attested_caller(requested),
            "External work complete",
        )
        return
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
        (requested["id"],),
    )
    db_conn.commit()
    with pytest.raises(leases.ImpersonationError, match="expired"):
        leases.require_active(database, requested["id"], attested_caller(requested))


def _assert_resumed_transcript(resumed: dict[str, Any], finish: str) -> None:
    transcript = "\n".join(str(message.content) for message in resumed["messages"])
    assert ("External work complete" if finish == "release" else "expired") in transcript
    assert "Peer message still pending" in transcript
    assert "Peer message acknowledged" not in transcript


@pytest.mark.parametrize("finish", ["release", "expire"])
@pytest.mark.usefixtures("runner_exec_env")
async def test_consent_exec_inbox_release_and_resume(
    hosted_resources: HostedTurnResources,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    finish: str,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
) -> None:
    scope = hosted_resources
    # The real exec child boots this installed unit, whose identity is file-owned.
    config_authority.env_path.write_text(f"AVA_MACHINE_NAME={machine_name()}\n", encoding="utf-8")
    graph, saver, ctx, config, reset, owner, requested, model_calls = await _prepare_graph(
        db_conn, aops_pool, monkeypatch, config_authority=config_authority
    )
    agent_id = owner.agent_id

    # The relay establishment gate is unit-tested separately; the real graph
    # handoff must not spawn a relay process in the test environment. The
    # recorded controller tree is synthetic (its pids are not live processes),
    # so pin the supervisor's liveness view (task #3998).

    def relay_ready(*_args: object) -> bool:
        return True

    monkeypatch.setattr(impersonation, "establish_relay", relay_ready)
    monkeypatch.setattr(impersonation, "_provider_anchor_states", Mock(return_value=["alive"]))
    first = await graph.ainvoke(
        reset,
        config,
        context=replace(ctx, original_incarnation=owner, hosted_resources=scope, native_work=None),
    )
    assert first["turn_idle"]
    assert (
        leases.get(database, event_bus, requested["id"], attested_caller(requested))["status"]
        == "accepted"
    )
    # Merely returning from exec/graph has NOT issued the external lease.
    await flush_checkpoint(saver, agent_id)
    assert await settle_checkpoint(
        graph,
        database,
        event_bus,
        agent_id,
        ctx.relays,
        incarnation=owner,
        resources=scope,
        notes=_notes(),
    )
    assert (
        leases.require_active(database, requested["id"], attested_caller(requested))["status"]
        == "active"
    )
    # The stubbed establishment writes no heartbeat; keep it fresh so the
    # held pass stays a no-op while this flow runs (task #3998).
    db_conn.execute(
        "UPDATE agent_impersonations SET relay_heartbeat_at=clock_timestamp() WHERE id=%s",
        (requested["id"],),
    )
    db_conn.commit()
    await _assert_consent_tool_result_checkpointed(saver, config)
    second_peer = _deliver_peers_and_ack_first(db_conn, database, event_bus, requested, agent_id)
    scope = await scope.require_service().turn()
    await graph.ainvoke(
        reset,
        config,
        context=replace(ctx, original_incarnation=owner, hosted_resources=scope, native_work=None),
    )
    await flush_checkpoint(saver, agent_id)
    assert len(model_calls) == 1
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (second_peer,)
    ).fetchone() == ("pending",)
    db_conn.commit()

    leases.merge_plugin_delta(
        database,
        requested["id"],
        attested_caller(requested),
        encode_plugin_delta({"handoff__total": 7}, graph.builder.state_schema),
        expected_version=0,
    )
    _end_external_session(db_conn, database, event_bus, requested, finish)
    assert not await settle_checkpoint(
        graph,
        database,
        event_bus,
        agent_id,
        ctx.relays,
        incarnation=owner,
        resources=scope,
        notes=_notes(),
    )
    scope = await scope.require_service().turn()
    resumed = await graph.ainvoke(
        reset,
        config,
        context=replace(ctx, original_incarnation=owner, hosted_resources=scope, native_work=None),
    )
    await flush_checkpoint(saver, agent_id)
    assert resumed["handoff__total"] == 7
    assert len(model_calls) == 2
    _assert_resumed_transcript(resumed, finish)
    # A second resume-boundary pass cannot double an additive reducer.
    assert not await settle_checkpoint(
        graph,
        database,
        event_bus,
        agent_id,
        ctx.relays,
        incarnation=owner,
        resources=scope,
        notes=_notes(),
    )
    assert (await graph.aget_state(config)).values["handoff__total"] == 7


def _executor_acks_says_and_releases(
    database: Database,
    event_bus: EventBus,
    requested: dict[str, Any],
    inbound_id: int,
) -> None:
    """The external executor reads and ACKs the queued input, reports, then releases."""
    from base.agents.impersonation import history as history

    leases.inbox(database, requested["id"], attested_caller(requested))
    leases.ack(database, event_bus, requested["id"], attested_caller(requested), [inbound_id])
    history.say(
        database,
        event_bus,
        requested["id"],
        attested_caller(requested),
        "Work completed",
        message_key="result",
    )
    leases.release(
        database,
        event_bus,
        requested["id"],
        attested_caller(requested),
        "Fixed login and verified the result.",
    )


def _assert_end_note_and_handoff_document(last: Any, workspace: Path) -> None:
    handoff_path = workspace / "impersonation" / "0.json"
    assert last.additional_kwargs["ava_note_tag"] == "impersonation"
    assert "Fixed login and verified" in last.content
    document = json.loads(handoff_path.read_text())
    assert [m["payload"]["content"] for m in document["messages"]] == [
        "During takeover",
        "Work completed",
    ]
    assert document["messages"][0]["acknowledged"]
    assert str(handoff_path) in last.content


def _assert_handoff_precedes_queued_input(messages: list[Any]) -> None:
    handoff_index = next(
        i for i, m in enumerate(messages) if m.id.startswith("impersonation-handoff:")
    )
    next_index = next(i for i, m in enumerate(messages) if "Next task" in m.content)
    assert handoff_index < next_index


async def test_automatic_takeover_handoff_precedes_queued_input(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
) -> None:
    from base.agents.impersonation import history as history

    graph, saver, ctx, config, reset, owner, requested, model_calls = await _prepare_graph(
        db_conn, aops_pool, monkeypatch, automatic=True, config_authority=config_authority
    )
    monkeypatch.setattr(impersonation, "establish_relay", _relay_ready)

    def workspace_for_agent(_agent_id: int) -> Path:
        return tmp_path

    monkeypatch.setattr(history, "workspace_dir", workspace_for_agent)
    await graph.ainvoke(
        reset,
        config,
        context=replace(ctx, original_incarnation=owner, hosted_resources=None, native_work=None),
    )
    assert not model_calls  # No native model acceptance turn.
    assert (
        leases.get(database, event_bus, requested["id"], attested_caller(requested))["status"]
        == "accepted"
    )
    await flush_checkpoint(saver, owner.agent_id)
    assert await settle_checkpoint(
        graph,
        database,
        event_bus,
        owner.agent_id,
        ctx.relays,
        incarnation=owner,
        resources=None,
        notes=_notes(),
    )
    inbound_id = insert_inbound_message(
        db_conn,
        owner.agent_id,
        "During takeover",
        source="user",
        bus=event_bus,
        database=database,
    )
    db_conn.commit()
    _executor_acks_says_and_releases(database, event_bus, requested, inbound_id)
    later = insert_inbound_message(
        db_conn, owner.agent_id, "Next task", source="user", bus=event_bus, database=database
    )
    db_conn.commit()
    await graph.ainvoke(
        reset,
        config,
        context=replace(ctx, original_incarnation=owner, hosted_resources=None, native_work=None),
    )
    assert not model_calls
    await flush_checkpoint(saver, owner.agent_id)
    assert not await settle_checkpoint(
        graph,
        database,
        event_bus,
        owner.agent_id,
        ctx.relays,
        incarnation=owner,
        resources=None,
        notes=_notes(),
    )
    snapshot = await graph.aget_state(config)
    _assert_end_note_and_handoff_document(snapshot.values["messages"][-1], tmp_path)
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (later,)
    ).fetchone() == ("pending",)
    # Repeated settlement must not duplicate the first resumed input.
    assert not await settle_checkpoint(
        graph,
        database,
        event_bus,
        owner.agent_id,
        ctx.relays,
        incarnation=owner,
        resources=None,
        notes=_notes(),
    )
    await graph.ainvoke(
        reset,
        config,
        context=replace(ctx, original_incarnation=owner, hosted_resources=None, native_work=None),
    )
    assert len(model_calls) == 1
    _assert_handoff_precedes_queued_input(model_calls[0].messages)


async def test_accepted_session_repairs_missing_start_checkpoint(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
) -> None:
    graph, saver, ctx, config, _reset, owner, requested, calls = await _prepare_graph(
        db_conn, aops_pool, monkeypatch, automatic=True, config_authority=config_authority
    )
    monkeypatch.setattr(impersonation, "establish_relay", _relay_ready)
    # Emulate a committed acceptance followed by a crash before claim's update.
    leases.accept(
        database,
        event_bus,
        requested["id"],
        owner.agent_id,
        owner,
        "Saved request, lost checkpoint",
    )
    assert await settle_checkpoint(
        graph,
        database,
        event_bus,
        owner.agent_id,
        ctx.relays,
        incarnation=owner,
        resources=None,
        notes=_notes(),
    )
    assert not calls
    durable = await saver.aget_tuple(config)
    assert durable is not None
    messages = durable.checkpoint["channel_values"]["messages"]
    assert [m.id for m in messages] == [
        "impersonation-introduction",
        f"impersonation-start:{owner.agent_id}:0",
    ]
    assert durable.checkpoint["channel_values"]["impersonation_introduced"] is True
    assert (
        leases.get(database, event_bus, requested["id"], attested_caller(requested))["status"]
        == "active"
    )


async def test_handoff_checkpoint_failure_keeps_gate_and_retry_flushes_receipt(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
) -> None:
    from agent.impersonation_handoff import deliver_handoff
    from base.agents.impersonation import history as history

    graph, saver, ctx, config, reset, owner, requested, calls = await _prepare_graph(
        db_conn, aops_pool, monkeypatch, automatic=True, config_authority=config_authority
    )
    monkeypatch.setattr(impersonation, "establish_relay", _relay_ready)

    def workspace_for_agent(_agent_id: int) -> Path:
        return tmp_path

    monkeypatch.setattr(history, "workspace_dir", workspace_for_agent)
    await graph.ainvoke(
        reset,
        config,
        context=replace(ctx, original_incarnation=owner, hosted_resources=None, native_work=None),
    )
    await flush_checkpoint(saver, owner.agent_id)
    assert await settle_checkpoint(
        graph,
        database,
        event_bus,
        owner.agent_id,
        ctx.relays,
        incarnation=owner,
        resources=None,
        notes=_notes(),
    )
    leases.release(
        database,
        event_bus,
        requested["id"],
        attested_caller(requested),
        "Done; please continue",
    )
    lease = history.resolve(database, owner.agent_id, 0)

    async def failed_flush(*_: object) -> None:
        raise OSError("checkpoint temporarily unavailable")

    monkeypatch.setattr(impersonation, "flush_checkpoint", failed_flush)
    with pytest.raises(OSError, match="checkpoint temporarily"):
        await deliver_handoff(graph, database, event_bus, lease, owner, notes=_notes())
    assert history.resolve(database, owner.agent_id, 0)["handoff_applied_at"] is None
    assert not calls
    monkeypatch.setattr(impersonation, "flush_checkpoint", flush_checkpoint)
    await deliver_handoff(graph, database, event_bus, lease, owner, notes=_notes())
    durable = await saver.aget_tuple(config)
    assert durable is not None
    notes = [
        m
        for m in durable.checkpoint["channel_values"]["messages"]
        if m.id == f"impersonation-handoff:{owner.agent_id}:0"
    ]
    assert len(notes) == 1
    assert history.resolve(database, owner.agent_id, 0)["handoff_applied_at"] is not None


async def test_handoff_of_a_released_log_native_lease_is_already_complete(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
) -> None:
    """With every source sealed at release, the event log is complete before delivery."""
    from agent.impersonation_handoff import deliver_handoff
    from base.agents.impersonation import history as history

    graph, saver, ctx, config, reset, owner, requested, _calls = await _prepare_graph(
        db_conn, aops_pool, monkeypatch, automatic=True, config_authority=config_authority
    )
    monkeypatch.setattr(impersonation, "establish_relay", _relay_ready)

    def workspace_for_agent(_agent_id: int) -> Path:
        return tmp_path

    monkeypatch.setattr(history, "workspace_dir", workspace_for_agent)
    await graph.ainvoke(
        reset,
        config,
        context=replace(ctx, original_incarnation=owner, hosted_resources=None, native_work=None),
    )
    await flush_checkpoint(saver, owner.agent_id)
    assert await settle_checkpoint(
        graph,
        database,
        event_bus,
        owner.agent_id,
        ctx.relays,
        incarnation=owner,
        resources=None,
        notes=_notes(),
    )
    leases.release(
        database,
        event_bus,
        requested["id"],
        attested_caller(requested),
        "Done; please continue",
    )
    lease = history.resolve(database, owner.agent_id, 0)
    await deliver_handoff(graph, database, event_bus, lease, owner, notes=_notes())

    landed = history.resolve(database, owner.agent_id, 0)
    assert landed["handoff_path"] is not None
    assert landed["handoff_applied_at"] is not None
    assert landed["events_completed_at"] is not None
    assert landed["event_delivery_pending_reason"] is None
    assert landed["handoff_document"]["statistics"]["event_delivery"]["state"] == "complete"
    assert '"state": "complete"' in (tmp_path / "impersonation" / "0.json").read_text()


def _assert_resume_note_delivery_contract(content: str) -> None:
    """Check that pending delivery cannot be read as no SDK activity."""
    assert "External work complete" in content
    assert "structured handoff" not in content
    assert "The session record is available at:" in content
    assert "impersonation/0.json" in content
    assert "missing entries do not establish that an action never happened" in content
    assert "Continue any requests whose completion is not established" in content
