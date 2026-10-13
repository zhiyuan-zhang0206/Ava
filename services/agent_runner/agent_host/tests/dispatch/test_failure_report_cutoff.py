"""A retry hint is published only after the original hosted turn's durable cutoff."""

import asyncio
from functools import partial
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import psycopg
import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool
from redis.asyncio.client import PubSub

from agent.graph import build_graph
from agent.graph.llm_errors import FatalProviderError
from agent.ownership.hosted import TurnFatalStamp, TurnSettlement, admit_hosted_runtime
from agent.state import BaseAgentState
from agent.turn.runloop import PendingTurnFailure, publish_turn_failure
from base.agents.context import AvaContext
from base.agents.recovery.breaker import admit_pending_wake
from base.agents.tests.units import spawn_agent
from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.projection import Error
from base.events.live.publisher import AgentEventPublisher
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.packages.plugins.extensions import ExtensionRegistry
from services.agent_runner.agent_host import settlement
from services.agent_runner.agent_host.invocation import finish_pending_failure
from services.agent_runner.agent_host.runtime import TurnOutcome
from services.agent_runner.agent_host.tests.host_policy import configured_policy
from tests.fixtures.unit.identity import machine_identity as machine_identity


def _failure() -> PendingTurnFailure:
    return PendingTurnFailure(
        FatalProviderError("expected refusal", error_class="permanent", provider="test", status=402)
    )


def _insert(conn: psycopg.Connection, agent_id: int, source: str, *, claimed: bool = False) -> int:
    row = conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source,status) "
        "VALUES(%s,'new explicit input','chat',%s,%s) RETURNING id",
        (agent_id, source, "claimed" if claimed else "pending"),
    ).fetchone()
    conn.commit()
    assert row is not None
    return row[0]


async def _close(
    pool: AsyncConnectionPool,
    bus: EventBus,
    incarnation: RuntimeIncarnation,
    outcome: TurnOutcome,
) -> None:
    await settlement.close_hosted_turn(pool, pool, bus, incarnation, outcome, resources=None)


async def _admit(
    conn: psycopg.Connection,
    pool: AsyncConnectionPool,
    database: Database,
    catalog: ModelCatalog,
    authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    local_machine: MachineIdentity,
) -> RuntimeIncarnation:
    agent_id = spawn_agent(
        catalog=catalog, authority=authority, database_gate=database_gate, machine=local_machine
    )
    machine = conn.execute("SELECT machine FROM agents_meta WHERE id=%s", (agent_id,)).fetchone()
    assert machine is not None
    incarnation = await admit_hosted_runtime(
        pool, agent_id, machine[0], uuid4(), db=database, expected_from="idling"
    )
    assert incarnation is not None
    return incarnation


async def test_error_visible_after_cutoff_allows_immediate_new_chat(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    database: Database,
    db_url: str,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    incarnation = await _admit(
        db_conn,
        aops_pool,
        database,
        model_catalog,
        config_authority,
        database_gate,
        machine_identity,
    )
    agent_id = incarnation.agent_id
    old_claim = _insert(db_conn, agent_id, "user", claimed=True)
    pending = _failure()
    publisher = AgentEventPublisher(event_bus.async_redis(), event_bus.channel, agent_id=agent_id)
    ctx = AvaContext(
        ops_pool=aops_pool,
        event_publisher=publisher,
        bus=event_bus,
        db=database,
        catalog=model_catalog,
        agent=AgentSlices.resolve(default_reader=configured_policy().default_reader),
        original_incarnation=incarnation,
    )
    received: list[Error] = []
    new_chats: list[int] = []
    async with AsyncPostgresSaver.from_conn_string(db_url) as saver:
        graph = build_graph(saver, ExtensionRegistry())
        config: RunnableConfig = {"configurable": {"thread_id": str(agent_id)}}
        async with asyncio.TaskGroup() as workers:
            await publisher.start(workers)
            initial_turn = graph.with_types(input_type=BaseAgentState, output_type=BaseAgentState)
            await initial_turn.ainvoke(
                BaseAgentState(halted=True), config, context=ctx, interrupt_before=["init_context"]
            )
            outcome = await finish_pending_failure(graph, saver, agent_id, ctx, config, pending)
            assert len(pending.errors) == 2
            outcome = await finish_pending_failure(graph, saver, agent_id, ctx, config, pending)
            assert len(pending.errors) == 2
            assert not await admit_pending_wake(aops_pool, agent_id)
            await publisher.aclose()
        async with (
            event_bus.open_async_redis() as client,
            PubSub(client.connection_pool) as subscription,
        ):
            await subscription.subscribe(event_bus.channel)

            async def react_to_error() -> None:
                async with asyncio.timeout(5):
                    while len(received) < 2:
                        message = await subscription.get_message(
                            ignore_subscribe_messages=True, timeout=1
                        )
                        if message is None:
                            continue
                        payload = message["data"]
                        if not isinstance(payload, str) or '"role":"error"' not in payload:
                            continue
                        received.append(Error.model_validate_json(payload))
                        if len(received) == 1:
                            row = db_conn.execute(
                                "SELECT status,last_turn_fatal_at FROM agents_meta WHERE id=%s",
                                (agent_id,),
                            ).fetchone()
                            assert row is not None and row[0] == "idling" and row[1] is not None
                            # Off the event loop: the settlement's consume pass may hold the
                            # agent's inbound-owner lock while this insert waits for it.
                            await asyncio.to_thread(_insert, db_conn, agent_id, "system:heartbeat")
                            assert not await admit_pending_wake(aops_pool, agent_id)
                            new_chats.append(
                                await asyncio.to_thread(_insert, db_conn, agent_id, "user")
                            )
                            assert await admit_pending_wake(aops_pool, agent_id)

            async with asyncio.TaskGroup() as reactions:
                reactions.create_task(react_to_error())
                await _close(aops_pool, event_bus, incarnation, outcome)
            await _close(aops_pool, event_bus, incarnation, outcome)
    assert len(received) == 2 and all(error.blocked and error.status == 402 for error in received)
    assert pending.errors == [] and outcome.failure_report is None
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (old_claim,)
    ).fetchone() == ("done",)
    row = db_conn.execute(
        "SELECT i.status,i.created_at>m.last_turn_fatal_at FROM inbound_messages i "
        "JOIN agents_meta m ON m.id=i.agent_id WHERE i.id=%s",
        (new_chats[0],),
    ).fetchone()
    assert row == ("pending", True)
    assert isinstance(pending.error, FatalProviderError)


@pytest.mark.parametrize("applied,settled", [(False, True), (True, False), (False, False)])
async def test_unproven_settlement_never_publishes_retry_instructions(
    monkeypatch: pytest.MonkeyPatch,
    applied: bool,
    settled: bool,
    loguru_records: list[dict[str, object]],
) -> None:
    monkeypatch.setattr(
        settlement,
        "settle_and_stamp_turn",
        AsyncMock(return_value=TurnSettlement(TurnFatalStamp(applied), settled)),
    )
    report = AsyncMock()
    outcome = TurnOutcome(exited=False, crashed=True, failure_report=report)
    await _close(Mock(), Mock(), RuntimeIncarnation(42, uuid4(), uuid4()), outcome)
    report.assert_not_awaited()
    assert outcome.failure_report is report
    assert any("turn Error withheld" in str(row["message"]) for row in loguru_records)


@pytest.mark.parametrize("fault", ["unknown", "unavailable", "cancel"])
async def test_publication_failure_is_observable_and_never_repeated(
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    loguru_records: list[dict[str, object]],
) -> None:
    bus = Mock(spec=EventBus)
    original = ValueError("publication failed") if fault == "unknown" else asyncio.CancelledError()
    bus.publish_best_effort = AsyncMock(
        side_effect=original if fault != "unavailable" else None, return_value=None
    )
    pending = _failure()
    pending.errors.append(Error(agent_id=42, content="original provider refusal", blocked=True))
    outcome = TurnOutcome(
        exited=False, crashed=True, failure_report=partial(publish_turn_failure, pending, bus)
    )
    monkeypatch.setattr(
        settlement,
        "settle_and_stamp_turn",
        AsyncMock(return_value=TurnSettlement(TurnFatalStamp(True), True)),
    )
    if fault == "cancel":
        with pytest.raises(asyncio.CancelledError) as caught:
            await _close(Mock(), bus, RuntimeIncarnation(42, uuid4(), uuid4()), outcome)
        assert caught.value is original
    else:
        await _close(Mock(), bus, RuntimeIncarnation(42, uuid4(), uuid4()), outcome)
    await _close(Mock(), bus, RuntimeIncarnation(42, uuid4(), uuid4()), outcome)
    if fault != "cancel":
        assert any("Error for agent 42" in str(row["message"]) for row in loguru_records)
    assert bus.publish_best_effort.await_count == 1
    assert outcome.failure_report is None and pending.errors == []
    assert isinstance(pending.error, FatalProviderError)


async def test_changed_incarnation_withholds_original_failure_report(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    database: Database,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    incarnation = await _admit(
        db_conn,
        aops_pool,
        database,
        model_catalog,
        config_authority,
        database_gate,
        machine_identity,
    )
    replacement = uuid4()
    db_conn.execute(
        "UPDATE agents_meta SET runtime_generation=%s WHERE id=%s",
        (replacement, incarnation.agent_id),
    )
    db_conn.commit()
    report = AsyncMock()
    outcome = TurnOutcome(exited=False, crashed=True, failure_report=report)
    await _close(aops_pool, event_bus, incarnation, outcome)
    report.assert_not_awaited()
    assert outcome.failure_report is report
    assert db_conn.execute(
        "SELECT runtime_generation,status,last_turn_fatal_at FROM agents_meta WHERE id=%s",
        (incarnation.agent_id,),
    ).fetchone() == (replacement, "running", None)
