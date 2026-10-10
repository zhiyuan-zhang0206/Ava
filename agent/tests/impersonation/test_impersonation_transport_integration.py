"""Real PostgreSQL + compiled graph + exec child cooperative handoff."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import psutil
import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent import impersonation
from agent.impersonation import flush_checkpoint, settle_checkpoint
from agent.ownership.hosted import admit_hosted_runtime
from agent.tests.impersonation.test_impersonation_integration import (
    _assert_resume_note_delivery_contract,
    _deliver_peers_and_ack_first,
    _end_external_session,
    _notes,
    _prepare_graph,
    _relay_ready,
)
from agent.tests.impersonation.test_impersonation_integration import (
    handoff_clients as handoff_clients,
)
from base.agents import impersonation as leases
from base.agents.context.clients import ClientSet
from base.agents.impersonation import _store, delivery
from base.agents.observation.relay_supervision import RelaySupervision
from base.cluster.machine import machine_name
from base.config.service_read import ConfigAuthority
from base.db import Database, insert_inbound_message
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
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
    await impersonation.supervise_relay(
        database, event_bus, session, owner.agent_id, relays, incarnation=owner
    )
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


def _assert_successor_handoff_preserves_unacknowledged_input(
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
        notes=_notes(),
    )
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
    *,
    config_authority: ConfigAuthority,
    handoff_clients: ClientSet,
    database_gate: ProcessDbGate,
) -> None:
    """Actual graph fencing persists through delivery faults; only authority end resumes it."""
    from base.agents.impersonation import history as history

    graph, saver, ctx, config, reset, owner, requested, model_calls = await _prepare_graph(
        db_conn,
        aops_pool,
        monkeypatch,
        automatic=True,
        config_authority=config_authority,
        clients=handoff_clients,
        database_gate=database_gate,
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
    assert not model_calls
    if cause == "relay_death":
        detail = await _stop_takeover_with_lost_relay(
            monkeypatch, database, event_bus, owner, ctx.relays
        )
    elif cause == "ack_exhaustion":
        detail = _stop_takeover_with_exhausted_ack(db_conn, requested, owner)
        monkeypatch.setattr(impersonation, "_provider_anchor_states", Mock(return_value=["alive"]))
    else:
        detail = "the executor process is gone"
        monkeypatch.setattr(impersonation, "_provider_anchor_states", Mock(return_value=["dead"]))
        session = leases.native_status(database, event_bus, owner.agent_id, owner)
        await impersonation.supervise_relay(
            database, event_bus, session, owner.agent_id, ctx.relays, incarnation=owner
        )
    died = leases.get(database, event_bus, requested["id"], attested_caller(requested))
    if cause == "executor_death":
        assert died["status"] == "expired"
        assert died["rejection_reason"] == f"aborted: {detail}"
    else:
        assert died["status"] == "active"
        assert died["rejection_reason"] is None
        await graph.ainvoke(
            reset,
            config,
            context=replace(
                ctx, original_incarnation=owner, hosted_resources=None, native_work=None
            ),
        )
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
        assert "Explicit end after preserved delivery fault" in model_calls[0].messages[-1].content
    if cause == "ack_exhaustion":
        _assert_unacknowledged_input_preserved(tmp_path)


@pytest.mark.parametrize("probe_state", ["unknown", "denied"])
async def test_unreadable_executor_preserves_authority_across_native_wakes(
    probe_state: str,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    handoff_clients: ClientSet,
    database_gate: ProcessDbGate,
) -> None:
    """Repeated unreadable process evidence cannot spend TTL or consume queued input."""
    graph, saver, ctx, config, reset, owner, requested, model_calls = await _prepare_graph(
        db_conn,
        aops_pool,
        monkeypatch,
        automatic=True,
        config_authority=config_authority,
        clients=handoff_clients,
        database_gate=database_gate,
    )
    monkeypatch.setattr(impersonation, "establish_relay", _relay_ready)
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
    pending = _deliver_peers_and_ack_first(db_conn, database, event_bus, requested, owner.agent_id)
    expiry = leases.require_active(database, requested["id"], attested_caller(requested))[
        "expires_at"
    ]
    original_process = psutil.Process

    def unreadable_process(pid: int) -> Any:
        if pid == 4240:  # The synthetic recorded Codex anchor, not a test process.
            if probe_state == "denied":
                raise psutil.AccessDenied(pid)
            raise OSError("Process identity temporarily unreadable")
        return original_process(pid)

    monkeypatch.setattr(_store.psutil, "Process", unreadable_process)
    assert leases.provider_anchor_states(requested["process_metadata"]) == [probe_state]
    for _ in range(3):
        await graph.ainvoke(
            reset,
            config,
            context=replace(
                ctx, original_incarnation=owner, hosted_resources=None, native_work=None
            ),
        )
        await flush_checkpoint(saver, owner.agent_id)
        assert not model_calls
        assert db_conn.execute(
            "SELECT status,expires_at,rejection_reason,relay_degraded_reason "
            "FROM agent_impersonations WHERE id=%s",
            (requested["id"],),
        ).fetchone() == (
            "active",
            expiry,
            None,
            "executor liveness unknown; original lease TTL remains authoritative",
        )
        assert db_conn.execute(
            "SELECT i.status,m.delivery_attempts,m.acknowledged_at,m.last_delivery_at "
            "FROM inbound_messages i JOIN agent_impersonation_messages m ON m.inbound_id=i.id "
            "WHERE i.id=%s AND m.lease_id=%s",
            (pending, requested["id"]),
        ).fetchone() == ("pending", 0, None, None)
        db_conn.commit()
    assert [
        row["id"] for row in leases.inbox(database, requested["id"], attested_caller(requested))
    ] == [pending]


async def test_successor_graph_stays_parked_and_resumes_preserved_pending_input(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    handoff_clients: ClientSet,
    database_gate: ProcessDbGate,
) -> None:
    """A newly admitted runtime reads the durable takeover before doing native work."""
    from dataclasses import replace

    from base.agents.impersonation import history

    graph, saver, ctx, config, reset, owner, requested, model_calls = await _prepare_graph(
        db_conn,
        aops_pool,
        monkeypatch,
        automatic=True,
        config_authority=config_authority,
        clients=handoff_clients,
        database_gate=database_gate,
    )
    monkeypatch.setattr(impersonation, "establish_relay", _relay_ready)
    monkeypatch.setattr(impersonation, "_provider_anchor_states", Mock(return_value=["alive"]))

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
    pending = _deliver_peers_and_ack_first(db_conn, database, event_bus, requested, owner.agent_id)
    expiry = leases.require_active(database, requested["id"], attested_caller(requested))[
        "expires_at"
    ]
    db_conn.execute(
        "UPDATE agents_meta SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
        (owner.agent_id,),
    )
    db_conn.commit()
    successor = await admit_hosted_runtime(
        aops_pool,
        owner.agent_id,
        machine_name(),
        uuid4(),
        expected_from="running",
        db=database,
    )
    assert successor is not None and successor.generation != owner.generation
    with pytest.raises(leases.ImpersonationError, match="no longer owns"):
        leases.native_status(database, event_bus, owner.agent_id, owner)
    replacement_ctx = replace(ctx, relays=RelaySupervision())
    await graph.ainvoke(
        reset,
        config,
        context=replace(
            replacement_ctx, original_incarnation=successor, hosted_resources=None, native_work=None
        ),
    )
    await flush_checkpoint(saver, owner.agent_id)
    assert not model_calls
    assert db_conn.execute(
        "SELECT status,expires_at,accepted_generation,accepted_owner "
        "FROM agent_impersonations WHERE id=%s",
        (requested["id"],),
    ).fetchone() == ("active", expiry, successor.generation, successor.owner)
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (pending,)
    ).fetchone() == ("pending",)
    db_conn.commit()
    leases.release(
        database, event_bus, requested["id"], attested_caller(requested), "Successor handoff"
    )
    assert not await settle_checkpoint(
        graph,
        database,
        event_bus,
        owner.agent_id,
        replacement_ctx.relays,
        incarnation=successor,
        resources=None,
        notes=_notes(),
    )
    resumed = await graph.ainvoke(
        reset,
        config,
        context=replace(
            replacement_ctx, original_incarnation=successor, hosted_resources=None, native_work=None
        ),
    )
    await flush_checkpoint(saver, owner.agent_id)
    assert len(model_calls) == 1
    _assert_successor_handoff_preserves_unacknowledged_input(resumed, tmp_path)
    assert db_conn.execute(
        "SELECT runtime_generation,runtime_owner FROM agents_meta WHERE id=%s",
        (owner.agent_id,),
    ).fetchone() == (successor.generation, successor.owner)


async def test_late_ack_after_delivery_budget_exhaustion_keeps_native_parked(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    handoff_clients: ClientSet,
    database_gate: ProcessDbGate,
) -> None:
    """Delivery exhaustion limits pushes, but does not revoke a valid controller's receipt."""
    graph, saver, ctx, config, reset, owner, requested, model_calls = await _prepare_graph(
        db_conn,
        aops_pool,
        monkeypatch,
        automatic=True,
        config_authority=config_authority,
        clients=handoff_clients,
        database_gate=database_gate,
    )
    monkeypatch.setattr(impersonation, "establish_relay", _relay_ready)
    monkeypatch.setattr(impersonation, "_provider_anchor_states", Mock(return_value=["alive"]))
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
    pending = _deliver_peers_and_ack_first(db_conn, database, event_bus, requested, owner.agent_id)
    token = str(uuid4())
    active = leases.provision_relay(database, requested["id"], owner, token)
    assert active is not None
    for _ in range(active["max_delivery_attempts"]):
        assert pending in {
            row["id"] for row in leases.relay_inbox(database, requested["id"], token)
        }
        assert delivery.reserve_delivery(
            database, event_bus, requested["id"], token, [pending]
        ) == {pending}
        # Advance the recorded window, not the attempt count; reserve is the real writer.
        db_conn.execute(
            "UPDATE agent_impersonation_messages SET last_delivery_at="
            "clock_timestamp()-%s*interval '1 second' WHERE lease_id=%s AND inbound_id=%s",
            (active["ack_window_seconds"] + 1, requested["id"], pending),
        )
        db_conn.commit()
    assert not delivery.reserve_delivery(database, event_bus, requested["id"], token, [pending])
    await graph.ainvoke(
        reset,
        config,
        context=replace(ctx, original_incarnation=owner, hosted_resources=None, native_work=None),
    )
    assert not model_calls
    leases.ack(database, event_bus, requested["id"], attested_caller(requested), [pending])
    assert leases.relay_inbox(database, requested["id"], token) == []
    assert db_conn.execute(
        "SELECT i.status,m.delivery_attempts,m.acknowledged_at IS NOT NULL "
        "FROM inbound_messages i JOIN agent_impersonation_messages m ON m.inbound_id=i.id "
        "WHERE i.id=%s AND m.lease_id=%s",
        (pending, requested["id"]),
    ).fetchone() == ("done", active["max_delivery_attempts"], True)
    db_conn.commit()
    lease = leases.require_active(database, requested["id"], attested_caller(requested))
    assert lease["expires_at"] == active["expires_at"]
    await graph.ainvoke(
        reset,
        config,
        context=replace(ctx, original_incarnation=owner, hosted_resources=None, native_work=None),
    )
    assert not model_calls


async def test_end_note_resumes_an_empty_queue(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    handoff_clients: ClientSet,
    database_gate: ProcessDbGate,
) -> None:
    """A release with nothing queued must still run the end note's first turn.

    Regression: the end-of-session note is a system note, so a window that never
    carried a real exchange reports has_conversation() == False and the claim
    idled out with the note unprocessed. The note is the resumed input: the claim
    runs before_llm with an empty queue, and delivery publishes a wake.
    """
    from base.agents.impersonation import history as history

    graph, saver, ctx, config, reset, owner, requested, model_calls = await _prepare_graph(
        db_conn,
        aops_pool,
        monkeypatch,
        automatic=True,
        config_authority=config_authority,
        clients=handoff_clients,
        database_gate=database_gate,
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
    await graph.ainvoke(
        reset,
        config,
        context=replace(ctx, original_incarnation=owner, hosted_resources=None, native_work=None),
    )
    assert not model_calls  # No native model acceptance turn.
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
        "External work complete",
    )
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
    assert not model_calls
    assert wakes == [(owner.agent_id, "impersonation")]

    resumed = await graph.ainvoke(
        reset,
        config,
        context=replace(ctx, original_incarnation=owner, hosted_resources=None, native_work=None),
    )
    await flush_checkpoint(saver, owner.agent_id)
    assert len(model_calls) == 1
    note = model_calls[0].messages[-1]
    assert note.id == f"impersonation-handoff:{owner.agent_id}:0"
    assert note.additional_kwargs["ava_note_tag"] == "impersonation"
    _assert_resume_note_delivery_contract(note.content)
    # The note is consumed once: another pass finds an idle agent, not a resume.
    await graph.ainvoke(
        reset,
        config,
        context=replace(ctx, original_incarnation=owner, hosted_resources=None, native_work=None),
    )
    assert len(model_calls) == 1
    assert resumed["impersonation_handoff_id"] == f"{owner.agent_id}:0"


async def test_acknowledged_but_unfinished_input_reaches_the_resumed_native(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    handoff_clients: ClientSet,
    database_gate: ProcessDbGate,
) -> None:
    """Task #5010: an ACK acknowledges the message, not the work — input the executor
    received and never finished survives expiry into the record and the resume note."""
    from base.agents.impersonation import history as history

    graph, saver, ctx, config, reset, owner, requested, model_calls = await _prepare_graph(
        db_conn,
        aops_pool,
        monkeypatch,
        automatic=True,
        config_authority=config_authority,
        clients=handoff_clients,
        database_gate=database_gate,
    )
    monkeypatch.setattr(impersonation, "establish_relay", _relay_ready)
    monkeypatch.setattr(history, "workspace_dir", Mock(return_value=tmp_path))
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
    inbound_id = insert_inbound_message(
        db_conn,
        owner.agent_id,
        "Rebuild the report; resume from the failing case",
        source="user",
        bus=event_bus,
        database=database,
    )
    db_conn.commit()
    leases.inbox(database, requested["id"], attested_caller(requested))
    leases.ack(database, event_bus, requested["id"], attested_caller(requested), [inbound_id])
    # Receipt is recorded; the executor dies before finishing the work.
    _end_external_session(db_conn, database, event_bus, requested, "expire")
    await graph.ainvoke(
        reset,
        config,
        context=replace(ctx, original_incarnation=owner, hosted_resources=None, native_work=None),
    )
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
    await graph.ainvoke(
        reset,
        config,
        context=replace(ctx, original_incarnation=owner, hosted_resources=None, native_work=None),
    )
    await flush_checkpoint(saver, owner.agent_id)
    assert len(model_calls) == 1
    note = model_calls[0].messages[-1]
    assert "Review the summary and incoming requests" in note.content
    assert "Continue any requests whose completion is not established" in note.content
    document = json.loads((tmp_path / "impersonation" / "0.json").read_text())
    message = next(m for m in document["messages"] if m["payload"]["content"].startswith("Rebuild"))
    assert message["acknowledged"] is True
