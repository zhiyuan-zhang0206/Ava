"""Host cold startup restores durable watcher intent through real PTY sessions."""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import psycopg
import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import SystemMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.graph._claim import claim_node
from agent.state import BaseAgentState
from ava import _watcher_reconcile, watcher
from ava.shell import sessions
from ops.agent_pause import resume_agents
from services.agent_host.daemon import _schedule_watcher_recovery
from services.agent_host.dispatcher import InboundWakeDispatcher, PendingInboundWake, TurnScheduler
from services.agent_host.host import AgentHost
from shared import pause_owner
from shared.cluster import inbound_channel
from shared.config import settings
from shared.context import AvaContext
from shared.machine import machine_name
from shared.maintenance_state import MaintenanceHold
from shared.platform import IS_WINDOWS
from shared.redis_client import sync_redis
from shared.watcher import TEMPLATE_VERSION
from shared.watcher_registry import register_watcher, watcher_rows

pytestmark = [
    pytest.mark.skipif(IS_WINDOWS, reason="Real POSIX PTY session fixture"),
    pytest.mark.usefixtures("_pty_sessions_env"),
]


async def _settled(scheduler: TurnScheduler) -> None:
    async with asyncio.timeout(15):
        while scheduler.active_agents:
            await asyncio.sleep(0.01)


@pytest.fixture
async def cold_host(
    _agent_row: int,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[int, AgentHost, TurnScheduler]]:
    agent_id = _agent_row
    db_conn.execute(
        "UPDATE agents_meta SET machine=%s,status='idling',config_overlay=%s WHERE id=%s",
        (machine_name(), Jsonb({"llm_model": "deepseek-v4-pro"}), agent_id),
    )
    db_conn.commit()
    model = FakeListChatModel(responses=[])
    built: list[str] = []

    def model_for(name: str) -> FakeListChatModel:
        built.append(name)
        return model

    monkeypatch.setattr("agent._process_boot.build_chat_model", model_for)

    def allow_model(*, model: str | None = None) -> None:
        assert model is not None

    monkeypatch.setattr("services.agent_host.host.validate_model_config", allow_model)
    # A missed-watcher alert normally crosses the gateway HTTP boundary. Keep
    # this test local; the durable missed status and addressed alert are asserted.
    monkeypatch.setattr(_watcher_reconcile, "_notify_missed_watcher", MagicMock())

    def no_llm(state: BaseAgentState) -> dict[str, object]:
        raise AssertionError("an empty-inbox watcher recovery must spend no LLM call")

    builder = StateGraph(BaseAgentState, context_schema=AvaContext)
    builder.add_node("claim", claim_node)  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
    builder.add_node("before_llm", no_llm)  # pyright: ignore[reportUnknownMemberType]
    builder.add_node("init_context", no_llm)  # pyright: ignore[reportUnknownMemberType]
    builder.add_edge(START, "claim")
    builder.add_edge("before_llm", END)
    builder.add_edge("init_context", END)
    async with AsyncPostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
        graph = builder.compile(checkpointer=saver)  # pyright: ignore[reportUnknownMemberType]
        await graph.aupdate_state(
            {"configurable": {"thread_id": str(agent_id)}},
            {"messages": [SystemMessage(content="watcher recovery test")], "halted": True},
        )
        host = AgentHost(pool=aops_pool, checkpointer=saver, graph=graph, machine=machine_name())
        scheduler = TurnScheduler(host.run_turn)
        try:
            yield agent_id, host, scheduler
            assert built and set(built) == {"deepseek-v4-pro"}
            assert host.stats.turns_started > 0
        finally:
            await scheduler.aclose()
            await host.aclose()


def _missing(agent_id: int, kind: str) -> None:
    now = datetime.now(UTC)
    register_watcher(
        agent_id,
        424242,
        kind="at" if kind.startswith("at-") else kind,
        name="recover-watcher",
        message="future test reminder",
        cron_expr="0 0 1 1 *" if kind == "cron" else None,
        cron_timezone="UTC" if kind == "cron" else None,
        fires_at=now + (timedelta(days=2) if kind == "at-future" else -timedelta(days=2))
        if kind.startswith("at-")
        else None,
        template_version=TEMPLATE_VERSION,
        generation=sessions._current_session_generation(),
    )


@pytest.mark.parametrize("kind", ["cron", "at-future", "at-past", "launch"])
async def test_idle_empty_inbox_boot_reconciles_missing_watcher(
    cold_host: tuple[int, AgentHost, TurnScheduler],
    db_conn: psycopg.Connection,
    kind: str,
) -> None:
    agent_id, host, scheduler = cold_host
    _missing(agent_id, kind)
    assert sessions.list() == {}
    assert await host.pending_inbound_wakes(30) == []
    await _schedule_watcher_recovery(host, scheduler)
    await _settled(scheduler)
    rows = watcher_rows(agent_id)
    original = next(row for row in rows if row["session_id"] == 424242)
    if kind in ("cron", "at-future"):
        assert original["status"] == "rebuilt"
        live = [row for row in rows if row["status"] == "running"]
        assert len(live) == 1
        assert live[0]["session_id"] in sessions.list()
    else:
        assert original["status"] == "missed"
        assert sessions.list() == {}
        cast(MagicMock, _watcher_reconcile._notify_missed_watcher).assert_called_once()
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent_id,)
    ).fetchone() == (0,)
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("idling",)


async def test_cold_host_preserves_live_watcher_after_pause(
    cold_host: tuple[int, AgentHost, TurnScheduler],
) -> None:
    agent_id, host, scheduler = cold_host
    session_id = await asyncio.to_thread(
        watcher.cron, "0 0 1 1 *", "live reminder", timezone="UTC", name="preserved"
    )
    before = watcher_rows(agent_id)
    assert session_id in sessions.list()
    await _schedule_watcher_recovery(host, scheduler)
    await _settled(scheduler)
    host.drop_agent(agent_id)
    await host.run_turn(agent_id)
    assert watcher_rows(agent_id) == before
    assert sessions.list() == {session_id: "preserved"}


async def test_failed_watcher_recovery_retries_cached_runtime_without_inbound(
    cold_host: tuple[int, AgentHost, TurnScheduler], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id, host, scheduler = cold_host
    _missing(agent_id, "cron")
    cron = _watcher_reconcile.cron
    attempts = 0

    def fail_once(*args: Any, **kwargs: Any) -> int:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("test PTY service unavailable during boot")
        return cron(*args, **kwargs)

    monkeypatch.setattr(_watcher_reconcile, "cron", fail_once)
    await _schedule_watcher_recovery(host, scheduler)
    await _settled(scheduler)
    assert sessions.list() == {}
    assert watcher_rows(agent_id)[0]["status"] == "running"
    pending = await host.pending_inbound_wakes(30)
    assert [wake.agent_id for wake in pending] == [agent_id]
    for wake in pending:
        scheduler.wake(wake.agent_id)
    await _settled(scheduler)
    assert attempts == 2
    assert host.stats.cache_misses == 1
    assert host.stats.cache_hits == 1
    assert len(sessions.list()) == 1
    assert await host.pending_inbound_wakes(30) == []


@pytest.mark.parametrize("resume_before_subscribe", [False, True])
async def test_held_start_then_resume_restores_parked_watcher(
    cold_host: tuple[int, AgentHost, TurnScheduler],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resume_before_subscribe: bool,
) -> None:
    agent_id, host, scheduler = cold_host
    _missing(agent_id, "cron")
    monkeypatch.setattr(pause_owner, "state_path", lambda: tmp_path / "pause-owner.json")
    at = datetime.now(UTC)
    initial = pause_owner.begin_maintenance("watcher-test", at)
    assert initial.maintenance is not None
    pause_owner.change_maintenance(
        "watcher-test", at, initial.maintenance, MaintenanceHold("starting", parked=(agent_id,))
    )
    await _schedule_watcher_recovery(host, scheduler)
    await _settled(scheduler)
    assert host.stats.cache_misses == 0
    assert sessions.list() == {}
    subscribed = asyncio.Event()

    async def pending(stale_after_s: float) -> list[PendingInboundWake]:
        subscribed.set()
        return await host.pending_inbound_wakes(stale_after_s)

    dispatcher = InboundWakeDispatcher(
        settings.data_plane.redis_url,
        scheduler,
        pending_scan=pending,
        stale_after_s=30,
        scan_interval_s=0.05,
        subscription_read_timeout_s=5,
    )
    if resume_before_subscribe:
        await asyncio.to_thread(resume_agents)
        # Pub/sub has no replay: this real hint reaches zero subscribers. The
        # pending boot workset, not a delivered hint or inbound row, must recover.
        # redis-py's publish stub leaves only its unused **kwargs untyped.
        delivered = await asyncio.to_thread(
            sync_redis().publish,  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            inbound_channel(agent_id),
            "maintenance",
        )
        assert delivered == 0
    task = asyncio.create_task(dispatcher.run())
    try:
        await asyncio.wait_for(subscribed.wait(), 5)
        if not resume_before_subscribe:
            await asyncio.to_thread(resume_agents)
        async with asyncio.timeout(15):
            while not sessions.list():
                await asyncio.sleep(0.02)
        await _settled(scheduler)
        assert await host.pending_inbound_wakes(30) == []
        assert pause_owner.read().status != "paused"
        assert len([row for row in watcher_rows(agent_id) if row["status"] == "running"]) == 1
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
