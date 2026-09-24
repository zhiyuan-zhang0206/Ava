"""Hosted idle recovery and reaper successor marker lifecycle."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import psycopg
import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.graph._claim import claim_node
from agent.state import BaseAgentState
from services.agent_host import dispatcher
from services.agent_host import host as host_module
from services.agent_host import runtime as runtime_module
from services.agent_host.dispatcher import InboundWakeDispatcher, TurnScheduler
from services.agent_host.host import AgentHost
from shared import pause_owner
from shared.context import AvaContext
from shared.incarnation_resources import IncarnationResources, ResourceProcess, decode_resources
from shared.maintenance_cohort import _classify, _RuntimeRow
from shared.maintenance_state import MaintenanceHold
from tests.agent.test_hosted_db_recovery import _admit
from tests.shared.poll_until import poll_until_async


def _accept_model(**_kwargs: object) -> None:
    pass


def _unexpected_model(state: BaseAgentState) -> dict[str, object]:
    del state
    pytest.fail("quiet idle recovery reached a model/initialization node")


@pytest.mark.parametrize("evidence", ["legacy", "managed"])
@pytest.mark.parametrize("released", [False, True])
async def test_quiet_idle_predecessor_is_recovered_without_a_model_call(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    evidence: str,
    released: bool,
) -> None:
    incarnation = await _admit(aops_pool)
    agent = incarnation.agent_id
    row = db_conn.execute(
        "SELECT incarnation_resources FROM agents_meta WHERE id=%s", (agent,)
    ).fetchone()
    assert row is not None
    resources = decode_resources(row[0])
    assert isinstance(resources, IncarnationResources)
    assert resources.host_process is not None
    # Same PID, different birth: the predecessor has exited, even if its PID
    # was recycled. No real host or test worker is stopped for this evidence.
    dead = ResourceProcess(pid=resources.host_process.pid, birth=resources.host_process.birth - 60)
    db_conn.execute(
        "UPDATE agents_meta SET status='idling',incarnation_resources=%s,"
        "lease_expires_at=CASE WHEN %s THEN NULL ELSE now()-interval '1s' END WHERE id=%s",
        (
            None
            if evidence == "legacy"
            else Jsonb(resources.model_copy(update={"host_process": dead}).model_dump(mode="json")),
            released,
            agent,
        ),
    )
    db_conn.commit()

    monkeypatch.setattr(runtime_module, "validate_model_config", _accept_model)
    monkeypatch.setattr(
        host_module, "boot_agent_scope", AsyncMock(return_value=FakeListChatModel(responses=[]))
    )

    saver = AsyncPostgresSaver(
        cast(AsyncConnectionPool[psycopg.AsyncConnection[DictRow]], aops_pool)
    )
    await saver.setup()
    builder = StateGraph(BaseAgentState, context_schema=AvaContext)
    builder.add_node("claim", claim_node)  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
    builder.add_node("before_llm", _unexpected_model)  # pyright: ignore[reportUnknownMemberType]
    builder.add_node("init_context", _unexpected_model)  # pyright: ignore[reportUnknownMemberType]
    builder.add_edge(START, "claim")
    builder.add_edge("before_llm", END)
    builder.add_edge("init_context", END)
    graph = builder.compile(checkpointer=saver)  # pyright: ignore[reportUnknownMemberType]
    config: RunnableConfig = {"configurable": {"thread_id": str(agent)}}
    messages = [HumanMessage(content="Existing question"), AIMessage(content="Already answered")]
    await graph.aupdate_state(config, {"messages": messages, "halted": True})
    host = AgentHost(pool=aops_pool, graph=graph, checkpointer=saver)
    scheduler = TurnScheduler(host.run_turn)
    dispatcher = InboundWakeDispatcher(
        "redis://unused", scheduler, pending_scan=host.pending_inbound_wakes, stale_after_s=60
    )
    try:
        assert [wake.agent_id for wake in await host.pending_inbound_wakes(60)] == [agent]
        await dispatcher.scan_once()
        await poll_until_async(lambda: not scheduler.active_agents, timeout=5)
        row = db_conn.execute(
            "SELECT id,status,runtime_kind,runtime_owner,runtime_generation,"
            "lease_expires_at>clock_timestamp(),pid,incarnation_resources "
            "FROM agents_meta WHERE id=%s",
            (agent,),
        ).fetchone()
        assert row is not None
        assert row[1:4] == ("idling", "hosted", host._owner)
        assert row[4] != incarnation.generation and row[5] is True
        assert host.stats.turns_started == 1
        assert await host.pending_inbound_wakes(60) == []
        assert db_conn.execute(
            "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
        ).fetchone() == (0,)
        assert db_conn.execute(
            "SELECT count(*) FROM agent_watchers WHERE agent_id=%s", (agent,)
        ).fetchone() == (0,)
        state = await graph.aget_state(config)
        assert [message.content for message in state.values["messages"]] == [
            message.content for message in messages
        ]
        assert state.values["halted"] is True
        assert _classify([_RuntimeRow(*row)], MaintenanceHold(), host._owner, set()).commands == {
            agent: 0
        }
    finally:
        await scheduler.aclose()
        await host.aclose()


async def test_maintenance_hold_does_not_adopt_a_quiet_foreign_owner(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    incarnation = await _admit(aops_pool)
    agent = incarnation.agent_id
    db_conn.execute(
        "UPDATE agents_meta SET status='idling',lease_expires_at=now()-interval '1s' WHERE id=%s",
        (agent,),
    )
    db_conn.commit()
    host = AgentHost(pool=aops_pool, graph=AsyncMock(), checkpointer=AsyncMock())
    assert [wake.agent_id for wake in await host.pending_inbound_wakes(60)] == [agent]
    before = db_conn.execute("SELECT * FROM agents_meta WHERE id=%s", (agent,)).fetchone()
    monkeypatch.setattr(pause_owner, "state_path", lambda: tmp_path / "pause.json")
    monkeypatch.setattr(pause_owner, "lock_path", lambda: tmp_path / "pause.lock")
    pause_owner.begin_maintenance("test-idle-recovery", datetime.now(UTC))
    assert await host.pending_inbound_wakes(60) == []
    assert db_conn.execute("SELECT * FROM agents_meta WHERE id=%s", (agent,)).fetchone() == before


class TestReapedSuccessorMarker:
    @pytest.mark.parametrize("outcome", ["return", "error", "cancel"])
    async def test_reaped_successor_marker_clears_when_task_finishes(self, outcome: str) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def run_turn(_agent_id: int) -> None:
            entered.set()
            await release.wait()
            if outcome == "error":
                raise ValueError("turn failed")

        scheduler = TurnScheduler(run_turn)
        try:
            original = scheduler.wake(1)
            assert original is not None
            original.cancel()  # Reap before the first pump runs.
            await poll_until_async(lambda: scheduler.reaped_successor(1) is not None, timeout=3)
            successor = scheduler.reaped_successor(1)
            assert successor is not None
            await asyncio.wait_for(entered.wait(), 3)
            assert scheduler.reaped_successor(1) is successor
            if outcome == "cancel":
                successor.cancel()
            else:
                release.set()
            await poll_until_async(
                lambda: successor.done() and scheduler.reaped_successor(1) is None, timeout=3
            )
        finally:
            release.set()
            await scheduler.aclose()

    async def test_old_successor_cleanup_preserves_new_reaper_marker(self) -> None:
        release = asyncio.Event()

        async def run_turn(_agent_id: int) -> None:
            await release.wait()

        async def pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return [dispatcher.PendingInboundWake(1, False, True)]

        scheduler = TurnScheduler(run_turn)
        disp = InboundWakeDispatcher(
            "redis://unused", scheduler, pending_scan=pending, stale_after_s=180.0
        )
        try:
            await disp.scan_once()
            original = disp._recovery_in_flight[1]
            original.cancel()
            await asyncio.sleep(0)  # Run the cancelled original.
            await asyncio.sleep(0)  # Run its reaper, before the replacement starts.
            old_successor = scheduler.reaped_successor(1)
            assert old_successor is not None
            assert disp._recovery_in_flight[1] is old_successor
            old_successor.cancel()  # Reap again before this pump starts.
            await poll_until_async(
                lambda: scheduler.reaped_successor(1) is not old_successor, timeout=3
            )
            new_successor = scheduler.reaped_successor(1)
            assert new_successor is not None
            assert scheduler.task_for(1) is new_successor
            await asyncio.sleep(0)  # Let its recovery-slot callback finish.
            assert scheduler.reaped_successor(1) is new_successor
            assert disp._recovery_in_flight[1] is new_successor
            release.set()
            await poll_until_async(
                lambda: (
                    new_successor.done()
                    and scheduler.reaped_successor(1) is None
                    and disp._recovery_in_flight == {}
                ),
                timeout=3,
            )
        finally:
            release.set()
            await scheduler.aclose()

    async def test_late_original_release_skips_completed_reaper_successor(self) -> None:
        async def run_turn(_agent_id: int) -> None:
            return

        scheduler = TurnScheduler(run_turn)
        disp = InboundWakeDispatcher("redis://unused", scheduler)
        try:
            original = scheduler.wake(1)
            assert original is not None
            disp._recovery_in_flight[1] = original
            original.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            successor = scheduler.reaped_successor(1)
            assert successor is not None
            await poll_until_async(
                lambda: successor.done() and scheduler.reaped_successor(1) is None, timeout=3
            )
            disp._release_recovery_slot(1, original)
            assert disp._recovery_in_flight == {}
        finally:
            await scheduler.aclose()
