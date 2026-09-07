"""Old inbox timestamps cannot kill fresh turns or hide expired owners."""

import asyncio
import subprocess
import sys
from unittest.mock import AsyncMock

import psutil
import psycopg
import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent import _turn_progress as progress
from agent.hosted_ownership import settle_stale_running_rows
from agent.state import AgentState
from services.agent_host import dispatcher as dispatch
from services.agent_host import host as host_module
from services.agent_host.dispatcher import InboundWakeDispatcher, TurnScheduler
from services.agent_host.host import AgentHost
from shared.db import insert_inbound_message
from shared.incarnation_resources import IncarnationResources, ResourceProcess, decode_resources
from shared.machine import machine_name
from tests.agent.test_hosted_db_recovery import _admit, _graph
from tests.shared.poll_until import poll_until_async


def _accept_model_config(**_kwargs: object) -> str:
    return "test"


@pytest.fixture(autouse=True)
def isolated_clocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(progress, "_PROGRESS", {})
    monkeypatch.setattr(dispatch, "CANCEL_UNWIND_TIMEOUT_S", 0.03)
    monkeypatch.setattr(host_module, "validate_model_config", _accept_model_config)
    monkeypatch.setattr(
        host_module,
        "boot_agent_scope",
        AsyncMock(return_value=FakeListChatModel(responses=["unused"])),
    )


@pytest.mark.parametrize("known_progress", [True, False])
async def test_old_pending_does_not_cancel_current_graph_progress(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, known_progress: bool
) -> None:
    incarnation = await _admit(aops_pool)
    agent = incarnation.agent_id
    inbound = insert_inbound_message(db_conn, agent, "Queued before host recovery", "user")
    db_conn.execute(
        "UPDATE inbound_messages SET created_at=now()-interval '1 day' WHERE id=%s", (inbound,)
    )
    db_conn.execute(
        "UPDATE agents_meta SET last_active_at=now()-interval '1 day' WHERE id=%s", (agent,)
    )
    db_conn.commit()
    entered, release = asyncio.Event(), asyncio.Event()
    invocations: list[int] = []

    async def work(_state: AgentState) -> dict[str, object]:
        invocations.append(agent)
        progress.mark_turn_progress(agent)
        entered.set()
        await release.wait()
        return {"turn_idle": True, "halted": True, "messages": [AIMessage(content="Completed")]}

    graph, saver = await _graph(aops_pool, agent, work)
    host = AgentHost(pool=aops_pool, graph=graph, checkpointer=saver)
    host._owner = incarnation.owner
    scheduler = TurnScheduler(host.run_turn)
    dispatcher = InboundWakeDispatcher(
        "redis://unused", scheduler, pending_scan=host.pending_inbound_wakes, stale_after_s=60
    )
    try:
        scheduler.wake(agent)
        await asyncio.wait_for(entered.wait(), 3)
        assert [(w.agent_id, w.stale) for w in await host.pending_inbound_wakes(60)] == [
            (agent, True)
        ]
        if not known_progress:
            progress._PROGRESS.pop(agent)
        await dispatcher.scan_once()
        assert agent in scheduler.active_agents
        assert invocations == [agent]
        assert not scheduler.restart_required
    finally:
        release.set()
        await poll_until_async(lambda: not scheduler.active_agents, timeout=3)
        await scheduler.aclose()
        await host.aclose()
    state = await graph.aget_state({"configurable": {"thread_id": str(agent)}})
    assert state.values["messages"][-1].content == "Completed"


async def test_expired_predecessor_is_rediscovered_after_boot_without_pending_messages(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    incarnation = await _admit(aops_pool)
    agent = incarnation.agent_id
    inbound = insert_inbound_message(db_conn, agent, "Claimed before host exit", "user")
    db_conn.execute("UPDATE inbound_messages SET status='claimed' WHERE id=%s", (inbound,))
    db_conn.commit()
    with subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
    ) as predecessor:
        assert predecessor.stdin is not None
        dead = ResourceProcess(
            pid=predecessor.pid, birth=psutil.Process(predecessor.pid).create_time()
        )
        predecessor.stdin.close()
        predecessor.wait(timeout=3)
    row = db_conn.execute(
        "SELECT incarnation_resources FROM agents_meta WHERE id=%s", (agent,)
    ).fetchone()
    assert row is not None
    resources = decode_resources(row[0])
    assert isinstance(resources, IncarnationResources)
    db_conn.execute(
        "UPDATE agents_meta SET incarnation_resources=%s WHERE id=%s",
        (Jsonb(resources.model_copy(update={"host_process": dead}).model_dump(mode="json")), agent),
    )
    db_conn.commit()
    calls: list[int] = []

    async def work(_state: AgentState) -> dict[str, object]:
        calls.append(agent)
        return {"turn_idle": True, "halted": True, "messages": [AIMessage(content="Recovered")]}

    graph, saver = await _graph(aops_pool, agent, work)
    await graph.aupdate_state(
        {"configurable": {"thread_id": str(agent)}},
        {
            "messages": [
                HumanMessage(content="Claimed work", additional_kwargs={"ava_inbound_id": inbound})
            ]
        },
        as_node="work",
    )
    host = AgentHost(pool=aops_pool, checkpointer=saver, graph=graph)
    # A host boot while the dead predecessor still has a fresh lease cannot settle it.
    assert await settle_stale_running_rows(aops_pool, machine_name()) == []
    assert await host.pending_inbound_wakes(60) == []
    db_conn.execute(
        "UPDATE agents_meta SET lease_expires_at=now()-interval '1s' WHERE id=%s", (agent,)
    )
    db_conn.commit()
    before = db_conn.execute("SELECT * FROM inbound_messages WHERE id=%s", (inbound,)).fetchone()
    assert [w.agent_id for w in await host.pending_inbound_wakes(60)] == [agent]
    assert (
        db_conn.execute("SELECT * FROM inbound_messages WHERE id=%s", (inbound,)).fetchone()
        == before
    )
    scheduler = TurnScheduler(host.run_turn)
    dispatcher = InboundWakeDispatcher(
        "redis://unused", scheduler, pending_scan=host.pending_inbound_wakes, stale_after_s=60
    )
    try:
        await dispatcher.scan_once()
        await poll_until_async(lambda: not scheduler.active_agents, timeout=3)
        assert calls == [agent]
        assert db_conn.execute(
            "SELECT status,runtime_owner,runtime_generation<>%s FROM agents_meta WHERE id=%s",
            (incarnation.generation, agent),
        ).fetchone() == ("idling", host._owner, True)
        assert db_conn.execute(
            "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
        ).fetchone() == (1,), "recovery must not invent an inbound message"
        state = await graph.aget_state({"configurable": {"thread_id": str(agent)}})
        assert state.values["messages"][-2].content == "Claimed work"
        assert state.values["messages"][-1].content == "Recovered"
        assert db_conn.execute(
            "SELECT status FROM inbound_messages WHERE id=%s", (inbound,)
        ).fetchone() == ("done",), "startup must reconcile the already checkpointed claim"
    finally:
        await scheduler.aclose()
        await host.aclose()


@pytest.mark.parametrize("boundary", ["fresh", "same_owner", "foreign", "idling", "terminated"])
async def test_owner_recovery_scan_excludes_unrelated_rows(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, boundary: str
) -> None:
    incarnation = await _admit(aops_pool)
    agent = incarnation.agent_id
    host = AgentHost(pool=aops_pool, checkpointer=AsyncMock(), graph=AsyncMock())
    if boundary == "same_owner":
        host._owner = incarnation.owner
    db_conn.execute(
        "UPDATE agents_meta SET status=%s,machine=%s,lease_expires_at=now()+make_interval(secs=>%s) "
        "WHERE id=%s",
        (
            boundary if boundary in {"idling", "terminated"} else "running",
            "another-machine" if boundary == "foreign" else machine_name(),
            60 if boundary == "fresh" else -60,
            agent,
        ),
    )
    db_conn.commit()
    assert await host.pending_inbound_wakes(60) == []


async def test_expired_scan_wake_cannot_steal_a_live_predecessor(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    incarnation = await _admit(aops_pool)
    agent = incarnation.agent_id
    host = AgentHost(pool=aops_pool, checkpointer=AsyncMock(), graph=AsyncMock())
    with subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
    ) as predecessor:
        assert predecessor.stdin is not None
        try:
            row = db_conn.execute(
                "SELECT incarnation_resources FROM agents_meta WHERE id=%s", (agent,)
            ).fetchone()
            assert row is not None
            resources = decode_resources(row[0])
            assert isinstance(resources, IncarnationResources)
            native = ResourceProcess(
                pid=predecessor.pid, birth=psutil.Process(predecessor.pid).create_time()
            )
            db_conn.execute(
                "UPDATE agents_meta SET incarnation_resources=%s,"
                "lease_expires_at=now()-interval '1s' WHERE id=%s",
                (
                    Jsonb(
                        resources.model_copy(update={"host_process": native}).model_dump(
                            mode="json"
                        )
                    ),
                    agent,
                ),
            )
            db_conn.commit()
            before = db_conn.execute("SELECT * FROM agents_meta WHERE id=%s", (agent,)).fetchone()
            assert [w.agent_id for w in await host.pending_inbound_wakes(60)] == [agent]
            await host.run_turn(agent)
            assert host.stats.cache_misses == 0
            assert (
                db_conn.execute("SELECT * FROM agents_meta WHERE id=%s", (agent,)).fetchone()
                == before
            )
            assert predecessor.poll() is None
        finally:
            predecessor.stdin.close()
            predecessor.wait(timeout=3)
