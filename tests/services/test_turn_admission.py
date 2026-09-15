"""The hosted turn admission gate — services/agent_host/admission.py (#3584).

`test_agent_host.py` locks that a configured cap queues excess turns and
`test_turn_dispatcher.py` locks WHEN agents run. This file locks the gate's own
contract:

1. **Ticket rotation** — one ticket per agent (TurnScheduler's single flight),
   arrival-order FIFO service, and a completed turn's next request taken at the
   tail: no starvation, no overtaking.
2. **Queued is not stalled** — the waiter is registered in
   ``agent/_turn_progress.py`` BEFORE its acquire await and cleared on serve or
   cancel; the dispatcher's fake-alive scan reads exactly this registry.
3. **Observability** — depth, ages and served-wait counters for ``/stats``;
   one ``long_waiters`` report per wait episode.
4. **Unlimited mode** — ``limit <= 0`` is a state-free no-op (legacy zero
   semantics).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from typing import cast

import pytest

from agent import _turn_progress as progress
from services.agent_host.admission import TurnAdmission
from services.agent_host.dispatcher import (
    InboundWakeDispatcher,
    PendingInboundWake,
    TurnScheduler,
)


async def _until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def _clean_wait_registry() -> Iterator[None]:
    yield
    progress._ADMISSION_WAIT.clear()


async def _park(gate: TurnAdmission, agent_id: int, entered: asyncio.Event) -> None:
    """One admitted turn that holds its slot until cancelled."""
    async with gate.admit(agent_id):
        entered.set()
        await asyncio.Event().wait()


class TestQueueFairness:
    async def test_fifo_service_order_under_capacity(self) -> None:
        """cap=1 with four arrivals: served strictly in arrival order."""
        gate = TurnAdmission(1)
        served: list[int] = []
        finish = {i: asyncio.Event() for i in range(4)}

        async def _turn(agent_id: int) -> None:
            async with gate.admit(agent_id):
                served.append(agent_id)
                await finish[agent_id].wait()

        tasks = [asyncio.create_task(_turn(i)) for i in range(4)]
        try:
            await _until(lambda: gate.waiting == 3)
            assert served == [0]
            for agent_id in range(4):
                finish[agent_id].set()
                if agent_id < 3:
                    await _until(lambda n=agent_id: served == list(range(n + 2)))
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task

        assert served == [0, 1, 2, 3]
        assert gate.waiting == 0
        assert gate.waits_total == 3
        assert gate.wait_seconds_max > 0.0

    async def test_a_completed_agents_next_ticket_joins_the_tail(self) -> None:
        """'Completion re-queues at the tail': the successor ticket is taken
        when it arrives — behind the agents already queued."""
        gate = TurnAdmission(1)
        served: list[str] = []
        finish: dict[str, asyncio.Event] = {}
        tasks: list[asyncio.Task[None]] = []

        async def _turn(tag: str, agent_id: int) -> None:
            event = asyncio.Event()
            finish[tag] = event
            async with gate.admit(agent_id):
                served.append(tag)
                await event.wait()

        try:
            tasks.append(asyncio.create_task(_turn("1:first", 1)))
            await _until(lambda: served == ["1:first"])
            tasks.append(asyncio.create_task(_turn("2:first", 2)))
            tasks.append(asyncio.create_task(_turn("3:first", 3)))
            await _until(lambda: gate.waiting == 2)
            tasks.append(asyncio.create_task(_turn("1:second", 1)))
            await _until(lambda: gate.waiting == 3)
            finish["1:first"].set()
            await _until(lambda: served == ["1:first", "2:first"])
            finish["2:first"].set()
            await _until(lambda: served == ["1:first", "2:first", "3:first"])
            finish["3:first"].set()
            await _until(lambda: served == ["1:first", "2:first", "3:first", "1:second"])
        finally:
            for event in finish.values():
                event.set()
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task

        assert served == ["1:first", "2:first", "3:first", "1:second"]


class TestWaitState:
    async def test_wait_state_is_registered_before_the_acquire_await(self) -> None:
        """The registry must see a waiter BEFORE it parks — no window in which
        the scan could cancel the about-to-queue task (review: 3230)."""
        gate = TurnAdmission(1)
        holder_entered = asyncio.Event()
        holder = asyncio.create_task(_park(gate, 1, holder_entered))
        await _until(holder_entered.is_set)

        entered = asyncio.Event()

        async def _waiter() -> None:
            async with gate.admit(2):
                entered.set()

        waiter = asyncio.create_task(_waiter())
        try:
            await _until(lambda: progress.admission_wait_age_s(2) is not None)
            assert gate.waiting == 1
            assert not entered.is_set()
        finally:
            holder.cancel()
            with suppress(asyncio.CancelledError):
                await holder
        await _until(entered.is_set)
        # Cleared as soon as the slot was served, not when the turn ends.
        assert progress.admission_wait_age_s(2) is None
        await waiter

    async def test_a_cancelled_waiter_leaves_no_state_behind(self) -> None:
        gate = TurnAdmission(1)
        holder_entered = asyncio.Event()
        holder = asyncio.create_task(_park(gate, 1, holder_entered))
        await _until(holder_entered.is_set)

        entered = asyncio.Event()
        waiter = asyncio.create_task(_park(gate, 2, entered))
        await _until(lambda: progress.admission_wait_age_s(2) is not None)

        waiter.cancel()
        with suppress(asyncio.CancelledError):
            await waiter
        assert gate.waiting == 0
        assert gate.waits_total == 0
        assert gate.wait_seconds_total == 0.0
        assert progress.admission_wait_age_s(2) is None
        assert progress._ADMISSION_WAIT.get(2) is None

        holder.cancel()
        with suppress(asyncio.CancelledError):
            await holder

    async def test_long_waiters_report_once_per_episode(self) -> None:
        gate = TurnAdmission(1)
        holder_entered = asyncio.Event()
        holder = asyncio.create_task(_park(gate, 1, holder_entered))
        await _until(holder_entered.is_set)

        entered = asyncio.Event()
        waiter = asyncio.create_task(_park(gate, 2, entered))
        try:
            await _until(lambda: gate.waiting == 1)
            first = gate.long_waiters(0.0)
            assert [agent_id for agent_id, _waited in first] == [2]
            assert gate.long_waiters(0.0) == [], "one report per wait episode"
        finally:
            holder.cancel()
            with suppress(asyncio.CancelledError):
                await holder
        await _until(entered.is_set)
        with suppress(asyncio.CancelledError):
            waiter.cancel()
            await waiter

    async def test_status_payload_reports_depth_and_ages(self) -> None:
        gate = TurnAdmission(2)
        entered = [asyncio.Event(), asyncio.Event()]
        holders = [
            asyncio.create_task(_park(gate, 10, entered[0])),
            asyncio.create_task(_park(gate, 11, entered[1])),
        ]
        queued: list[asyncio.Task[None]] = []
        try:
            await _until(lambda: all(event.is_set() for event in entered))
            queued = [
                asyncio.create_task(_park(gate, agent_id, asyncio.Event()))
                for agent_id in (12, 13, 14)
            ]
            await _until(lambda: gate.waiting == 3)
            payload = gate.payload()
            assert payload["admission_limit"] == 2
            assert payload["admission_waiting"] == 3
            ages = cast("dict[str, float]", payload["admission_waiting_agents"])
            assert set(ages) == {"12", "13", "14"}
            assert all(isinstance(age, float) and age >= 0 for age in ages.values())
        finally:
            for task in [*holders, *queued]:
                task.cancel()
            for task in [*holders, *queued]:
                with suppress(asyncio.CancelledError):
                    await task

    async def test_unlimited_mode_is_a_state_free_no_op(self) -> None:
        gate = TurnAdmission(0)
        assert gate.enabled is False
        assert gate.payload()["admission_limit"] == 0
        assert gate.payload()["admission_waiting"] == 0

        async def _turn(agent_id: int) -> None:
            async with gate.admit(agent_id):
                assert progress.admission_wait_age_s(agent_id) is None

        await asyncio.gather(*(_turn(i) for i in range(64)))
        assert gate.waiting == 0
        assert gate.waits_total == 0
        assert progress._ADMISSION_WAIT == {}


class TestSchedulerIntegration:
    async def test_a_wake_during_a_turn_takes_its_successor_ticket_at_the_tail(self) -> None:
        gate = TurnAdmission(1)
        served: list[tuple[int, int]] = []
        calls: dict[int, int] = {}
        releases: dict[tuple[int, int], asyncio.Event] = {}

        async def run_turn(agent_id: int) -> None:
            calls[agent_id] = calls.get(agent_id, 0) + 1
            key = (agent_id, calls[agent_id])
            releases[key] = asyncio.Event()
            async with gate.admit(agent_id):
                served.append(key)
                await releases[key].wait()

        scheduler = TurnScheduler(run_turn)
        try:
            scheduler.wake(1)
            await _until(lambda: served == [(1, 1)])
            scheduler.wake(2)
            scheduler.wake(3)
            await _until(lambda: gate.waiting == 2)
            scheduler.wake(1)  # wake during the turn → a successor becomes pending
            releases[(1, 1)].set()
            await _until(lambda: served == [(1, 1), (2, 1)])
            releases[(2, 1)].set()
            await _until(lambda: served == [(1, 1), (2, 1), (3, 1)])
            releases[(3, 1)].set()
            await _until(lambda: served == [(1, 1), (2, 1), (3, 1), (1, 2)])
        finally:
            for event in releases.values():
                event.set()
            await scheduler.aclose()
        assert served == [(1, 1), (2, 1), (3, 1), (1, 2)]

    async def test_a_queued_request_survives_a_dropped_queue(self) -> None:
        """Queue positions are in-process; the wake's cause is durable. A
        dropped queue (host restart) loses no work — the durable scan re-wakes
        the agents and the fresh queue serves them."""
        gate = TurnAdmission(1)
        served: list[int] = []
        hold_first = asyncio.Event()

        async def run_turn(agent_id: int) -> None:
            async with gate.admit(agent_id):
                served.append(agent_id)
                if agent_id == 1:
                    await hold_first.wait()

        scheduler = TurnScheduler(run_turn)
        scheduler.wake(1)
        await _until(lambda: served == [1])
        scheduler.wake(2)
        scheduler.wake(3)
        await _until(lambda: gate.waiting == 2)

        await scheduler.aclose()  # the restart: tasks die with the queue
        assert gate.waiting == 0, "the dropped queue leaves no waiter state"
        assert gate.waits_total == 0, "an unserved wait is not counted as served"

        fresh = TurnScheduler(run_turn)

        async def _pending(_stale_after_s: float) -> list[PendingInboundWake]:
            return [
                PendingInboundWake(agent_id=2, stale=False),
                PendingInboundWake(agent_id=3, stale=False),
            ]

        disp = InboundWakeDispatcher(
            "redis://unused", fresh, pending_scan=_pending, stale_after_s=180.0
        )
        try:
            await disp.scan_once()
            await _until(lambda: sorted(served) == [1, 2, 3])
        finally:
            await fresh.aclose()
        assert served == [1, 2, 3]
