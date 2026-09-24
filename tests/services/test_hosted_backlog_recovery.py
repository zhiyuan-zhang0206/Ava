"""Old inbox timestamps cannot kill fresh turns or hide expired owners."""

import asyncio
import subprocess
import sys
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

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
from services.agent_host import dispatcher
from services.agent_host import host as host_module
from services.agent_host import runtime as runtime_module
from services.agent_host.dispatcher import InboundWakeDispatcher, TurnScheduler
from services.agent_host.host import AgentHost
from shared.db import insert_inbound_message
from shared.incarnation_resources import IncarnationResources, ResourceProcess, decode_resources
from shared.machine import machine_name
from tests.agent.test_hosted_db_recovery import _admit, _graph
from tests.services.test_agent_host import _PendingScanPool
from tests.services.test_turn_dispatcher import _ScanScheduler, _stale_age
from tests.shared.poll_until import poll_until_async


def _accept_model_config(**_kwargs: object) -> str:
    return "test"


@pytest.fixture(autouse=True)
def isolated_clocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(progress, "_PROGRESS", {})
    monkeypatch.setattr(dispatcher, "CANCEL_UNWIND_TIMEOUT_S", 0.03)
    monkeypatch.setattr(runtime_module, "validate_model_config", _accept_model_config)
    monkeypatch.setattr(
        host_module,
        "boot_agent_scope",
        AsyncMock(return_value=FakeListChatModel(responses=["unused"])),
    )


async def test_pending_scan_classifies_lifecycle_work_and_lease(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    incarnation = await _admit(aops_pool)
    agent = incarnation.agent_id
    db_conn.execute("UPDATE agents_meta SET status='idling' WHERE id=%s", (agent,))
    lifecycle = insert_inbound_message(db_conn, agent, "", "system:test", kind="restart")
    db_conn.commit()
    host = AgentHost(pool=aops_pool, checkpointer=AsyncMock(), graph=AsyncMock())

    assert [(wake.agent_id, wake.recovery) for wake in await host.pending_inbound_wakes(60)] == [
        (agent, True)
    ]

    ordinary = insert_inbound_message(db_conn, agent, "Need a reply", "user")
    db_conn.commit()
    assert [(wake.agent_id, wake.recovery) for wake in await host.pending_inbound_wakes(60)] == [
        (agent, False)
    ]

    db_conn.execute(
        "UPDATE inbound_messages SET status='done' WHERE id IN (%s,%s)", (lifecycle, ordinary)
    )
    db_conn.execute(
        "INSERT INTO agent_impersonations(id,agent_id,session_id,source,machine,status,"
        "ttl_seconds,expires_at) VALUES(%s,%s,1,'cli',%s,'requested',300,"
        "clock_timestamp()+interval '5 minutes')",
        (uuid4(), agent, machine_name()),
    )
    db_conn.commit()
    assert [(wake.agent_id, wake.recovery) for wake in await host.pending_inbound_wakes(60)] == [
        (agent, False)
    ]


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
        assert (await host.pending_inbound_wakes(60))[0].recovery is False
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
    assert [(w.agent_id, w.recovery) for w in await host.pending_inbound_wakes(60)] == [
        (agent, True)
    ]
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


@pytest.mark.parametrize("status", ["running", "idling"])
@pytest.mark.parametrize(
    "boundary", ["fresh", "same_owner", "foreign", "unowned", "fatal", "terminated"]
)
async def test_owner_recovery_scan_excludes_unrelated_rows(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, status: str, boundary: str
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
            "terminated" if boundary == "terminated" else status,
            "another-machine" if boundary == "foreign" else machine_name(),
            60 if boundary == "fresh" else -60,
            agent,
        ),
    )
    if boundary == "unowned":
        db_conn.execute(
            "UPDATE agents_meta SET runtime_owner=NULL,runtime_generation=NULL WHERE id=%s",
            (agent,),
        )
    if boundary == "fatal":
        db_conn.execute("UPDATE agents_meta SET last_turn_fatal_at=now() WHERE id=%s", (agent,))
    db_conn.commit()
    assert await host.pending_inbound_wakes(60) == []


@pytest.mark.parametrize("status", ["running", "idling"])
async def test_expired_scan_wake_cannot_steal_a_live_predecessor(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, status: str
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
                "UPDATE agents_meta SET status=%s,incarnation_resources=%s,"
                "lease_expires_at=now()-interval '1s' WHERE id=%s",
                (
                    status,
                    Jsonb(
                        resources.model_copy(update={"host_process": native}).model_dump(
                            mode="json"
                        )
                    ),
                    agent,
                ),
            )
            db_conn.commit()
            before = db_conn.execute(
                "SELECT status,runtime_generation,runtime_owner,incarnation_resources,"
                "lease_expires_at FROM agents_meta WHERE id=%s",
                (agent,),
            ).fetchone()
            assert [w.agent_id for w in await host.pending_inbound_wakes(60)] == [agent]
            await host.run_turn(agent)
            assert host.stats.cache_misses == 0
            assert (
                db_conn.execute(
                    "SELECT status,runtime_generation,runtime_owner,incarnation_resources,"
                    "lease_expires_at FROM agents_meta WHERE id=%s",
                    (agent,),
                ).fetchone()
                == before
            )
            observation = db_conn.execute(
                "SELECT last_admission_outcome,last_admission_at FROM agents_meta WHERE id=%s",
                (agent,),
            ).fetchone()
            assert observation is not None and observation[0] == "admission_guard_refused"
            assert observation[1] is not None
            assert predecessor.poll() is None
        finally:
            predecessor.stdin.close()
            predecessor.wait(timeout=3)


class TestHostedWakePacing:
    async def test_recovery_slot_released_before_ordinary_work_successor_finishes(self) -> None:
        first_started = asyncio.Event()
        successor_started = asyncio.Event()
        second_started = asyncio.Event()
        finish_first = asyncio.Event()
        finish_successor = asyncio.Event()
        finish_second = asyncio.Event()
        calls: list[int] = []

        async def run_turn(agent_id: int) -> None:
            calls.append(agent_id)
            if agent_id == 1 and calls.count(1) == 1:
                first_started.set()
                await finish_first.wait()
            elif agent_id == 1:
                successor_started.set()
                await finish_successor.wait()
            else:
                second_started.set()
                await finish_second.wait()

        scheduler = TurnScheduler(run_turn)
        pending = [dispatcher.PendingInboundWake(1, False, True)]

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return pending

        disp = InboundWakeDispatcher(
            "redis://unused",
            scheduler,
            pending_scan=_pending,
            stale_after_s=180.0,
            recovery_wake_batch=2,
            recovery_wake_inflight=1,
        )
        try:
            await disp.scan_once()
            await asyncio.wait_for(first_started.wait(), 3)
            scheduler.wake(1)  # Ordinary work queues behind the recovery turn.
            finish_first.set()
            await asyncio.wait_for(successor_started.wait(), 3)
            assert disp._recovery_in_flight == {}

            pending[:] = [dispatcher.PendingInboundWake(2, False, True)]
            await disp.scan_once()
            await asyncio.wait_for(second_started.wait(), 3)
            assert calls == [1, 1, 2]
            assert scheduler.active_agents == {1, 2}
            assert set(disp._recovery_in_flight) == {2}
        finally:
            finish_successor.set()
            finish_second.set()
            await scheduler.aclose()

    async def test_recovery_inflight_cap_releases_completed_turns_and_preserves_work(self) -> None:
        entered: set[int] = set()
        release = {agent_id: asyncio.Event() for agent_id in (1, 2, 3, 4, 99)}

        async def run_turn(agent_id: int) -> None:
            entered.add(agent_id)
            await release[agent_id].wait()

        scheduler = TurnScheduler(run_turn)
        remaining = {1, 2, 3, 4}

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return [
                dispatcher.PendingInboundWake(agent_id=agent_id, stale=False, recovery=True)
                for agent_id in sorted(remaining)
            ] + [dispatcher.PendingInboundWake(agent_id=99, stale=False)]

        disp = InboundWakeDispatcher(
            "redis://unused",
            scheduler,
            pending_scan=_pending,
            stale_after_s=180.0,
            recovery_wake_batch=3,
            recovery_wake_inflight=2,
        )
        try:
            await disp.scan_once()
            assert scheduler.active_agents == {1, 2, 99}
            await disp.scan_once()
            assert scheduler.active_agents == {1, 2, 99}
            release[1].set()
            await poll_until_async(lambda: 1 not in scheduler.active_agents, timeout=3)
            remaining.remove(1)
            await disp.scan_once()
            assert scheduler.active_agents == {2, 3, 99}
            release[2].set()
            release[3].set()
            await poll_until_async(lambda: not ({2, 3} & scheduler.active_agents), timeout=3)
            remaining.difference_update({2, 3})
            await disp.scan_once()
            assert scheduler.active_agents == {4, 99}
            await poll_until_async(lambda: entered >= {1, 2, 3, 4, 99}, timeout=3)
        finally:
            for event in release.values():
                event.set()
            await scheduler.aclose()

    async def test_unstarted_recovery_wake_does_not_take_slot(self) -> None:
        scheduler = _ScanScheduler()
        pending = [dispatcher.PendingInboundWake(agent_id=1, stale=False, recovery=True)]

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return pending

        disp = InboundWakeDispatcher(
            "redis://unused",
            scheduler,
            pending_scan=_pending,
            stale_after_s=180.0,
            recovery_wake_batch=2,
            recovery_wake_inflight=1,
        )
        await disp.scan_once()
        assert scheduler.woken == [1]
        assert disp._recovery_in_flight == {}
        pending[:] = [dispatcher.PendingInboundWake(agent_id=2, stale=False, recovery=True)]
        await disp.scan_once()
        assert scheduler.woken == [1, 2]
        assert disp._recovery_in_flight == {}

    async def test_closed_scheduler_scans_start_no_recovery_turns(self) -> None:
        calls: list[int] = []

        async def run_turn(agent_id: int) -> None:
            calls.append(agent_id)

        scheduler = TurnScheduler(run_turn)
        await scheduler.aclose()

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return [dispatcher.PendingInboundWake(1, False, True)]

        disp = InboundWakeDispatcher(
            "redis://unused", scheduler, pending_scan=_pending, stale_after_s=180.0
        )
        await disp.scan_once()
        await disp.scan_once()
        assert calls == []
        assert scheduler.active_agents == set()
        assert disp._recovery_in_flight == {}

    async def test_failed_recovery_turn_releases_slot_on_next_scan(self) -> None:
        attempted: list[int] = []

        async def run_turn(agent_id: int) -> None:
            attempted.append(agent_id)
            raise RuntimeError("failed before admission")

        scheduler = TurnScheduler(run_turn)
        pending = [dispatcher.PendingInboundWake(agent_id=1, stale=False, recovery=True)]

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return pending

        disp = InboundWakeDispatcher(
            "redis://unused",
            scheduler,
            pending_scan=_pending,
            stale_after_s=180.0,
            recovery_wake_batch=2,
            recovery_wake_inflight=1,
        )
        try:
            await disp.scan_once()
            await poll_until_async(
                lambda: attempted == [1] and not scheduler.active_agents, timeout=3
            )
            pending[:] = [dispatcher.PendingInboundWake(agent_id=2, stale=False, recovery=True)]
            await disp.scan_once()
            await poll_until_async(lambda: attempted == [1, 2], timeout=3)
        finally:
            await scheduler.aclose()

    async def test_recovery_cap_drains_across_scans_without_delaying_work(self) -> None:
        scheduler = _ScanScheduler()
        remaining = set(range(1, 7))

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return [
                dispatcher.PendingInboundWake(agent_id=agent_id, stale=False, recovery=True)
                for agent_id in sorted(remaining)
            ] + [dispatcher.PendingInboundWake(agent_id=99, stale=False)]

        disp = InboundWakeDispatcher(
            "redis://unused",
            scheduler,
            pending_scan=_pending,
            stale_after_s=180.0,
            recovery_wake_batch=2,
        )
        await disp.scan_once()
        assert scheduler.woken == [1, 2, 99]
        remaining.difference_update(scheduler.woken)

        await disp.scan_once()
        assert scheduler.woken == [1, 2, 99, 3, 4, 99]
        remaining.difference_update(scheduler.woken)

        await disp.scan_once()
        assert scheduler.woken == [1, 2, 99, 3, 4, 99, 5, 6, 99]
        remaining.difference_update(scheduler.woken)
        assert remaining == set()
        assert [agent_id for agent_id in scheduler.woken if agent_id != 99] == list(range(1, 7))
        await disp.scan_once()
        assert scheduler.woken[-1] == 99

    async def test_recovery_wakes_for_active_agents_do_not_spend_new_turn_budget(self) -> None:
        scheduler = _ScanScheduler({1})

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return [
                dispatcher.PendingInboundWake(agent_id=1, stale=False, recovery=True),
                dispatcher.PendingInboundWake(agent_id=2, stale=False, recovery=True),
                dispatcher.PendingInboundWake(agent_id=3, stale=False, recovery=True),
            ]

        disp = InboundWakeDispatcher(
            "redis://unused",
            scheduler,
            pending_scan=_pending,
            stale_after_s=180.0,
            recovery_wake_batch=1,
        )
        await disp.scan_once()
        assert scheduler.woken == [1, 2]

    async def test_recovery_cap_does_not_skip_stale_cancellation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(dispatcher, "turn_progress_age_s", _stale_age)
        scheduler = _ScanScheduler({3})

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return [
                dispatcher.PendingInboundWake(agent_id=1, stale=False, recovery=True),
                dispatcher.PendingInboundWake(agent_id=2, stale=False, recovery=True),
                dispatcher.PendingInboundWake(agent_id=3, stale=True, recovery=True),
            ]

        disp = InboundWakeDispatcher(
            "redis://unused",
            scheduler,
            pending_scan=_pending,
            stale_after_s=180.0,
            recovery_wake_batch=1,
        )
        await disp.scan_once()
        assert scheduler.cancelled == [3]
        assert scheduler.woken == [1]

    async def test_small_recovery_cohort_is_all_woken_in_first_scan(self) -> None:
        scheduler = _ScanScheduler()

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return [
                dispatcher.PendingInboundWake(agent_id=agent_id, stale=False, recovery=True)
                for agent_id in (1, 2)
            ]

        disp = InboundWakeDispatcher(
            "redis://unused",
            scheduler,
            pending_scan=_pending,
            stale_after_s=180.0,
            recovery_wake_batch=2,
        )
        await disp.scan_once()
        assert scheduler.woken == [1, 2]


class TestHostedHostWakePacing:
    async def test_held_cohort_wakes_are_never_paced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pool = _PendingScanPool([(17, False, False)])
        host = AgentHost(
            pool=cast(AsyncConnectionPool[Any], pool),
            checkpointer=object(),  # pyright: ignore[reportArgumentType]
            graph=object(),  # pyright: ignore[reportArgumentType]
            machine="this-box",
        )

        def held_wakes(_fences: object) -> list[dispatcher.PendingInboundWake]:
            return [
                dispatcher.PendingInboundWake(17, False),
                dispatcher.PendingInboundWake(23, False),
            ]

        monkeypatch.setattr(
            "services.agent_host.host.maintenance_receipts.pending_wakes",
            held_wakes,
        )
        # The drain's held re-drive is update machinery: never paced.
        assert [
            (wake.agent_id, wake.recovery) for wake in await host.pending_inbound_wakes(30)
        ] == [
            (17, False),
            (23, False),
        ]
        scheduler = _ScanScheduler()
        wake_dispatcher = InboundWakeDispatcher(
            "redis://unused",
            scheduler,
            pending_scan=host.pending_inbound_wakes,
            stale_after_s=30,
            recovery_wake_batch=1,
            recovery_wake_inflight=1,
        )
        await wake_dispatcher.scan_once()
        assert scheduler.woken == [17, 23]

    async def test_settled_reap_wake_is_consumed_by_first_turn_attempt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pool = _PendingScanPool([])
        host = AgentHost(
            pool=cast(AsyncConnectionPool[Any], pool),
            checkpointer=object(),  # pyright: ignore[reportArgumentType]
            graph=object(),  # pyright: ignore[reportArgumentType]
            machine="this-box",
        )
        turn = AsyncMock(return_value=None)
        monkeypatch.setattr(host, "_run_turn", turn)
        monkeypatch.setattr("shared.hosted_force.original_host_force", AsyncMock(return_value=None))
        monkeypatch.setattr(
            "services.agent_host.host.maintenance_receipts.record_drained",
            AsyncMock(return_value=None),
        )
        host.arm_settled_reaps([17, 17])
        scheduler = TurnScheduler(host.run_turn)
        wake_dispatcher = dispatcher.InboundWakeDispatcher(
            "redis://unused",
            scheduler,
            pending_scan=host.pending_inbound_wakes,
            stale_after_s=30,
            recovery_wake_batch=1,
        )
        try:
            assert [
                (wake.agent_id, wake.recovery) for wake in await host.pending_inbound_wakes(30)
            ] == [(17, True)]
            await wake_dispatcher.scan_once()
            await poll_until_async(lambda: not scheduler.active_agents, timeout=3)
            await wake_dispatcher.scan_once()
            assert await host.pending_inbound_wakes(30) == []
            turn.assert_awaited_once_with(17)
        finally:
            await scheduler.aclose()
