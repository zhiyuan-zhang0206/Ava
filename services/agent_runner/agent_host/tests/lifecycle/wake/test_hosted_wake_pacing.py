"""Hosted wake scheduling bounds recovery without blocking direct successors."""

import asyncio

import pytest

from base.events.live.bus import EventBus
from services.agent_runner.agent_host import dispatcher
from services.agent_runner.agent_host.dispatcher import InboundWakeDispatcher, TurnScheduler
from services.agent_runner.agent_host.tests.lifecycle.wake_recovery_setup import isolated_clocks
from services.agent_runner.agent_host.tests.turn_dispatcher.scan_setup import (
    FixedClock,
    ScanScheduler,
)
from tests.components.base.poll_until import poll_until_async


class TestHostedWakePacing:
    async def test_active_cancel_direct_successor_does_not_hold_recovery_slot(self) -> None:
        release = asyncio.Event()
        entered: list[int] = []

        async def run_turn(agent_id: int) -> None:
            entered.append(agent_id)
            if agent_id == 1 and entered.count(1) == 1:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    asyncio.get_running_loop().call_soon(scheduler.wake, 1)
                    raise
            await release.wait()

        pending = [dispatcher.PendingInboundWake(1, False, True)]

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return pending

        scheduler = TurnScheduler(run_turn)
        disp = InboundWakeDispatcher(
            EventBus.from_settings(),
            scheduler,
            pending_scan=_pending,
            stale_after_s=180.0,
            recovery_wake_inflight=1,
        )
        try:
            await disp.scan_once()
            original = disp._recovery_in_flight[1]
            await poll_until_async(lambda: entered == [1], timeout=3)
            assert await scheduler.cancel_agent(1)
            await poll_until_async(lambda: entered == [1, 1], timeout=3)
            direct = scheduler.task_for(1)
            assert original.done() and direct is not None and not direct.done()
            assert disp._recovery_in_flight == {} and scheduler.reaped_successor(1) is None
            pending[:] = [dispatcher.PendingInboundWake(2, False, True)]
            await disp.scan_once()
            await poll_until_async(lambda: entered == [1, 1, 2], timeout=3)
            assert set(disp._recovery_in_flight) == {2} and not direct.done()
            release.set()
            await poll_until_async(lambda: disp._recovery_in_flight == {}, timeout=3)
        finally:
            release.set()
            await scheduler.aclose()

    async def test_active_cancel_releases_slot_without_claiming_direct_wake(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def run_turn(agent_id: int) -> None:
            if agent_id == 1:
                entered.set()
            await release.wait()

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return [dispatcher.PendingInboundWake(1, False, True)]

        scheduler = TurnScheduler(run_turn)
        disp = InboundWakeDispatcher(
            EventBus.from_settings(), scheduler, pending_scan=_pending, stale_after_s=180.0
        )
        try:
            direct = scheduler.wake(3)
            assert direct is not None and disp._recovery_in_flight == {}
            await disp.scan_once()
            first = disp._recovery_in_flight[1]
            await asyncio.wait_for(entered.wait(), 3)
            assert await scheduler.cancel_agent(1) is True
            assert first.done() and disp._recovery_in_flight == {}
            assert 1 not in scheduler.active_agents
            await disp.scan_once()
            second = disp._recovery_in_flight[1]
            assert second is not first
            disp._release_recovery_slot(1, first)  # A late callback cannot remove a newer slot.
            assert disp._recovery_in_flight[1] is second
            release.set()
            await poll_until_async(lambda: disp._recovery_in_flight == {}, timeout=3)
        finally:
            release.set()
            await scheduler.aclose()

    async def test_prestart_reap_transfers_recovery_slot_until_replacement_finishes(self) -> None:
        entered: list[int] = []
        release = asyncio.Event()

        async def run_turn(agent_id: int) -> None:
            entered.append(agent_id)
            await release.wait()

        pending = [dispatcher.PendingInboundWake(1, False, True)]
        observed: list[bool] = []

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            if pending[0].agent_id == 2 and scheduler.task_for(1) is original:
                observed.append(original.done() and scheduler.reaped_successor(1) is None)
                assert set(disp._recovery_in_flight) == {1}
            return pending

        scheduler = TurnScheduler(run_turn)
        disp = InboundWakeDispatcher(
            EventBus.from_settings(),
            scheduler,
            pending_scan=_pending,
            stale_after_s=180.0,
            recovery_wake_inflight=1,
        )
        try:
            await disp.scan_once()
            original = disp._recovery_in_flight[1]
            pending[:] = [dispatcher.PendingInboundWake(2, False, True)]
            racing_scan = asyncio.create_task(disp.scan_once())
            assert await scheduler.cancel_agent(1) is True
            await racing_scan
            assert observed == [True] and scheduler.task_for(2) is None
            successor = scheduler.task_for(1)
            assert successor is scheduler.reaped_successor(1) is disp._recovery_in_flight[1]
            await disp.scan_once()
            await poll_until_async(lambda: entered == [1], timeout=3)
            assert scheduler.task_for(2) is None and disp._recovery_in_flight == {1: successor}
            release.set()
            await poll_until_async(lambda: disp._recovery_in_flight == {}, timeout=3)
            await disp.scan_once()
            assert set(disp._recovery_in_flight) == {2}
            await poll_until_async(lambda: entered == [1, 2], timeout=3)
            await poll_until_async(lambda: disp._recovery_in_flight == {}, timeout=3)
        finally:
            release.set()
            await scheduler.aclose()

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
            EventBus.from_settings(),
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
            EventBus.from_settings(),
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
        scheduler = ScanScheduler()
        pending = [dispatcher.PendingInboundWake(agent_id=1, stale=False, recovery=True)]

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return pending

        disp = InboundWakeDispatcher(
            EventBus.from_settings(),
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
            EventBus.from_settings(), scheduler, pending_scan=_pending, stale_after_s=180.0
        )
        await disp.scan_once()
        await disp.scan_once()
        assert calls == []
        assert scheduler.active_agents == set()
        assert disp._recovery_in_flight == {}

    async def test_failed_recovery_turn_releases_slot_on_next_scan(self) -> None:
        attempted: list[int] = []
        failures: list[RuntimeError] = []

        async def run_turn(agent_id: int) -> None:
            attempted.append(agent_id)
            failure = RuntimeError("failed before admission")
            failures.append(failure)
            raise failure

        scheduler = TurnScheduler(run_turn)
        pending = [dispatcher.PendingInboundWake(agent_id=1, stale=False, recovery=True)]

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return pending

        disp = InboundWakeDispatcher(
            EventBus.from_settings(),
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
            with pytest.raises(ExceptionGroup) as joined:
                await scheduler.aclose()
            assert joined.value.exceptions == tuple(failures)

    async def test_recovery_cap_drains_across_scans_without_delaying_work(self) -> None:
        scheduler = ScanScheduler()
        remaining = set(range(1, 7))

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return [
                dispatcher.PendingInboundWake(agent_id=agent_id, stale=False, recovery=True)
                for agent_id in sorted(remaining)
            ] + [dispatcher.PendingInboundWake(agent_id=99, stale=False)]

        disp = InboundWakeDispatcher(
            EventBus.from_settings(),
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
        scheduler = ScanScheduler({1})

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return [
                dispatcher.PendingInboundWake(agent_id=1, stale=False, recovery=True),
                dispatcher.PendingInboundWake(agent_id=2, stale=False, recovery=True),
                dispatcher.PendingInboundWake(agent_id=3, stale=False, recovery=True),
            ]

        disp = InboundWakeDispatcher(
            EventBus.from_settings(),
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
        scheduler = ScanScheduler({3})

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return [
                dispatcher.PendingInboundWake(agent_id=1, stale=False, recovery=True),
                dispatcher.PendingInboundWake(agent_id=2, stale=False, recovery=True),
                dispatcher.PendingInboundWake(agent_id=3, stale=True, recovery=True),
            ]

        disp = InboundWakeDispatcher(
            EventBus.from_settings(),
            scheduler,
            pending_scan=_pending,
            stale_after_s=180.0,
            recovery_wake_batch=1,
            turn_progress=FixedClock(3600.0),
        )
        await disp.scan_once()
        assert scheduler.cancelled == [3]
        assert scheduler.woken == [1]

    async def test_small_recovery_cohort_is_all_woken_in_first_scan(self) -> None:
        scheduler = ScanScheduler()

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return [
                dispatcher.PendingInboundWake(agent_id=agent_id, stale=False, recovery=True)
                for agent_id in (1, 2)
            ]

        disp = InboundWakeDispatcher(
            EventBus.from_settings(),
            scheduler,
            pending_scan=_pending,
            stale_after_s=180.0,
            recovery_wake_batch=2,
        )
        await disp.scan_once()
        assert scheduler.woken == [1, 2]


pytestmark = pytest.mark.usefixtures(isolated_clocks.__name__)
