"""Real PostgreSQL + compiled graph + exec child cooperative handoff."""

from pathlib import Path
from typing import Annotated, Any
from unittest.mock import MagicMock, Mock
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

from agent import impersonation
from agent import state as states
from agent.graph.claim.node import claim_node
from agent.graph.exec.node import exec_node
from agent.impersonation import flush_checkpoint, protect_native_hooks, settle_checkpoint
from agent.ownership.hosted import admit_hosted_runtime
from agent.startup import wrap_saver_writes_with_nstep_interval
from ava.external.state import encode_plugin_delta
from base.agents import impersonation as leases
from base.agents.context import AvaContext
from base.agents.history.delta_read_compat import wrap_saver_reads_with_delta_reconstruction
from base.agents.messages.caller_identity import CallerIdentity
from base.cluster.machine import machine_name
from base.db import create_agent, insert_inbound_message
from base.host.env.agent_slices import AgentSlices
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import bind_turn_identity
from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions
from tests.impersonation_support import attested_caller, recorded_tree


def _relay_ready(*_args: object) -> bool:
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
    # `build_agent_state` rebinds `agent.state.AgentState`; the monkeypatch restores it after the test.
    monkeypatch.setattr(states, "AgentState", states.AgentState)
    registry = ExtensionRegistry((("handoff", PluginContributions(state=(HandoffState,))),))
    state_cls = states.build_agent_state(registry)
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
        process_metadata=recorded_tree(),
        reason="Do the task",
        automatic=automatic,
        name="Integration test",
        executor_name="Codex: integration",
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
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
    ctx = AvaContext(ops_pool=aops_pool, event_publisher=MagicMock(), agent=AgentSlices.resolve())
    config: RunnableConfig = {"configurable": {"thread_id": str(agent_id)}, "recursion_limit": 100}
    reset: dict[str, Any] = {
        "turn_active": False,
        "turn_idle": False,
        "exit_requested": False,
        "restart_requested": False,
    }
    return graph, saver, ctx, config, reset, owner, requested, model_calls


@pytest.mark.parametrize("finish", ["release", "expire"])
@pytest.mark.usefixtures("runner_exec_env")
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

    # The relay establishment gate is unit-tested separately; the real graph
    # handoff must not spawn a relay process in the test environment. The
    # recorded controller tree is synthetic (its pids are not live processes),
    # so pin the supervisor's liveness view (task #3998).

    def relay_ready(_session: Any, _incarnation: Any) -> bool:
        return True

    monkeypatch.setattr(impersonation, "establish_relay", relay_ready)
    monkeypatch.setattr(impersonation, "_provider_anchor_states", Mock(return_value=["alive"]))
    with bind_turn_identity(agent_id, incarnation=owner):
        first = await graph.ainvoke(reset, config, context=ctx)
        assert first["turn_idle"]
        assert leases.get(requested["id"], attested_caller(requested))["status"] == "accepted"
        # Merely returning from exec/graph has NOT issued the external lease.
        await flush_checkpoint(saver, agent_id)
        assert await settle_checkpoint(graph, agent_id)
        assert (
            leases.require_active(requested["id"], attested_caller(requested))["status"] == "active"
        )
        # The stubbed establishment writes no heartbeat; keep it fresh so the
        # held pass stays a no-op while this flow runs (task #3998).
        db_conn.execute(
            "UPDATE agent_impersonations SET relay_heartbeat_at=clock_timestamp() WHERE id=%s",
            (requested["id"],),
        )
        db_conn.commit()
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
        inbox = leases.inbox(requested["id"], attested_caller(requested))
        assert {row["id"] for row in inbox} == {first_peer, second_peer}
        leases.ack(requested["id"], attested_caller(requested), [first_peer])
        await graph.ainvoke(reset, config, context=ctx)
        await flush_checkpoint(saver, agent_id)
        assert len(model_calls) == 1
        assert db_conn.execute(
            "SELECT status FROM inbound_messages WHERE id=%s", (second_peer,)
        ).fetchone() == ("pending",)
        db_conn.commit()

        leases.merge_plugin_delta(
            requested["id"],
            attested_caller(requested),
            encode_plugin_delta({"handoff__total": 7}),
            expected_version=0,
        )
        if finish == "release":
            leases.release(requested["id"], attested_caller(requested), "External work complete")
        else:
            db_conn.execute(
                "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
                (requested["id"],),
            )
            db_conn.commit()
            with pytest.raises(leases.ImpersonationError, match="expired"):
                leases.require_active(requested["id"], attested_caller(requested))
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


async def test_automatic_takeover_handoff_precedes_queued_input(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import json
    from pathlib import Path

    from base.agents.impersonation import history as history

    graph, saver, ctx, config, reset, owner, requested, model_calls = await _prepare_graph(
        db_conn,
        aops_pool,
        monkeypatch,
        automatic=True,
    )
    monkeypatch.setattr(impersonation, "establish_relay", _relay_ready)

    def workspace_for_agent(_agent_id: int) -> Path:
        return tmp_path

    monkeypatch.setattr(history, "workspace_dir", workspace_for_agent)
    with bind_turn_identity(owner.agent_id, incarnation=owner):
        await graph.ainvoke(reset, config, context=ctx)
        assert not model_calls  # No native model acceptance turn.
        assert leases.get(requested["id"], attested_caller(requested))["status"] == "accepted"
        await flush_checkpoint(saver, owner.agent_id)
        assert await settle_checkpoint(graph, owner.agent_id)
        inbound_id = insert_inbound_message(
            db_conn, owner.agent_id, "During takeover", source="user"
        )
        db_conn.commit()
        leases.inbox(requested["id"], attested_caller(requested))
        leases.ack(requested["id"], attested_caller(requested), [inbound_id])
        history.say(
            requested["id"], attested_caller(requested), "Work completed", message_key="result"
        )
        leases.release(
            requested["id"], attested_caller(requested), "Fixed login and verified the result."
        )
        later = insert_inbound_message(db_conn, owner.agent_id, "Next task", source="user")
        db_conn.commit()
        await graph.ainvoke(reset, config, context=ctx)
        assert not model_calls
        await flush_checkpoint(saver, owner.agent_id)
        assert not await settle_checkpoint(graph, owner.agent_id)
        snapshot = await graph.aget_state(config)
        last = snapshot.values["messages"][-1]
        assert last.additional_kwargs["ava_note_tag"] == "impersonation"
        assert "Fixed login and verified" in last.content
        document = json.loads((tmp_path / "impersonation" / "0.json").read_text())
        assert [m["payload"]["content"] for m in document["messages"]] == [
            "During takeover",
            "Work completed",
        ]
        assert document["messages"][0]["acknowledged"]
        assert str(Path(tmp_path) / "impersonation" / "0.json") in last.content
        assert db_conn.execute(
            "SELECT status FROM inbound_messages WHERE id=%s", (later,)
        ).fetchone() == ("pending",)
        # Repeated settlement must not duplicate the first resumed input.
        assert not await settle_checkpoint(graph, owner.agent_id)
        await graph.ainvoke(reset, config, context=ctx)
        assert len(model_calls) == 1
        messages = model_calls[0].messages
        handoff_index = next(
            i for i, m in enumerate(messages) if m.id.startswith("impersonation-handoff:")
        )
        next_index = next(i for i, m in enumerate(messages) if "Next task" in m.content)
        assert handoff_index < next_index


async def test_accepted_session_repairs_missing_start_checkpoint(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph, saver, _ctx, config, _reset, owner, requested, calls = await _prepare_graph(
        db_conn,
        aops_pool,
        monkeypatch,
        automatic=True,
    )
    monkeypatch.setattr(impersonation, "establish_relay", _relay_ready)
    # Emulate a committed acceptance followed by a crash before claim's update.
    leases.accept(requested["id"], owner.agent_id, owner, "Saved request, lost checkpoint")
    with bind_turn_identity(owner.agent_id, incarnation=owner):
        assert await settle_checkpoint(graph, owner.agent_id)
    assert not calls
    durable = await saver.aget_tuple(config)
    assert durable is not None
    messages = durable.checkpoint["channel_values"]["messages"]
    assert [m.id for m in messages] == [f"impersonation-start:{owner.agent_id}:0"]
    assert leases.get(requested["id"], attested_caller(requested))["status"] == "active"


async def test_handoff_checkpoint_failure_keeps_gate_and_retry_flushes_receipt(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from agent.impersonation_handoff import deliver_handoff
    from base.agents.impersonation import history as history

    graph, saver, ctx, config, reset, owner, requested, calls = await _prepare_graph(
        db_conn,
        aops_pool,
        monkeypatch,
        automatic=True,
    )
    monkeypatch.setattr(impersonation, "establish_relay", _relay_ready)

    def workspace_for_agent(_agent_id: int) -> Path:
        return tmp_path

    monkeypatch.setattr(history, "workspace_dir", workspace_for_agent)
    with bind_turn_identity(owner.agent_id, incarnation=owner):
        await graph.ainvoke(reset, config, context=ctx)
        await flush_checkpoint(saver, owner.agent_id)
        assert await settle_checkpoint(graph, owner.agent_id)
        leases.release(requested["id"], attested_caller(requested), "Done; please continue")
        lease = history.resolve(owner.agent_id, 0)

        async def failed_flush(*_: object) -> None:
            raise OSError("checkpoint temporarily unavailable")

        monkeypatch.setattr(impersonation, "flush_checkpoint", failed_flush)
        with pytest.raises(OSError, match="checkpoint temporarily"):
            await deliver_handoff(graph, lease, owner)
        assert history.resolve(owner.agent_id, 0)["handoff_applied_at"] is None
        assert not calls
        monkeypatch.setattr(impersonation, "flush_checkpoint", flush_checkpoint)
        await deliver_handoff(graph, lease, owner)
        durable = await saver.aget_tuple(config)
        assert durable is not None
        notes = [
            m
            for m in durable.checkpoint["channel_values"]["messages"]
            if m.id == f"impersonation-handoff:{owner.agent_id}:0"
        ]
        assert len(notes) == 1
        assert history.resolve(owner.agent_id, 0)["handoff_applied_at"] is not None


async def test_handoff_of_a_released_log_native_lease_is_already_complete(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With every source sealed at release, the event log is complete before delivery."""
    from agent.impersonation_handoff import deliver_handoff
    from base.agents.impersonation import history as history

    graph, saver, ctx, config, reset, owner, requested, _calls = await _prepare_graph(
        db_conn, aops_pool, monkeypatch, automatic=True
    )
    monkeypatch.setattr(impersonation, "establish_relay", _relay_ready)

    def workspace_for_agent(_agent_id: int) -> Path:
        return tmp_path

    monkeypatch.setattr(history, "workspace_dir", workspace_for_agent)
    with bind_turn_identity(owner.agent_id, incarnation=owner):
        await graph.ainvoke(reset, config, context=ctx)
        await flush_checkpoint(saver, owner.agent_id)
        assert await settle_checkpoint(graph, owner.agent_id)
        leases.release(requested["id"], attested_caller(requested), "Done; please continue")
        lease = history.resolve(owner.agent_id, 0)
        await deliver_handoff(graph, lease, owner)

        landed = history.resolve(owner.agent_id, 0)
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
    assert "the complete structured record of this session is available at:" in content
    assert "impersonation/0.json" in content
    assert "zero means no events have been consumed yet" in content
    assert "not that no SDK calls occurred" in content
    assert "record of emitted SDK/API events" in content
    assert "never proves no SDK calls" in content


async def test_end_note_resumes_an_empty_queue(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A release with nothing queued must still run the end note's first turn.

    Regression: the end-of-session note is a system note, so a window that never
    carried a real exchange reports has_conversation() == False and the claim
    idled out with the note unprocessed. The note is the resumed input: the claim
    runs before_llm with an empty queue, and delivery publishes a wake.
    """
    from base.agents.impersonation import history as history

    graph, saver, ctx, config, reset, owner, requested, model_calls = await _prepare_graph(
        db_conn, aops_pool, monkeypatch, automatic=True
    )
    monkeypatch.setattr(impersonation, "establish_relay", _relay_ready)

    def workspace_for_agent(_agent_id: int) -> Path:
        return tmp_path

    monkeypatch.setattr(history, "workspace_dir", workspace_for_agent)
    wakes: list[tuple[int, str]] = []

    def record_wake(agent_id: int, payload: str) -> bool:
        wakes.append((agent_id, payload))
        return True

    monkeypatch.setattr("agent.impersonation_handoff.publish_inbound_wake", record_wake)
    with bind_turn_identity(owner.agent_id, incarnation=owner):
        await graph.ainvoke(reset, config, context=ctx)
        assert not model_calls  # No native model acceptance turn.
        await flush_checkpoint(saver, owner.agent_id)
        assert await settle_checkpoint(graph, owner.agent_id)
        leases.release(requested["id"], attested_caller(requested), "External work complete")
        assert not await settle_checkpoint(graph, owner.agent_id)
        assert not model_calls
        assert wakes == [(owner.agent_id, "impersonation")]

        resumed = await graph.ainvoke(reset, config, context=ctx)
        await flush_checkpoint(saver, owner.agent_id)
        assert len(model_calls) == 1
        note = model_calls[0].messages[-1]
        assert note.id == f"impersonation-handoff:{owner.agent_id}:0"
        assert note.additional_kwargs["ava_note_tag"] == "impersonation"
        _assert_resume_note_delivery_contract(note.content)
        # The note is consumed once: another pass finds an idle agent, not a resume.
        await graph.ainvoke(reset, config, context=ctx)
        assert len(model_calls) == 1
        assert resumed["impersonation_handoff_id"] == f"{owner.agent_id}:0"


@pytest.mark.parametrize("cause", ["relay_death", "ack_exhaustion"])
async def test_aborted_takeover_resumes_the_native_with_the_death_cause(
    cause: str,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Task #3998 end to end: the supervisor stops an automatic takeover whose
    relay handle vanished outside the fresh-start window, and the resume chain
    delivers the end note naming the death cause; the note's first turn runs."""
    from base.agents.impersonation import history as history

    graph, saver, ctx, config, reset, owner, requested, model_calls = await _prepare_graph(
        db_conn, aops_pool, monkeypatch, automatic=True
    )
    monkeypatch.setattr(impersonation, "establish_relay", _relay_ready)

    def workspace_for_agent(_agent_id: int) -> Path:
        return tmp_path

    monkeypatch.setattr(history, "workspace_dir", workspace_for_agent)
    wakes: list[tuple[int, str]] = []

    def record_wake(agent_id: int, payload: str) -> bool:
        wakes.append((agent_id, payload))
        return True

    monkeypatch.setattr("agent.impersonation_handoff.publish_inbound_wake", record_wake)
    with bind_turn_identity(owner.agent_id, incarnation=owner):
        await graph.ainvoke(reset, config, context=ctx)
        await flush_checkpoint(saver, owner.agent_id)
        assert await settle_checkpoint(graph, owner.agent_id)
        assert not model_calls
        if cause == "relay_death":
            # A lost relay outside the fresh-start window stops the takeover.
            monkeypatch.setattr(
                impersonation, "_provider_anchor_states", Mock(return_value=["alive"])
            )
            monkeypatch.setattr(
                impersonation, "_PROCESS_STARTED_MONOTONIC", impersonation.time.monotonic() - 1000.0
            )
            session = leases.native_status(owner.agent_id, owner)
            assert session is not None
            await impersonation.supervise_relay(session, owner.agent_id)
            detail = "the bound relay stopped heartbeating"
        else:
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
            detail = f"the executor did not ACK message {row[0]} after 2 delivery attempts (180s per ACK window)"
        died = leases.get(requested["id"], attested_caller(requested))
        assert died["status"] == "expired"
        assert died["rejection_reason"] == f"aborted: {detail}"
        # The graph boundary pass observes the terminal lease; the resume chain
        # then delivers the end note (never the abort transaction itself).
        await graph.ainvoke(reset, config, context=ctx)
        assert not model_calls
        await flush_checkpoint(saver, owner.agent_id)
        assert not await settle_checkpoint(graph, owner.agent_id)
        assert wakes == [(owner.agent_id, "impersonation")]
        resumed = await graph.ainvoke(reset, config, context=ctx)
        await flush_checkpoint(saver, owner.agent_id)
        assert len(model_calls) == 1
        note = model_calls[0].messages[-1]
        assert note.id == f"impersonation-handoff:{owner.agent_id}:0"
        assert f"This session was stopped early: {detail}." in note.content
        if cause == "ack_exhaustion":
            handoff = (tmp_path / "impersonation/0.json").read_text()
            assert "Preserve this unacknowledged input" in handoff
            assert '"acknowledged": false' in handoff
        assert "structured handoff" not in note.content
        assert resumed["impersonation_handoff_id"] == f"{owner.agent_id}:0"
