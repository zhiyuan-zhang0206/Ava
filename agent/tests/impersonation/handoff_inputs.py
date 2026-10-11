"""Real handoff graph, owned clients and transport evidence for integration tests."""

import json
from collections.abc import Callable, Iterator
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
from agent.impersonation import protect_native_hooks, settle_checkpoint
from agent.impersonation_handoff import flush_checkpoint
from agent.ownership.hosted import admit_hosted_runtime
from agent.startup import wrap_saver_writes_with_nstep_interval
from ava.sdk_surface.process_context import process_clients
from base.agents import impersonation as leases
from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet
from base.agents.context.identity import AgentIdentity
from base.agents.history.checkpoint_postgres_walks import (
    HistoryAsyncPostgresSaver as AsyncPostgresSaver,
)
from base.agents.history.delta_read_compat import wrap_saver_reads_with_delta_reconstruction
from base.agents.impersonation.notes import HandoffNotes
from base.agents.messages.caller_identity import CallerIdentity
from base.agents.observation.relay_supervision import RelaySupervision
from base.clock import Clock
from base.cluster.machine import MachineIdentity
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.db import Database, create_agent, insert_inbound_message
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.lm.plugin_providers import build_model_catalog
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions
from tests.fixtures.configuration import snapshot_process_config
from tests.fixtures.pin_agent import hosted_resources as hosted_resources
from tests.impersonation_support import attested_caller, recorded_tree

__all__ = [
    "HandoffState",
    "assert_consent_tool_result_checkpointed",
    "assert_death_cause_note",
    "assert_end_note_and_handoff_document",
    "assert_handoff_precedes_queued_input",
    "assert_resume_note_delivery_contract",
    "assert_resumed_transcript",
    "assert_successor_handoff_preserves_unacknowledged_input",
    "assert_unacknowledged_input_preserved",
    "deliver_peers_and_ack_first",
    "end_external_session",
    "executor_acks_says_and_releases",
    "handoff_clients",
    "handoff_notes",
    "prepare_graph",
    "relay_ready",
    "resume_native",
    "stop_takeover_with_exhausted_ack",
    "stop_takeover_with_lost_relay",
]


@pytest.fixture
def handoff_clients(machine_identity: MachineIdentity) -> Iterator[ClientSet]:
    clients = process_clients(config=snapshot_process_config(), machine=machine_identity)
    try:
        yield clients
    finally:
        clients.close()


def handoff_notes() -> HandoffNotes:
    return HandoffNotes(Clock.from_settings, lambda: settings.general.message_timestamps)


def relay_ready(
    _db: object, _bus: object, *_args: object, read_local_machine: Callable[[], str]
) -> bool:
    return True


def _add(left: int, right: int) -> int:
    return left + right


class HandoffState(BaseModel):
    total: Annotated[int, _add] = 0


async def prepare_graph(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    automatic: bool = False,
    config_authority: ConfigAuthority,
    clients: ClientSet,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
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
    machine = machine_identity.name()
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine) VALUES(%s,'idling',%s)", (agent_id, machine)
    )
    db_conn.commit()
    owner = await admit_hosted_runtime(
        aops_pool,
        agent_id,
        machine,
        uuid4(),
        expected_from="idling",
        db=Database.from_settings(gate=database_gate),
    )
    assert owner is not None
    requested = leases.request(
        Database.from_settings(gate=database_gate),
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
        local_machine=machine_identity.name(),
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
        machine=machine_identity,
        ops_pool=aops_pool,
        event_publisher=MagicMock(),
        agent=AgentSlices.resolve(
            default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
        ),
        db=Database.from_settings(gate=database_gate),
        bus=EventBus.from_settings(),
        clients=clients,
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


async def assert_consent_tool_result_checkpointed(
    saver: AsyncPostgresSaver, config: RunnableConfig
) -> None:
    checkpoint = await saver.aget(config)
    assert checkpoint is not None
    assert any(
        getattr(message, "tool_call_id", None) == "consent"
        for message in checkpoint["channel_values"]["messages"]
    )


def deliver_peers_and_ack_first(
    db_conn: psycopg.Connection[Any],
    database: Database,
    event_bus: EventBus,
    requested: dict[str, Any],
    agent_id: int,
    *,
    machine_identity: MachineIdentity,
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
    inbox = leases.inbox(
        database,
        requested["id"],
        attested_caller(
            requested,
        ),
        local_machine=machine_identity.name(),
    )
    assert {row["id"] for row in inbox} == {first_peer, second_peer}
    leases.ack(
        database,
        event_bus,
        requested["id"],
        attested_caller(
            requested,
        ),
        [first_peer],
        local_machine=machine_identity.name(),
    )
    return second_peer


def end_external_session(
    db_conn: psycopg.Connection[Any],
    database: Database,
    event_bus: EventBus,
    requested: dict[str, Any],
    finish: str,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """End the external lease by release or by letting it expire."""
    if finish == "release":
        leases.release(
            database,
            event_bus,
            requested["id"],
            attested_caller(requested),
            "External work complete",
            local_machine=machine_identity.name(),
        )
        return
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
        (requested["id"],),
    )
    db_conn.commit()
    with pytest.raises(leases.ImpersonationError, match="expired"):
        leases.require_active(
            database,
            requested["id"],
            attested_caller(
                requested,
            ),
            local_machine=machine_identity.name(),
        )


def assert_resumed_transcript(resumed: dict[str, Any], finish: str) -> None:
    transcript = "\n".join(str(message.content) for message in resumed["messages"])
    assert ("External work complete" if finish == "release" else "expired") in transcript
    assert "Peer message still pending" in transcript
    assert "Peer message acknowledged" not in transcript


def executor_acks_says_and_releases(
    database: Database,
    event_bus: EventBus,
    requested: dict[str, Any],
    inbound_id: int,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """The external executor reads and ACKs the queued input, reports, then releases."""
    from base.agents.impersonation import history as history

    leases.inbox(
        database,
        requested["id"],
        attested_caller(
            requested,
        ),
        local_machine=machine_identity.name(),
    )
    leases.ack(
        database,
        event_bus,
        requested["id"],
        attested_caller(
            requested,
        ),
        [inbound_id],
        local_machine=machine_identity.name(),
    )
    history.say(
        database,
        event_bus,
        requested["id"],
        attested_caller(requested),
        "Work completed",
        message_key="result",
        local_machine=machine_identity.name(),
    )
    leases.release(
        database,
        event_bus,
        requested["id"],
        attested_caller(requested),
        "Fixed login and verified the result.",
        local_machine=machine_identity.name(),
    )


def assert_end_note_and_handoff_document(last: Any, workspace: Path) -> None:
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


def assert_handoff_precedes_queued_input(messages: list[Any]) -> None:
    handoff_index = next(
        i for i, m in enumerate(messages) if m.id.startswith("impersonation-handoff:")
    )
    next_index = next(i for i, m in enumerate(messages) if "Next task" in m.content)
    assert handoff_index < next_index


def assert_resume_note_delivery_contract(content: str) -> None:
    """Check that pending delivery cannot be read as no SDK activity."""
    assert "External work complete" in content
    assert "structured handoff" not in content
    assert "The session record is available at:" in content
    assert "impersonation/0.json" in content
    assert "missing entries do not establish that an action never happened" in content
    assert "Continue any requests whose completion is not established" in content


async def stop_takeover_with_lost_relay(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    owner: RuntimeIncarnation,
    relays: RelaySupervision,
    *,
    machine_identity: MachineIdentity,
) -> str:
    """Observe unknown legacy relay custody without revoking the executor lease."""
    monkeypatch.setattr(impersonation, "_provider_anchor_states", Mock(return_value=["alive"]))
    session = leases.native_status(database, event_bus, owner.agent_id, owner)
    assert session is not None
    await impersonation.supervise_relay(
        database,
        event_bus,
        session,
        owner.agent_id,
        relays,
        incarnation=owner,
        read_local_machine=machine_identity.name,
    )
    return "the bound relay stopped heartbeating"


def stop_takeover_with_exhausted_ack(
    db_conn: psycopg.Connection[Any], requested: dict[str, Any], owner: RuntimeIncarnation
) -> str:
    """Stage an input whose ACK window ran out twice; the death detail the supervisor names."""
    row = db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,kind,source,content) "
        "VALUES(%s,'chat','user','Preserve this unacknowledged input') RETURNING id",
        (owner.agent_id,),
    ).fetchone()
    assert row is not None
    db_conn.execute(
        "INSERT INTO agent_impersonation_messages(lease_id,inbound_id,delivery_attempts,last_delivery_at) "
        "VALUES(%s,%s,2,clock_timestamp()-interval '181 seconds')",
        (requested["id"], row[0]),
    )
    db_conn.commit()
    return (
        f"the executor did not ACK message {row[0]} after 2 delivery attempts (180s per ACK window)"
    )


def assert_death_cause_note(note: Any, agent_id: int, detail: str) -> None:
    assert note.id == f"impersonation-handoff:{agent_id}:0"
    assert f"This session was stopped early: {detail}." in note.content
    assert "structured handoff" not in note.content


def assert_unacknowledged_input_preserved(workspace: Path) -> None:
    handoff = (workspace / "impersonation/0.json").read_text()
    assert "Preserve this unacknowledged input" in handoff
    assert '"acknowledged": false' in handoff


def assert_successor_handoff_preserves_unacknowledged_input(
    resumed: dict[str, Any], workspace: Path
) -> None:
    transcript = "\n".join(str(message.content) for message in resumed["messages"])
    assert "Successor handoff" in transcript
    assert "Peer message acknowledged" not in transcript
    # Inputs the executor read are returned through the handoff record, not replayed.
    handoff_path = workspace / "impersonation/0.json"
    assert str(handoff_path) in transcript
    handoff = json.loads(handoff_path.read_text())
    returned = next(
        row
        for row in handoff["messages"]
        if row["payload"]["content"] == "Peer message still pending"
    )
    assert returned["acknowledged"] is False


async def resume_native(
    graph: Any,
    saver: Any,
    ctx: Any,
    config: Any,
    reset: Any,
    owner: RuntimeIncarnation,
    model_calls: Any,
    wakes: list[tuple[int, str]],
    database: Database,
    event_bus: EventBus,
) -> None:
    # The graph boundary pass observes the terminal lease; the resume chain
    # then delivers the end note (never the abort transaction itself).
    ctx = replace(ctx, original_incarnation=owner)
    await graph.ainvoke(reset, config, context=ctx)
    assert not model_calls
    await flush_checkpoint(saver, owner.agent_id)
    assert not await settle_checkpoint(
        graph,
        database,
        event_bus,
        owner.agent_id,
        ctx.relays,
        incarnation=owner,
        resources=ctx.hosted_resources,
        notes=handoff_notes(),
        read_local_machine=lambda: ctx.require_machine().name(),
    )
    assert wakes == [(owner.agent_id, "impersonation")]
    resumed = await graph.ainvoke(reset, config, context=ctx)
    await flush_checkpoint(saver, owner.agent_id)
    assert len(model_calls) == 1
    assert resumed["impersonation_handoff_id"] == f"{owner.agent_id}:0"
