"""Real PostgreSQL + compiled graph + exec child cooperative handoff."""

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent import impersonation, impersonation_handoff
from agent.impersonation import settle_checkpoint
from agent.impersonation_handoff import flush_checkpoint
from agent.tests.impersonation.handoff_inputs import (
    assert_consent_tool_result_checkpointed,
    assert_end_note_and_handoff_document,
    assert_handoff_precedes_queued_input,
    assert_resumed_transcript,
    deliver_peers_and_ack_first,
    end_external_session,
    executor_acks_says_and_releases,
    handoff_notes,
    prepare_graph,
    relay_ready,
)
from agent.tests.impersonation.handoff_inputs import handoff_clients as handoff_clients
from ava.external.state import encode_plugin_delta
from base.agents import impersonation as leases
from base.agents.context.clients import ClientSet
from base.agents.inbox.insert import insert_inbound_message
from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.native_process.turn_identity import HostedTurnResources
from tests.fixtures.pin_agent import hosted_resources as hosted_resources
from tests.impersonation_support import attested_caller


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
    handoff_clients: ClientSet,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    scope = hosted_resources
    # The real exec child boots this installed unit, whose identity is file-owned.
    config_authority.env_path.write_text(
        f"AVA_MACHINE_NAME={machine_identity.name()}\n", encoding="utf-8"
    )
    graph, saver, ctx, config, reset, owner, requested, model_calls = await prepare_graph(
        db_conn,
        aops_pool,
        monkeypatch,
        config_authority=config_authority,
        clients=handoff_clients,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    agent_id = owner.agent_id

    # The relay establishment gate is unit-tested separately; the real graph
    # handoff must not spawn a relay process in the test environment. The
    # recorded controller tree is synthetic (its pids are not live processes),
    # so pin the supervisor's liveness view (task #3998).

    def relay_ready(*_args: object, read_local_machine: Callable[[], str]) -> bool:
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
        leases.get(
            database,
            event_bus,
            requested["id"],
            attested_caller(
                requested,
            ),
            local_machine=machine_identity.name(),
        )["status"]
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
        notes=handoff_notes(),
        read_local_machine=machine_identity.name,
    )
    assert (
        leases.require_active(
            database,
            requested["id"],
            attested_caller(
                requested,
            ),
            local_machine=machine_identity.name(),
        )["status"]
        == "active"
    )
    # The stubbed establishment writes no heartbeat; keep it fresh so the
    # held pass stays a no-op while this flow runs (task #3998).
    db_conn.execute(
        "UPDATE agent_impersonations SET relay_heartbeat_at=clock_timestamp() WHERE id=%s",
        (requested["id"],),
    )
    db_conn.commit()
    await assert_consent_tool_result_checkpointed(saver, config)
    second_peer = deliver_peers_and_ack_first(
        db_conn, database, event_bus, requested, agent_id, machine_identity=machine_identity
    )
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
        local_machine=machine_identity.name(),
    )
    end_external_session(
        db_conn, database, event_bus, requested, finish, machine_identity=machine_identity
    )
    assert not await settle_checkpoint(
        graph,
        database,
        event_bus,
        agent_id,
        ctx.relays,
        incarnation=owner,
        resources=scope,
        notes=handoff_notes(),
        read_local_machine=machine_identity.name,
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
    assert_resumed_transcript(resumed, finish)
    # A second resume-boundary pass cannot double an additive reducer.
    assert not await settle_checkpoint(
        graph,
        database,
        event_bus,
        agent_id,
        ctx.relays,
        incarnation=owner,
        resources=scope,
        notes=handoff_notes(),
        read_local_machine=machine_identity.name,
    )
    assert (await graph.aget_state(config)).values["handoff__total"] == 7


async def test_automatic_takeover_handoff_precedes_queued_input(
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
    machine_identity: MachineIdentity,
) -> None:
    from base.agents.impersonation import history as history

    graph, saver, ctx, config, reset, owner, requested, model_calls = await prepare_graph(
        db_conn,
        aops_pool,
        monkeypatch,
        automatic=True,
        config_authority=config_authority,
        clients=handoff_clients,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    monkeypatch.setattr(impersonation, "establish_relay", relay_ready)

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
        leases.get(
            database,
            event_bus,
            requested["id"],
            attested_caller(
                requested,
            ),
            local_machine=machine_identity.name(),
        )["status"]
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
        notes=handoff_notes(),
        read_local_machine=machine_identity.name,
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
    executor_acks_says_and_releases(
        database, event_bus, requested, inbound_id, machine_identity=machine_identity
    )
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
        notes=handoff_notes(),
        read_local_machine=machine_identity.name,
    )
    snapshot = await graph.aget_state(config)
    assert_end_note_and_handoff_document(snapshot.values["messages"][-1], tmp_path)
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
        notes=handoff_notes(),
        read_local_machine=machine_identity.name,
    )
    await graph.ainvoke(
        reset,
        config,
        context=replace(ctx, original_incarnation=owner, hosted_resources=None, native_work=None),
    )
    assert len(model_calls) == 1
    assert_handoff_precedes_queued_input(model_calls[0].messages)


async def test_accepted_session_repairs_missing_start_checkpoint(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    handoff_clients: ClientSet,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    graph, saver, ctx, config, _reset, owner, requested, calls = await prepare_graph(
        db_conn,
        aops_pool,
        monkeypatch,
        automatic=True,
        config_authority=config_authority,
        clients=handoff_clients,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    monkeypatch.setattr(impersonation, "establish_relay", relay_ready)
    # Emulate a committed acceptance followed by a crash before claim's update.
    leases.accept(
        database,
        event_bus,
        requested["id"],
        owner.agent_id,
        owner,
        "Saved request, lost checkpoint",
        local_machine=machine_identity.name(),
    )
    assert await settle_checkpoint(
        graph,
        database,
        event_bus,
        owner.agent_id,
        ctx.relays,
        incarnation=owner,
        resources=None,
        notes=handoff_notes(),
        read_local_machine=machine_identity.name,
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
        leases.get(
            database,
            event_bus,
            requested["id"],
            attested_caller(
                requested,
            ),
            local_machine=machine_identity.name(),
        )["status"]
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
    handoff_clients: ClientSet,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    from agent.impersonation_handoff import deliver_handoff
    from base.agents.impersonation import history as history

    graph, saver, ctx, config, reset, owner, requested, calls = await prepare_graph(
        db_conn,
        aops_pool,
        monkeypatch,
        automatic=True,
        config_authority=config_authority,
        clients=handoff_clients,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    monkeypatch.setattr(impersonation, "establish_relay", relay_ready)

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
        notes=handoff_notes(),
        read_local_machine=machine_identity.name,
    )
    leases.release(
        database,
        event_bus,
        requested["id"],
        attested_caller(requested),
        "Done; please continue",
        local_machine=machine_identity.name(),
    )
    lease = history.resolve(database, owner.agent_id, 0, local_machine=machine_identity.name())

    async def failed_flush(*_: object) -> None:
        raise OSError("checkpoint temporarily unavailable")

    monkeypatch.setattr(impersonation_handoff, "flush_checkpoint", failed_flush)
    with pytest.raises(OSError, match="checkpoint temporarily"):
        await deliver_handoff(graph, database, event_bus, lease, owner, notes=handoff_notes())
    assert (
        history.resolve(database, owner.agent_id, 0, local_machine=machine_identity.name())[
            "handoff_applied_at"
        ]
        is None
    )
    assert not calls
    monkeypatch.setattr(impersonation_handoff, "flush_checkpoint", flush_checkpoint)
    await deliver_handoff(graph, database, event_bus, lease, owner, notes=handoff_notes())
    durable = await saver.aget_tuple(config)
    assert durable is not None
    notes = [
        m
        for m in durable.checkpoint["channel_values"]["messages"]
        if m.id == f"impersonation-handoff:{owner.agent_id}:0"
    ]
    assert len(notes) == 1
    assert (
        history.resolve(database, owner.agent_id, 0, local_machine=machine_identity.name())[
            "handoff_applied_at"
        ]
        is not None
    )


async def test_handoff_of_a_released_log_native_lease_is_already_complete(
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
    machine_identity: MachineIdentity,
) -> None:
    """With every source sealed at release, the event log is complete before delivery."""
    from agent.impersonation_handoff import deliver_handoff
    from base.agents.impersonation import history as history

    graph, saver, ctx, config, reset, owner, requested, _calls = await prepare_graph(
        db_conn,
        aops_pool,
        monkeypatch,
        automatic=True,
        config_authority=config_authority,
        clients=handoff_clients,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    monkeypatch.setattr(impersonation, "establish_relay", relay_ready)

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
        notes=handoff_notes(),
        read_local_machine=machine_identity.name,
    )
    leases.release(
        database,
        event_bus,
        requested["id"],
        attested_caller(requested),
        "Done; please continue",
        local_machine=machine_identity.name(),
    )
    lease = history.resolve(database, owner.agent_id, 0, local_machine=machine_identity.name())
    await deliver_handoff(graph, database, event_bus, lease, owner, notes=handoff_notes())

    landed = history.resolve(database, owner.agent_id, 0, local_machine=machine_identity.name())
    assert landed["handoff_path"] is not None
    assert landed["handoff_applied_at"] is not None
    assert landed["events_completed_at"] is not None
    assert landed["event_delivery_pending_reason"] is None
    assert landed["handoff_document"]["statistics"]["event_delivery"]["state"] == "complete"
    assert '"state": "complete"' in (tmp_path / "impersonation" / "0.json").read_text()
