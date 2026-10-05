"""Real PostgreSQL + compiled graph + exec child cooperative handoff."""

from pathlib import Path
from typing import Any
from unittest.mock import Mock

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent import impersonation
from agent.impersonation import flush_checkpoint, settle_checkpoint
from agent.tests.test_impersonation_integration import _prepare_graph, _relay_ready
from base.agents import impersonation as leases
from base.agents.observation.relay_supervision import RelaySupervision
from base.db import Database
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import bind_turn_identity
from tests.impersonation_support import attested_caller


async def _stop_takeover_with_lost_relay(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    owner: RuntimeIncarnation,
    relays: RelaySupervision,
) -> str:
    """Observe unknown legacy relay custody without revoking the executor lease."""
    monkeypatch.setattr(impersonation, "_provider_anchor_states", Mock(return_value=["alive"]))
    session = leases.native_status(database, event_bus, owner.agent_id, owner)
    assert session is not None
    await impersonation.supervise_relay(database, event_bus, session, owner.agent_id, relays)
    return "the bound relay stopped heartbeating"


def _stop_takeover_with_exhausted_ack(
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


def _assert_death_cause_note(note: Any, agent_id: int, detail: str) -> None:
    assert note.id == f"impersonation-handoff:{agent_id}:0"
    assert f"This session was stopped early: {detail}." in note.content
    assert "structured handoff" not in note.content


def _assert_unacknowledged_input_preserved(workspace: Path) -> None:
    handoff = (workspace / "impersonation/0.json").read_text()
    assert "Preserve this unacknowledged input" in handoff
    assert '"acknowledged": false' in handoff


async def _resume_native(
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
    await graph.ainvoke(reset, config, context=ctx)
    assert not model_calls
    await flush_checkpoint(saver, owner.agent_id)
    assert not await settle_checkpoint(graph, database, event_bus, owner.agent_id, ctx.relays)
    assert wakes == [(owner.agent_id, "impersonation")]
    resumed = await graph.ainvoke(reset, config, context=ctx)
    await flush_checkpoint(saver, owner.agent_id)
    assert len(model_calls) == 1
    assert resumed["impersonation_handoff_id"] == f"{owner.agent_id}:0"


@pytest.mark.parametrize("cause", ["relay_death", "ack_exhaustion", "executor_death"])
async def test_transport_fault_keeps_native_parked_until_actual_lease_end(
    cause: str,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: Database,
    event_bus: EventBus,
) -> None:
    """Actual graph fencing persists through delivery faults; only authority end resumes it."""
    from base.agents.impersonation import history as history

    graph, saver, ctx, config, reset, owner, requested, model_calls = await _prepare_graph(
        db_conn, aops_pool, monkeypatch, automatic=True
    )
    monkeypatch.setattr(impersonation, "establish_relay", _relay_ready)

    def workspace_for_agent(_agent_id: int) -> Path:
        return tmp_path

    monkeypatch.setattr(history, "workspace_dir", workspace_for_agent)
    wakes: list[tuple[int, str]] = []

    def record_wake(_db: object, _bus: object, agent_id: int, payload: str) -> bool:
        wakes.append((agent_id, payload))
        return True

    monkeypatch.setattr("agent.impersonation_handoff.publish_inbound_wake", record_wake)
    with bind_turn_identity(owner.agent_id, incarnation=owner):
        await graph.ainvoke(reset, config, context=ctx)
        await flush_checkpoint(saver, owner.agent_id)
        assert await settle_checkpoint(graph, database, event_bus, owner.agent_id, ctx.relays)
        assert not model_calls
        if cause == "relay_death":
            detail = await _stop_takeover_with_lost_relay(
                monkeypatch, database, event_bus, owner, ctx.relays
            )
        elif cause == "ack_exhaustion":
            detail = _stop_takeover_with_exhausted_ack(db_conn, requested, owner)
            monkeypatch.setattr(
                impersonation, "_provider_anchor_states", Mock(return_value=["alive"])
            )
        else:
            detail = "the executor process is gone"
            monkeypatch.setattr(
                impersonation, "_provider_anchor_states", Mock(return_value=["dead"])
            )
            session = leases.native_status(database, event_bus, owner.agent_id, owner)
            await impersonation.supervise_relay(
                database, event_bus, session, owner.agent_id, ctx.relays
            )
        died = leases.get(database, event_bus, requested["id"], attested_caller(requested))
        if cause == "executor_death":
            assert died["status"] == "expired"
            assert died["rejection_reason"] == f"aborted: {detail}"
        else:
            assert died["status"] == "active"
            assert died["rejection_reason"] is None
            await graph.ainvoke(reset, config, context=ctx)
            assert not model_calls  # No native invocation overlaps valid external authority.
            leases.release(
                database,
                event_bus,
                requested["id"],
                attested_caller(requested),
                "Explicit end after preserved delivery fault",
            )
        await _resume_native(
            graph, saver, ctx, config, reset, owner, model_calls, wakes, database, event_bus
        )
        if cause == "executor_death":
            _assert_death_cause_note(model_calls[0].messages[-1], owner.agent_id, detail)
        else:
            assert (
                "Explicit end after preserved delivery fault" in model_calls[0].messages[-1].content
            )
        if cause == "ack_exhaustion":
            _assert_unacknowledged_input_preserved(tmp_path)
