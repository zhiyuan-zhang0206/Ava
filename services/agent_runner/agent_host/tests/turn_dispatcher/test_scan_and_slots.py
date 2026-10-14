"""Turn dispatcher cases: scheduler slot release, wake scan and restart escalation."""

from __future__ import annotations

import asyncio
import threading

import pytest

from base.config import settings
from base.events.live.bus import EventBus
from services.agent_runner.agent_host import dispatcher
from services.agent_runner.agent_host.dispatcher import (
    InboundWakeDispatcher,
    TurnScheduler,
    agent_id_from_channel,
)
from services.agent_runner.agent_host.tests.turn_dispatcher.helpers import (
    QueueingPubSub,
    Recorder,
    StuckTurn,
    patch_redis,
    releasing_stuck_turn,
    settle,
)
from services.agent_runner.agent_host.tests.turn_dispatcher.scan_setup import ScanScheduler


class TestCancelBeforeTheFirstSlice:
    """A cancel landing before the task's first loop slice must not leave a
    phantom slot (task #3085, found in the PR #2217 review): `_pump`'s
    `finally` never runs for a task that never started, so the reaper callback
    releases the slot and re-arms the wake the task never consumed."""

    async def test_force_cancel_before_the_first_slice_releases_the_slot(self) -> None:
        rec = Recorder()
        sched = TurnScheduler(rec)

        async def _validate(_agent_id: int, _command_id: int) -> bool:
            return True

        sched.wake(7)
        assert await asyncio.wait_for(sched.cancel_exact_force(7, 1, _validate), 2) is True
        await settle()

        assert rec.started == [7]
        assert sched.active_agents == frozenset()

    async def test_aclose_does_not_rearm_after_a_prestart_cancel(self) -> None:
        """Shutdown must stay quiet: the closed flag outranks any pending wake,
        so a pre-start cancel during `aclose` never starts a successor."""
        rec = Recorder()
        sched = TurnScheduler(rec)

        sched.wake(7)
        await sched.aclose()
        await settle()

        assert rec.started == [], "a closed scheduler must not start a successor"
        assert sched.active_agents == frozenset()


async def test_stuck_fixture_releases_after_failed_assertion() -> None:
    """A useful failure must not strand an unkillable task in pytest teardown."""
    turn: StuckTurn | None = None
    sched: TurnScheduler | None = None
    try:
        with pytest.raises(AssertionError, match="injected assertion failure"):
            async with releasing_stuck_turn() as turn:
                sched = TurnScheduler(turn)
                sched.wake(5)
                await asyncio.wait_for(turn.entered.wait(), 2)
                raise AssertionError("injected assertion failure")
        assert turn is not None
        assert sched is not None
        assert turn._task is not None and turn._task.done()
        assert 5 not in sched.active_agents
    finally:
        # Independent rescue keeps a broken context-manager regression bounded.
        if turn is not None:
            await turn.release()


async def _validate_force(_agent_id: int, _command_id: int) -> bool:
    return True


async def test_same_incarnation_settlement_precedes_next_turn_after_cancel_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The existing single-flight slot survives a delayed to_thread unwind."""
    monkeypatch.setattr(dispatcher, "CANCEL_UNWIND_TIMEOUT_S", 0.01)
    entered, next_turn = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    events: list[str] = []

    async def run(_agent_id: int) -> None:
        if not events:
            events.append("turn1")
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                assert await asyncio.to_thread(release.wait, 2)
            finally:
                events.append("settled1")
        else:
            events.append("turn2")
            next_turn.set()

    scheduler = TurnScheduler(run)
    try:
        scheduler.wake(1)
        await asyncio.wait_for(entered.wait(), 2)
        await scheduler.cancel_exact_force(1, 1, _validate_force)
        scheduler.wake(1)
        await asyncio.sleep(0)
        assert events == ["turn1"]
        assert scheduler.active_agents == frozenset({1})
        release.set()
        await asyncio.wait_for(next_turn.wait(), 2)
        assert events == ["turn1", "settled1", "turn2"]
    finally:
        release.set()
        await scheduler.aclose()


class TestChannelParsing:
    @pytest.mark.parametrize(
        ("channel", "expected"),
        [
            ("ava:inbound:42", 42),
            ("ava:inbound:1", 1),
            ("someprefix:inbound:9999", 9999),
        ],
    )
    def test_valid_channels(self, channel: str, expected: int) -> None:
        assert agent_id_from_channel(channel) == expected

    @pytest.mark.parametrize(
        "channel",
        ["ava:inbound:", "ava:inbound:abc", "nocolons", "ava:inbound:12x"],
    )
    def test_malformed_channel_is_none_not_a_raise(self, channel: str) -> None:
        """One bad frame must not kill the subscription every other agent shares."""
        assert agent_id_from_channel(channel) is None


class TestPatternMatchesTheRealChannel:
    def test_pattern_covers_what_inbound_channel_publishes(self) -> None:
        """The subscription must cover published channels; drift silently
        leaves agents asleep despite successful publishes."""
        from fnmatch import fnmatchcase

        from base.cluster import inbound_channel, redis_channel_prefix
        from services.agent_runner.agent_host.dispatcher import INBOUND_PATTERN_SUFFIX

        pattern = (
            f"{redis_channel_prefix(settings.data_plane.events_channel)}{INBOUND_PATTERN_SUFFIX}"
        )
        for agent_id in (1, 42, 999999):
            channel = inbound_channel(agent_id, settings.data_plane.events_channel)
            assert fnmatchcase(channel, pattern), f"{channel!r} not covered by {pattern!r}"
            assert agent_id_from_channel(channel) == agent_id


class TestDispatcherMessageHandling:
    def _dispatcher(self) -> tuple[InboundWakeDispatcher, list[int]]:
        woken: list[int] = []

        class _Sched:
            def wake(self, agent_id: int) -> None:
                woken.append(agent_id)

        return InboundWakeDispatcher(EventBus.from_settings(), _Sched()), woken  # pyright: ignore[reportArgumentType]

    def test_pmessage_wakes_its_agent(self) -> None:
        disp, woken = self._dispatcher()
        disp._handle({"type": "pmessage", "channel": "ava:inbound:42", "data": "x"})
        assert woken == [42]

    def test_non_pmessage_frames_are_ignored(self) -> None:
        """psubscribe confirmations and plain messages share the stream."""
        disp, woken = self._dispatcher()
        disp._handle({"type": "psubscribe", "channel": "ava:inbound:*", "data": 1})
        assert woken == []

    def test_unparseable_channel_is_dropped_quietly(self) -> None:
        disp, woken = self._dispatcher()
        disp._handle({"type": "pmessage", "channel": "ava:inbound:oops", "data": "x"})
        assert woken == []


class TestPendingScan:
    async def test_scan_wakes_pending_inbound_even_when_pubsub_missed_it(self) -> None:
        """Redis notification loss may cost latency, never durable work."""
        scheduler = ScanScheduler()

        async def _pending() -> list[int]:
            return [23]

        disp = InboundWakeDispatcher(EventBus.from_settings(), scheduler, pending_scan=_pending)

        await disp.scan_once()

        assert scheduler.woken == [23]

    async def test_scan_defers_through_the_stop_window_and_runs_after_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stop window leaves rows untouched; the scan runs once it closes."""
        from base.deploy.maintenance import admission

        state = {"quiesced": True}
        monkeypatch.setattr(admission, "quiesced", lambda: state["quiesced"])
        scanner_calls: list[int] = []
        scheduler = ScanScheduler()

        async def _pending() -> list[int]:
            scanner_calls.append(1)
            return [23]

        disp = InboundWakeDispatcher(EventBus.from_settings(), scheduler, pending_scan=_pending)

        await disp.scan_once()
        assert scanner_calls == []
        assert scheduler.woken == []

        state["quiesced"] = False
        await disp.scan_once()
        assert scheduler.woken == [23]


class TestStallRestartEscalation:
    """Task #2417, half 2: a stalled turn that refuses its bounded unwind must
    not be rescheduled beside itself — the turn task raises the escalation out
    of the pump, the scheduler flags it, and the dispatcher loop exits."""

    async def test_a_refused_unwind_marks_the_scheduler_for_restart(self) -> None:
        class _RefusingTurn:
            async def __call__(self, _agent_id: int) -> None:
                raise dispatcher.HostRestartRequiredError("refused")

        sched = TurnScheduler(_RefusingTurn())
        sched.wake(23)
        await settle()
        assert sched.restart_required is True

    async def test_a_clean_stall_abort_does_not_request_a_restart(self) -> None:
        class _CleanAbort:
            async def __call__(self, _agent_id: int) -> None:
                raise dispatcher.TurnStallTimeoutError(23)

        sched = TurnScheduler(_CleanAbort())
        sched.wake(23)
        await settle()
        assert sched.restart_required is False

    async def test_the_dispatcher_loop_exits_when_restart_is_required(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pubsub = QueueingPubSub()
        patch_redis(monkeypatch, pubsub)
        scheduler = ScanScheduler(restart_required=True)

        async def _pending() -> list[int]:
            return []

        disp = InboundWakeDispatcher(
            EventBus.from_settings(),
            scheduler,
            pending_scan=_pending,
            scan_interval_s=0.005,
            subscription_read_timeout_s=0.005,
            reconnect_delay_s=0.0,
        )
        with pytest.raises(dispatcher.HostRestartRequiredError, match="supervisor recovery"):
            await asyncio.wait_for(disp.run(), timeout=2.0)


class TestScanNeverCancelsATurn:
    """The pending scan only wakes: a running turn that makes no progress is the
    in-graph stall guard's to abort, never the dispatcher's."""

    async def test_a_scan_wake_for_a_running_turn_defers_to_it_instead_of_cancelling(
        self,
    ) -> None:
        cancelled: list[int] = []
        entered, release = asyncio.Event(), asyncio.Event()
        runs: list[int] = []

        async def run_turn(agent_id: int) -> None:
            runs.append(agent_id)
            if len(runs) == 1:
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.append(agent_id)
                    raise

        scheduler = TurnScheduler(run_turn)
        scheduler.wake(23)
        await asyncio.wait_for(entered.wait(), 2)
        first = scheduler.task_for(23)

        async def _pending() -> list[int]:
            return [23]

        disp = InboundWakeDispatcher(EventBus.from_settings(), scheduler, pending_scan=_pending)
        await disp.scan_once()

        assert cancelled == [], "the dispatcher scan must not cancel a running turn"
        assert scheduler.task_for(23) is first and first is not None and not first.done()
        release.set()
        await settle()
        assert runs == [23, 23], "the scan wake is served by one more turn after the first ends"
        await scheduler.aclose()
