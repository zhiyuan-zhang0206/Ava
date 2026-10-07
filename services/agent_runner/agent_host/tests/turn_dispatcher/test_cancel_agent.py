"""Turn dispatcher cases: cancel agent."""

from __future__ import annotations

import asyncio
import threading

import pytest

from base.events.live.bus import EventBus
from services.agent_runner.agent_host import dispatcher
from services.agent_runner.agent_host.dispatcher import (
    InboundWakeDispatcher,
    TurnScheduler,
    agent_id_from_channel,
)
from services.agent_runner.agent_host.scheduling.admission import TurnAdmission
from services.agent_runner.agent_host.tests.test_turn_dispatcher import (
    _FRESH,
    _STALE,
    _UNKNOWN,
    _patch_redis,
    _QueueingPubSub,
    _Recorder,
    _releasing_stuck_turn,
    _scan_dispatcher,
    _ScanScheduler,
    _settle,
    _StuckTurn,
)


class TestCancelAgent:
    async def test_cancel_agent_cancels_a_running_turn(self) -> None:
        """The hosted force-terminate primitive: one agent's turn task is
        cancelled at its next await point, the task unwinds and leaves the
        registry — other agents' turns are untouched."""
        rec = _Recorder()
        sched = TurnScheduler(rec)
        rec.gate(7)  # hold turn 7 open so the cancel lands on a RUNNING turn
        rec.gate(8)  # hold turn 8 open too — it must survive 7's cancel
        sched.wake(7)
        sched.wake(8)
        await rec.arrival(7).wait()
        await rec.arrival(8).wait()

        assert await asyncio.wait_for(sched.cancel_agent(7), 2) is True
        await _settle()

        assert 7 not in sched.active_agents, "the cancelled turn must unwind out of the registry"
        assert 8 in sched.active_agents, "another agent's turn must not be touched"

        # clean up agent 8
        rec.gate(8).set()
        await _settle()

    async def test_cancel_agent_without_a_task_returns_false(self) -> None:
        """False means "nothing to accelerate", never an error — the ops caller
        treats a host with no task for this agent as already done."""
        sched = TurnScheduler(_Recorder())
        assert await asyncio.wait_for(sched.cancel_agent(42), 2) is False

    async def test_cancel_agent_reports_a_stuck_turn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A turn blocked where asyncio cannot interrupt it (a C call) refuses
        the cancel — the bound applies, the SAME uncancellable report as a full
        shutdown is emitted, and the task stays in the registry so a later wake
        does not double-schedule the agent."""
        monkeypatch.setattr(dispatcher, "CANCEL_UNWIND_TIMEOUT_S", 0.05)
        records: list[dict[str, object]] = []

        def _capture(_msg: str, **kw: object) -> None:
            records.append(kw)

        monkeypatch.setattr(dispatcher.logger, "error", _capture)
        async with _releasing_stuck_turn() as turn:
            sched = TurnScheduler(turn)
            sched.wake(5)
            await asyncio.wait_for(turn.entered.wait(), 2)

            assert await asyncio.wait_for(sched.cancel_agent(5), 2) is False
            report = next(r for r in records if r.get("event") == "host_turn_uncancellable")
            assert report["agent_id"] == 5
            assert 5 in sched.active_agents, "a wedged turn still owns its registry slot"
            original_task = turn._task
            sched.wake(5)
            await _settle()
            assert turn._task is original_task, "another wake must not replace the live task"
        assert 5 not in sched.active_agents, "only actual unwind releases the slot"


class TestCancelBeforeTheFirstSlice:
    """A cancel landing before the task's first loop slice must not leave a
    phantom slot (task #3085, found in the PR #2217 review): `_pump`'s
    `finally` never runs for a task that never started, so the reaper callback
    releases the slot and re-arms the wake the task never consumed."""

    async def test_the_unstarted_slot_is_released_and_the_wake_rearmed(self) -> None:
        rec = _Recorder()
        sched = TurnScheduler(rec)

        sched.wake(7)  # queued; not one slice has run
        assert await asyncio.wait_for(sched.cancel_agent(7), 2) is True
        await _settle()

        # Pre-fix the entry leaked: the agent stayed in `active_agents` (the
        # scan reads that as a straggler) and the recorded wake went nowhere.
        assert rec.started == [7], "the unconsumed wake must be re-armed, not dropped"
        assert sched.active_agents == frozenset()

    async def test_force_cancel_before_the_first_slice_releases_the_slot(self) -> None:
        rec = _Recorder()
        sched = TurnScheduler(rec)

        async def _validate(_agent_id: int, _command_id: int) -> bool:
            return True

        sched.wake(7)
        assert await asyncio.wait_for(sched.cancel_exact_force(7, 1, _validate), 2) is True
        await _settle()

        assert rec.started == [7]
        assert sched.active_agents == frozenset()

    async def test_aclose_does_not_rearm_after_a_prestart_cancel(self) -> None:
        """Shutdown must stay quiet: the closed flag outranks any pending wake,
        so a pre-start cancel during `aclose` never starts a successor."""
        rec = _Recorder()
        sched = TurnScheduler(rec)

        sched.wake(7)
        await sched.aclose()
        await _settle()

        assert rec.started == [], "a closed scheduler must not start a successor"
        assert sched.active_agents == frozenset()

    async def test_a_cancelled_before_start_turn_never_reads_as_a_straggler(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The scan's post-cancel check judges the captured task, not registry
        membership: pre-fix, the staging below exits the host with a false
        `did not unwind`."""
        rec = _Recorder()
        sched = TurnScheduler(rec)

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return [dispatcher.PendingInboundWake(agent_id=7, stale=True)]

        disp = _scan_dispatcher(sched, _pending, _STALE)

        sched.wake(7)
        await asyncio.wait_for(sched.cancel_agent(7), 2)
        await _settle()

        await disp.scan_once()  # must not raise: no straggler exists
        await _settle()

        assert rec.started.count(7) >= 1, "the scan must re-drive the agent it saw stale"


async def test_stuck_fixture_releases_after_failed_assertion() -> None:
    """A useful failure must not strand an unkillable task in pytest teardown."""
    turn: _StuckTurn | None = None
    sched: TurnScheduler | None = None
    try:
        with pytest.raises(AssertionError, match="injected assertion failure"):
            async with _releasing_stuck_turn() as turn:
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
        await scheduler.cancel_agent(1)
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
        from services.agent_runner.agent_host.dispatcher import _INBOUND_PATTERN_SUFFIX

        pattern = f"{redis_channel_prefix()}{_INBOUND_PATTERN_SUFFIX}"
        for agent_id in (1, 42, 999999):
            channel = inbound_channel(agent_id)
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
        scheduler = _ScanScheduler()

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return [dispatcher.PendingInboundWake(agent_id=23, stale=False)]

        disp = InboundWakeDispatcher(
            EventBus.from_settings(), scheduler, pending_scan=_pending, stale_after_s=180.0
        )

        await disp.scan_once()

        assert scheduler.woken == [23]
        assert scheduler.cancelled == []

    async def test_scan_defers_through_the_stop_leg_and_runs_from_the_start_leg(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stop leg leaves rows untouched; start leg scans even while held
        because pub/sub has no replay."""
        from base.deploy.maintenance import admission

        state = {"in_stop_leg": True}
        monkeypatch.setattr(admission, "in_stop_leg", lambda: state["in_stop_leg"])
        scanner_calls: list[int] = []
        scheduler = _ScanScheduler()

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            scanner_calls.append(1)
            return [dispatcher.PendingInboundWake(agent_id=23, stale=False)]

        disp = InboundWakeDispatcher(
            EventBus.from_settings(), scheduler, pending_scan=_pending, stale_after_s=180.0
        )

        await disp.scan_once()
        assert scanner_calls == []
        assert scheduler.woken == []

        state["in_stop_leg"] = False
        await disp.scan_once()
        assert scheduler.woken == [23]

    async def test_scan_cancels_a_stale_active_turn_before_rescheduling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Old pending work and an actually stalled turn allow recovery."""
        scheduler = _ScanScheduler({23})

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return [dispatcher.PendingInboundWake(agent_id=23, stale=True)]

        disp = _scan_dispatcher(scheduler, _pending, _STALE)

        await disp.scan_once()

        assert scheduler.cancelled == [23]
        assert scheduler.woken == [23]

    async def test_scan_requires_a_host_restart_when_stale_turn_will_not_unwind(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A task that survives bounded cancellation retains its one-turn slot.

        Exiting is the only safe recovery: scheduling another task beside it
        would let one agent claim and mutate its checkpoint concurrently.
        """
        scheduler = _ScanScheduler({23}, unwinds_on_cancel=False)

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return [dispatcher.PendingInboundWake(agent_id=23, stale=True)]

        disp = _scan_dispatcher(scheduler, _pending, _STALE)

        with pytest.raises(dispatcher.HostRestartRequiredError, match="did not unwind"):
            await disp.scan_once()

        assert scheduler.cancelled == [23]
        assert scheduler.woken == []


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
        await _settle()
        assert sched.restart_required is True

    async def test_a_clean_stall_abort_does_not_request_a_restart(self) -> None:
        class _CleanAbort:
            async def __call__(self, _agent_id: int) -> None:
                raise dispatcher.TurnStallTimeoutError(23)

        sched = TurnScheduler(_CleanAbort())
        sched.wake(23)
        await _settle()
        assert sched.restart_required is False

    async def test_the_dispatcher_loop_exits_when_restart_is_required(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pubsub = _QueueingPubSub()
        _patch_redis(monkeypatch, pubsub)
        scheduler = _ScanScheduler(restart_required=True)

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return []

        disp = InboundWakeDispatcher(
            EventBus.from_settings(),
            scheduler,
            pending_scan=_pending,
            stale_after_s=180.0,
            scan_interval_s=0.005,
            subscription_read_timeout_s=0.005,
            reconnect_delay_s=0.0,
        )
        with pytest.raises(dispatcher.HostRestartRequiredError, match="supervisor recovery"):
            await asyncio.wait_for(disp.run(), timeout=2.0)


class TestTurnLevelStaleScan:
    """Task #2417: an in-flight hosted turn whose turn-progress clock is stale
    is turn-level fake-alive even when NO pending inbound exists (agent 2998:
    claimed its whole queue, then hung inside graph.ainvoke for 3.5h). Pending
    rows and pids cannot see that shape; the in-flight set + the progress clock
    can."""

    async def test_stale_in_flight_turn_is_cancelled_and_rescheduled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scheduler = _ScanScheduler({23})

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return []

        disp = _scan_dispatcher(scheduler, _pending, _STALE)

        await disp.scan_once()

        assert scheduler.cancelled == [23]
        assert scheduler.woken == [23]

    async def test_a_fresh_in_flight_turn_is_left_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scheduler = _ScanScheduler({23})

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return []

        disp = _scan_dispatcher(scheduler, _pending, _FRESH)

        await disp.scan_once()

        assert scheduler.cancelled == []
        assert scheduler.woken == []

    async def test_an_unknown_progress_clock_is_never_stale(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fresh host has no clock entry for anyone: nothing means "no turn
        has ever marked progress", which must not cancel turns it knows nothing
        about — the same reading the uncancellable report uses for None."""
        scheduler = _ScanScheduler({23})

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return []

        disp = _scan_dispatcher(scheduler, _pending, _UNKNOWN)

        await disp.scan_once()

        assert scheduler.cancelled == []
        assert scheduler.woken == []

    async def test_stale_turn_that_will_not_unwind_requires_a_host_restart(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scheduler = _ScanScheduler({23}, unwinds_on_cancel=False)

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return []

        disp = _scan_dispatcher(scheduler, _pending, _STALE)

        with pytest.raises(dispatcher.HostRestartRequiredError, match="did not unwind"):
            await disp.scan_once()

        assert scheduler.cancelled == [23]
        assert scheduler.woken == []

    async def test_a_turn_completing_during_an_earlier_unwind_is_not_a_straggler(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The loop walks a snapshot of the active set; while an earlier
        candidate's cancel awaits its unwind, another turn can complete
        naturally. `cancel_agent` reports the missing task as False — nothing
        left to unwind, not a straggler — so the verdict must not raise a
        false HostRestartRequiredError (QA #3242, task #3085)."""
        order = list(frozenset({1, 2}))
        hang_id, quick_id = order[0], order[1]
        completed: list[int] = []
        cancelled: list[int] = []

        async def run_turn(agent_id: int) -> None:
            if agent_id == hang_id:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.append(agent_id)
                    await asyncio.sleep(0.2)  # slow unwind — the window
                    raise
            else:
                await asyncio.sleep(0.05)  # completes naturally mid-window
                completed.append(agent_id)

        scheduler = dispatcher.TurnScheduler(run_turn)
        scheduler.wake(hang_id)
        scheduler.wake(quick_id)
        for _ in range(6):
            await asyncio.sleep(0)

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return []

        disp = _scan_dispatcher(scheduler, _pending, _STALE)

        await disp.scan_once()  # must not raise

        assert cancelled == [hang_id]
        assert completed == [quick_id]
        await scheduler.aclose()


class TestAdmissionWaitExemption:
    """Task #3584: a turn queued at the admission gate must never be treated as
    a stalled turn. Its progress clock is silent by design, and cancelling a
    waiter would only re-queue it at the tail of the same queue — starvation
    amplification, not recovery."""

    async def test_a_queued_turn_is_exempt_from_the_turn_level_scan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scheduler = _ScanScheduler({23})
        admission = TurnAdmission(1)
        admission._waiting[23] = 0.0

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return []

        disp = _scan_dispatcher(scheduler, _pending, _STALE, turn_admission=admission)
        await disp.scan_once()

        assert scheduler.cancelled == []
        assert scheduler.woken == []

    async def test_a_queued_stale_candidate_is_not_cancelled_before_its_wake(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scheduler = _ScanScheduler({17})
        admission = TurnAdmission(1)
        admission._waiting[17] = 0.0

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return [dispatcher.PendingInboundWake(agent_id=17, stale=True)]

        disp = _scan_dispatcher(scheduler, _pending, _STALE, turn_admission=admission)
        await disp.scan_once()

        assert scheduler.cancelled == []
        assert scheduler.woken == [17]
