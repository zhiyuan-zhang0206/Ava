"""Hosted-runner wake dispatch — `services/agent_host/dispatcher.py`.

Phase 1 work item (a) of `future/infra/agent-runner-as-server.md`. The contracts
locked here are the ones the hosted model cannot be correct without:

1. **A wake starts a turn** — and an idle agent has no task at all, which is the
   whole point of the model.
2. **One agent's turns serialize; different agents overlap.** The design allows
   concurrency across agents and forbids it within one, because a single agent's
   turns share a checkpointer thread.
3. **The wake race** — a wake landing while a turn is winding down must still be
   served. This is the subtle one, and it is why the scheduler exists at all.
4. **Wakes coalesce** — two wakes during one turn cause one more turn, not two.
5. **A crashing turn does not wedge its agent or the host.**
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

import pytest

from services.agent_host import dispatcher
from services.agent_host.dispatcher import (
    InboundWakeDispatcher,
    TurnScheduler,
    agent_id_from_channel,
)


class _Recorder:
    """A `run_turn` that records calls and can be released on command."""

    def __init__(self) -> None:
        self.started: list[int] = []
        self.finished: list[int] = []
        self.gates: dict[int, asyncio.Event] = {}
        self.entered: dict[int, asyncio.Event] = {}

    def gate(self, agent_id: int) -> asyncio.Event:
        return self.gates.setdefault(agent_id, asyncio.Event())

    def arrival(self, agent_id: int) -> asyncio.Event:
        return self.entered.setdefault(agent_id, asyncio.Event())

    async def __call__(self, agent_id: int) -> None:
        self.started.append(agent_id)
        self.arrival(agent_id).set()
        gate = self.gates.get(agent_id)
        if gate is not None:
            await gate.wait()
            gate.clear()
        self.finished.append(agent_id)


async def _settle() -> None:
    """Let every ready callback run. Two hops: one for the task to be scheduled,
    one for it to run to its next await."""
    for _ in range(6):
        await asyncio.sleep(0)


class TestBasicDispatch:
    async def test_wake_starts_a_turn_and_the_task_ends_when_idle(self) -> None:
        rec = _Recorder()
        sched = TurnScheduler(rec)

        assert sched.active_agents == frozenset(), "an idle agent must have no task"
        sched.wake(7)
        await _settle()

        assert rec.started == [7]
        # The turn returned (nothing gated it), so the agent is idle again —
        # idle costs nothing because there is nothing.
        assert sched.active_agents == frozenset()

    async def test_second_wake_after_idle_starts_a_fresh_turn(self) -> None:
        rec = _Recorder()
        sched = TurnScheduler(rec)

        sched.wake(7)
        await _settle()
        sched.wake(7)
        await _settle()

        assert rec.started == [7, 7]


class TestSerializationAndConcurrency:
    async def test_one_agents_turns_never_overlap(self) -> None:
        """The same agent's turns must serialize — they share a checkpointer
        thread. A wake during a running turn may not start a second task."""
        rec = _Recorder()
        rec.gate(7)  # turn 7 blocks until released
        sched = TurnScheduler(rec)

        sched.wake(7)
        await _settle()
        assert rec.started == [7]

        sched.wake(7)  # arrives mid-turn
        await _settle()
        assert rec.started == [7], "a second concurrent turn was started for one agent"
        assert len(sched.active_agents) == 1

        rec.gate(7).set()
        await _settle()
        assert rec.started == [7, 7], "the mid-turn wake did not produce a follow-up turn"

    async def test_different_agents_run_concurrently(self) -> None:
        """Cross-agent concurrency is the point of the model — one blocked agent
        must not hold up another."""
        rec = _Recorder()
        rec.gate(1)
        rec.gate(2)
        sched = TurnScheduler(rec)

        sched.wake(1)
        sched.wake(2)
        await _settle()

        assert sorted(rec.started) == [1, 2]
        assert sched.active_agents == frozenset({1, 2})

        rec.gate(1).set()
        rec.gate(2).set()
        await _settle()
        assert sched.active_agents == frozenset()


class TestWakeRace:
    async def test_wake_landing_as_the_turn_winds_down_is_not_lost(self) -> None:
        """THE race (see the module docstring of dispatcher.py).

        The wake is delivered after the turn body has finished its work but
        before the task object is gone. Without the wake-pending flag the
        dispatcher would see a live task, assume it would pick the inbound up,
        and the row would sit unclaimed. With the flag, the pump re-checks and
        loops.

        `release_then_wake` reproduces the interleaving deterministically: it
        releases the gate (so the turn body returns) and wakes in the SAME
        synchronous stretch, which is exactly the window the flag protects.
        """
        rec = _Recorder()
        rec.gate(7)
        sched = TurnScheduler(rec)

        sched.wake(7)
        await _settle()
        assert rec.started == [7]

        # The turn is about to finish AND a wake arrives — the exact overlap.
        rec.gate(7).set()
        sched.wake(7)
        await _settle()

        assert rec.started == [7, 7], "the wake was swallowed by the winding-down turn"

    async def test_wake_after_the_task_is_gone_starts_a_new_one(self) -> None:
        """The other side of the same window: once the task has been removed, a
        wake must start a fresh task rather than be recorded against nothing."""
        rec = _Recorder()
        sched = TurnScheduler(rec)

        sched.wake(7)
        await _settle()
        assert sched.active_agents == frozenset()

        sched.wake(7)
        await _settle()
        assert rec.started == [7, 7]

    async def test_two_wakes_during_one_turn_coalesce(self) -> None:
        """Two wakes are one reason to run again: the follow-up turn drains
        whatever is pending, so a chat burst must not queue a turn burst."""
        rec = _Recorder()
        rec.gate(7)
        sched = TurnScheduler(rec)

        sched.wake(7)
        await _settle()
        sched.wake(7)
        sched.wake(7)
        sched.wake(7)
        rec.gate(7).set()
        await _settle()

        assert rec.started == [7, 7], f"wakes did not coalesce: {rec.started}"


class TestFailureIsolation:
    async def test_a_crashing_turn_drops_the_task_without_killing_the_host(self) -> None:
        """A turn that raises must not wedge its agent (the task has to be
        removed so the next wake can start one) and must not propagate."""
        calls: list[int] = []

        async def boom(agent_id: int) -> None:
            calls.append(agent_id)
            raise RuntimeError("turn exploded")

        sched = TurnScheduler(boom)
        sched.wake(7)
        await _settle()

        assert calls == [7]
        assert sched.active_agents == frozenset(), "a crashed turn left its agent wedged"

        sched.wake(7)  # the agent is still serviceable
        await _settle()
        assert calls == [7, 7]

    async def test_aclose_cancels_running_turns(self) -> None:
        rec = _Recorder()
        rec.gate(7)
        sched = TurnScheduler(rec)
        sched.wake(7)
        await _settle()
        assert sched.active_agents == frozenset({7})

        await sched.aclose()

        assert sched.active_agents == frozenset()
        # Closed: further wakes are ignored rather than resurrecting a task
        # during shutdown.
        sched.wake(7)
        await _settle()
        assert sched.active_agents == frozenset()


class _StuckTurn:
    """A turn that refuses the cancel, then can be released.

    Swallowing `CancelledError` forever is the faithful stand-in for a task
    blocked in a C call — and it is also unkillable at loop teardown, so a test
    using one hangs the whole run instead of failing. `release()` re-arms
    cancellation so the test can clean up after asserting.
    """

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self._released = False
        self._task: asyncio.Task[None] | None = None

    async def __call__(self, _agent_id: int) -> None:
        self._task = asyncio.current_task()
        self.entered.set()
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                if self._released:
                    raise
                continue  # refuses to unwind, exactly like a blocked C call

    async def release(self) -> None:
        """Let the turn die, so the loop can close on a clean task set."""
        self._released = True
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


async def _aclose_without_hanging(sched: TurnScheduler, stuck: _StuckTurn) -> bool:
    """Run `sched.aclose()` and report whether it RETURNED, never hanging.

    `asyncio.wait`, not `wait_for`: the pre-bound implementation sat in
    `await task` under a `suppress(CancelledError, ...)` which ATE the
    cancellation `wait_for` sends, so `wait_for` could not rescue the test and
    the whole run hung instead of failing. Waiting without cancelling turns "it
    never returned" into a boolean the caller can assert on.

    Cleanup order is load-bearing: releasing the turn is what lets the pump task
    die, which is what lets a still-parked `aclose` finish. Cancelling `aclose`
    first cannot do it, for the same reason `wait_for` could not.
    """
    closing = asyncio.create_task(sched.aclose())
    try:
        done, _ = await asyncio.wait([closing], timeout=5)
        return closing in done
    finally:
        await stuck.release()
        closing.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await closing


class TestUncancellableTurn:
    """`aclose` must be BOUNDED, and must name what would not unwind.

    `Task.cancel()` lands at the next await, so a turn blocked where asyncio
    cannot interrupt it — a C call — never unwinds. The old `await task` waited
    on that forever: shutdown hung until the supervisor SIGKILLed the host, and
    nothing said which agent was stuck. These lock the replacement.
    """

    async def test_aclose_returns_even_when_a_turn_refuses_to_unwind(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The load-bearing one: shutdown completes rather than hanging.

        The turn swallows its CancelledError and keeps awaiting — a faithful
        stand-in for a task that cannot be interrupted, and one that hangs the
        unbounded version of this method forever.
        """
        monkeypatch.setattr(dispatcher, "CANCEL_UNWIND_TIMEOUT_S", 0.05)
        stuck = _StuckTurn()
        sched = TurnScheduler(stuck)
        sched.wake(7)
        await asyncio.wait_for(stuck.entered.wait(), 2)

        returned = await _aclose_without_hanging(sched, stuck)

        assert returned, (
            "aclose never returned — a turn that refuses to unwind is hanging shutdown, "
            "which is the failure the bounded wait exists to prevent"
        )
        assert sched.active_agents == frozenset(), "aclose must not leave the registry populated"

    async def test_the_straggler_is_named_in_the_event_river(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ "Is anything wedged right now" must be answerable without reading
        logs, so the report carries the agent id and how long the cancel was
        pending as event fields."""
        monkeypatch.setattr(dispatcher, "CANCEL_UNWIND_TIMEOUT_S", 0.05)
        records: list[dict[str, object]] = []

        def _capture(_msg: str, **kw: object) -> None:
            records.append(kw)

        monkeypatch.setattr(dispatcher.logger, "error", _capture)
        turn = _StuckTurn()
        sched = TurnScheduler(turn)
        sched.wake(31)
        await asyncio.wait_for(turn.entered.wait(), 2)

        assert await _aclose_without_hanging(sched, turn), "aclose never returned"

        reports = [r for r in records if r.get("event") == "host_turn_uncancellable"]
        assert len(reports) == 1, "exactly one report per stuck agent"
        assert reports[0]["agent_id"] == 31
        assert isinstance(reports[0]["waited_s"], float)

    async def test_a_turn_that_unwinds_cleanly_reports_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ordinary case must stay silent, or the report is noise nobody
        reads by the time it matters."""
        monkeypatch.setattr(dispatcher, "CANCEL_UNWIND_TIMEOUT_S", 0.05)
        records: list[dict[str, object]] = []

        def _capture(_msg: str, **kw: object) -> None:
            records.append(kw)

        monkeypatch.setattr(dispatcher.logger, "error", _capture)

        rec = _Recorder()
        rec.gate(7)
        sched = TurnScheduler(rec)
        sched.wake(7)
        await _settle()
        await asyncio.wait_for(sched.aclose(), 5)

        assert [r for r in records if r.get("event") == "host_turn_uncancellable"] == []

    async def test_the_report_carries_the_real_activity_clock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A wedge report has to answer "how long has this agent actually been
        silent", or a slow shutdown and a genuine wedge look identical.

        The clock is `agents_meta.last_active_at` — written on every completed
        LLM step — NOT the `/api/agents` field of the same name, which is
        `MAX(inbound_messages.created_at)` and goes stale during exactly the long
        turns where the question is real (issue #183).
        """
        monkeypatch.setattr(dispatcher, "CANCEL_UNWIND_TIMEOUT_S", 0.05)
        records: list[dict[str, object]] = []

        def _capture(_msg: str, **kw: object) -> None:
            records.append(kw)

        monkeypatch.setattr(dispatcher.logger, "error", _capture)
        silent_since = datetime.now(UTC) - timedelta(minutes=20)

        async def _clock(_agent_id: int) -> datetime:
            return silent_since

        turn = _StuckTurn()
        sched = TurnScheduler(turn, activity_clock=_clock)
        sched.wake(5)
        await asyncio.wait_for(turn.entered.wait(), 2)

        assert await _aclose_without_hanging(sched, turn), "aclose never returned"

        report = next(r for r in records if r.get("event") == "host_turn_uncancellable")
        assert report["last_active_at"] == silent_since.isoformat()
        idle_s = report["idle_s"]
        assert isinstance(idle_s, float)
        assert idle_s > 1000, "a 20-minute-silent agent must not read as freshly active"

    async def test_a_failing_clock_read_still_reports_the_wedge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The clock is an enrichment; the agent id and pending duration are the
        part that cannot be reconstructed later. A DB that is unreachable — quite
        possibly what the turn is stuck on — must not cost the whole report."""
        monkeypatch.setattr(dispatcher, "CANCEL_UNWIND_TIMEOUT_S", 0.05)
        records: list[dict[str, object]] = []

        def _capture(_msg: str, **kw: object) -> None:
            records.append(kw)

        monkeypatch.setattr(dispatcher.logger, "error", _capture)

        async def _broken_clock(_agent_id: int) -> datetime:
            raise RuntimeError("db unreachable")

        turn = _StuckTurn()
        sched = TurnScheduler(turn, activity_clock=_broken_clock)
        sched.wake(9)
        await asyncio.wait_for(turn.entered.wait(), 2)

        assert await _aclose_without_hanging(sched, turn), "aclose never returned"

        report = next(r for r in records if r.get("event") == "host_turn_uncancellable")
        assert report["agent_id"] == 9
        assert isinstance(report["waited_s"], float)
        assert report["last_active_at"] is None, "an unreadable clock reports as absent"
        assert report["idle_s"] is None

    async def test_a_hanging_clock_read_does_not_delay_shutdown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same reasoning one step further: a clock read that never returns is
        the shutdown-hang this whole class exists to prevent, reintroduced
        through the diagnostic. It is bounded too."""
        monkeypatch.setattr(dispatcher, "CANCEL_UNWIND_TIMEOUT_S", 0.05)
        monkeypatch.setattr(dispatcher, "CLOCK_READ_TIMEOUT_S", 0.05)
        records: list[dict[str, object]] = []

        def _capture(_msg: str, **kw: object) -> None:
            records.append(kw)

        def _swallow(_msg: str, **_kw: object) -> None:
            """The clock-read failure warning; not what this test is asserting."""

        monkeypatch.setattr(dispatcher.logger, "error", _capture)
        monkeypatch.setattr(dispatcher.logger, "warning", _swallow)

        async def _hanging_clock(_agent_id: int) -> datetime:
            await asyncio.sleep(3600)
            raise AssertionError("unreachable")

        turn = _StuckTurn()
        sched = TurnScheduler(turn, activity_clock=_hanging_clock)
        sched.wake(12)
        await asyncio.wait_for(turn.entered.wait(), 2)

        assert await _aclose_without_hanging(sched, turn), "a hanging clock read blocked shutdown"

        report = next(r for r in records if r.get("event") == "host_turn_uncancellable")
        assert report["agent_id"] == 12
        assert report["last_active_at"] is None

    def test_the_unwind_bound_fits_inside_the_stop_paths_kill_window(self) -> None:
        """The bound is only useful if the host survives long enough to emit the
        report. `ava stop` SIGTERMs each agent process and force-kills after
        `_reap_agent_sessions(timeout_s=...)`, so this wait plus the rest of
        shutdown has to fit inside that window.

        Pinned against the stop path's ACTUAL default rather than a number
        copied into a comment, so moving either side surfaces here.
        """
        import inspect

        from cli.commands.stop import _reap_agent_sessions

        kill_window = inspect.signature(_reap_agent_sessions).parameters["timeout_s"].default
        assert kill_window > dispatcher.CANCEL_UNWIND_TIMEOUT_S, (
            f"the host waits {dispatcher.CANCEL_UNWIND_TIMEOUT_S}s for a turn to unwind but "
            f"the stop path force-kills after {kill_window}s — the uncancellable-turn report "
            "would never be emitted"
        )
        # Both waits, summed — the cancel wait, and then the activity-clock read
        # that enriches the report. This replaces a `2 *` proxy for "leave room
        # for the rest of shutdown" now that there is a real second term; the
        # remaining headroom covers closing the pool, the healthz server and the
        # pidfile.
        total_wait = dispatcher.CANCEL_UNWIND_TIMEOUT_S + dispatcher.CLOCK_READ_TIMEOUT_S
        assert kill_window > total_wait * 2, (
            f"the host can wait {total_wait}s before it even starts closing its handles, "
            f"against a {kill_window}s force-kill window — too little headroom for the "
            "rest of shutdown"
        )


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
        """The dispatcher's PSUBSCRIBE pattern and the publisher's channel name
        are derived from the same prefix, and this asserts they actually meet:
        a drift here is silent (no error, just a runner that never wakes), which
        is exactly the failure `inbound_channel` was centralised to prevent."""
        from fnmatch import fnmatchcase

        from services.agent_host.dispatcher import _INBOUND_PATTERN_SUFFIX
        from shared.cluster import inbound_channel, redis_channel_prefix

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

        return InboundWakeDispatcher("redis://unused", _Sched()), woken  # pyright: ignore[reportArgumentType]

    def test_pmessage_wakes_its_agent(self) -> None:
        disp, woken = self._dispatcher()
        disp._handle({"type": "pmessage", "channel": "ava:inbound:42", "data": "x"})  # pyright: ignore[reportPrivateUsage]
        assert woken == [42]

    def test_non_pmessage_frames_are_ignored(self) -> None:
        """psubscribe confirmations and plain messages share the stream."""
        disp, woken = self._dispatcher()
        disp._handle({"type": "psubscribe", "channel": "ava:inbound:*", "data": 1})  # pyright: ignore[reportPrivateUsage]
        assert woken == []

    def test_unparseable_channel_is_dropped_quietly(self) -> None:
        disp, woken = self._dispatcher()
        disp._handle({"type": "pmessage", "channel": "ava:inbound:oops", "data": "x"})  # pyright: ignore[reportPrivateUsage]
        assert woken == []
