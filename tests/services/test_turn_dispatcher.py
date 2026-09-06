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
import threading
import time
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest

from services.agent_host import dispatcher
from services.agent_host.dispatcher import (
    InboundWakeDispatcher,
    TurnScheduler,
    agent_id_from_channel,
)
from services.agent_host.runtime import _active_turn_config_fingerprint


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

    async def test_a_wake_materializes_the_task_without_any_await(self) -> None:
        """The wake -> turn-task path is pure in-memory: `wake()` creates the
        task synchronously, with no await between the wake and task creation.
        That is the ms-level wake latency the hosted design calls for, asserted
        structurally (a wall-clock bound would be CI flake bait)."""
        rec = _Recorder()
        sched = TurnScheduler(rec)

        assert sched.active_agents == frozenset()
        sched.wake(7)
        assert 7 in sched.active_agents, "the turn task must exist before any await"
        await _settle()


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
    async def test_crash_event_carries_exception_type_and_config_fingerprint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        records: list[dict[str, object]] = []

        def _capture(_msg: str, **kw: object) -> None:
            records.append(kw)

        monkeypatch.setattr(dispatcher.logger, "exception", _capture)

        async def boom(_agent_id: int) -> None:
            _active_turn_config_fingerprint.set("stored-config-fingerprint")
            raise ValueError("turn exploded")

        sched = TurnScheduler(boom)
        sched.wake(7)
        await _settle()

        report = next(r for r in records if r.get("event") == "host_turn_crashed")
        assert report["exception_type"] == "ValueError"
        assert report["config_fingerprint"] == "stored-config-fingerprint"

    async def test_crash_event_omits_unavailable_config_fingerprint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        records: list[dict[str, object]] = []

        def _capture(_msg: str, **kw: object) -> None:
            records.append(kw)

        monkeypatch.setattr(dispatcher.logger, "exception", _capture)

        async def boom(_agent_id: int) -> None:
            raise ValueError("turn exploded")

        sched = TurnScheduler(boom)
        sched.wake(7)
        await _settle()

        report = next(r for r in records if r.get("event") == "host_turn_crashed")
        assert report["exception_type"] == "ValueError"
        assert "config_fingerprint" not in report

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


@contextlib.asynccontextmanager
async def _releasing_stuck_turn() -> AsyncGenerator[_StuckTurn, None]:
    """Release the cancellation-resistant fixture even when an assertion fails."""
    turn = _StuckTurn()
    try:
        yield turn
    finally:
        await turn.release()


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


class _ScanScheduler:
    """Small scheduler double for the dispatcher's recovery boundary."""

    def __init__(
        self,
        active: set[int] | None = None,
        *,
        unwinds_on_cancel: bool = True,
        restart_required: bool = False,
    ) -> None:
        self._active = active or set()
        self._unwinds_on_cancel = unwinds_on_cancel
        self._restart_required = restart_required
        self.woken: list[int] = []
        self.cancelled: list[int] = []
        self.woken_event = asyncio.Event()

    @property
    def active_agents(self) -> frozenset[int]:
        return frozenset(self._active)

    @property
    def restart_required(self) -> bool:
        return self._restart_required

    def wake(self, agent_id: int) -> None:
        self.woken.append(agent_id)
        self.woken_event.set()

    async def cancel_agent(self, agent_id: int) -> bool:
        self.cancelled.append(agent_id)
        if self._unwinds_on_cancel:
            self._active.discard(agent_id)
        return True


class TestPendingScan:
    async def test_scan_wakes_pending_inbound_even_when_pubsub_missed_it(self) -> None:
        """The database scan is the hosted counterpart of process mode's
        fallback SELECT: Redis notification loss may cost latency, never work."""
        scheduler = _ScanScheduler()

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return [dispatcher.PendingInboundWake(agent_id=23, stale=False)]

        disp = InboundWakeDispatcher(
            "redis://unused", scheduler, pending_scan=_pending, stale_after_s=180.0
        )

        await disp.scan_once()

        assert scheduler.woken == [23]
        assert scheduler.cancelled == []

    async def test_scan_cancels_a_stale_active_turn_before_rescheduling(self) -> None:
        """A pending row old enough to prove no progress must not remain behind
        a task that has silently stopped consuming scheduler wakes."""
        scheduler = _ScanScheduler({23})

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return [dispatcher.PendingInboundWake(agent_id=23, stale=True)]

        disp = InboundWakeDispatcher(
            "redis://unused", scheduler, pending_scan=_pending, stale_after_s=180.0
        )

        await disp.scan_once()

        assert scheduler.cancelled == [23]
        assert scheduler.woken == [23]

    async def test_scan_requires_a_host_restart_when_stale_turn_will_not_unwind(self) -> None:
        """A task that survives bounded cancellation retains its one-turn slot.

        Exiting is the only safe recovery: scheduling another task beside it
        would let one agent claim and mutate its checkpoint concurrently.
        """
        scheduler = _ScanScheduler({23}, unwinds_on_cancel=False)

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return [dispatcher.PendingInboundWake(agent_id=23, stale=True)]

        disp = InboundWakeDispatcher(
            "redis://unused", scheduler, pending_scan=_pending, stale_after_s=180.0
        )

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
            "redis://unused",
            scheduler,
            pending_scan=_pending,
            stale_after_s=180.0,
            scan_interval_s=0.005,
            subscription_read_timeout_s=0.005,
            reconnect_delay_s=0.0,
        )
        with pytest.raises(dispatcher.HostRestartRequiredError, match="supervisor recovery"):
            await asyncio.wait_for(disp.run(), timeout=2.0)


def _stale_age(_agent_id: int) -> float:
    """A turn-progress clock silent well past the scan budget (task #2417)."""
    return 3600.0


def _fresh_age(_agent_id: int) -> float:
    return 10.0


def _unknown_age(_agent_id: int) -> float | None:
    return None


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
        monkeypatch.setattr(dispatcher, "turn_progress_age_s", _stale_age)

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return []

        disp = InboundWakeDispatcher(
            "redis://unused", scheduler, pending_scan=_pending, stale_after_s=180.0
        )

        await disp.scan_once()

        assert scheduler.cancelled == [23]
        assert scheduler.woken == [23]

    async def test_a_fresh_in_flight_turn_is_left_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scheduler = _ScanScheduler({23})
        monkeypatch.setattr(dispatcher, "turn_progress_age_s", _fresh_age)

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return []

        disp = InboundWakeDispatcher(
            "redis://unused", scheduler, pending_scan=_pending, stale_after_s=180.0
        )

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
        monkeypatch.setattr(dispatcher, "turn_progress_age_s", _unknown_age)

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return []

        disp = InboundWakeDispatcher(
            "redis://unused", scheduler, pending_scan=_pending, stale_after_s=180.0
        )

        await disp.scan_once()

        assert scheduler.cancelled == []
        assert scheduler.woken == []

    async def test_stale_turn_that_will_not_unwind_requires_a_host_restart(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scheduler = _ScanScheduler({23}, unwinds_on_cancel=False)
        monkeypatch.setattr(dispatcher, "turn_progress_age_s", _stale_age)

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            return []

        disp = InboundWakeDispatcher(
            "redis://unused", scheduler, pending_scan=_pending, stale_after_s=180.0
        )

        with pytest.raises(dispatcher.HostRestartRequiredError, match="did not unwind"):
            await disp.scan_once()

        assert scheduler.cancelled == [23]
        assert scheduler.woken == []


class _QueueingPubSub:
    """Subscription fake that stays live while tests inject wake frames."""

    def __init__(self) -> None:
        self.messages: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self.closed = False

    async def psubscribe(self, _pattern: str) -> None:
        return None

    async def get_message(self, *, timeout: float) -> dict[str, object] | None:
        try:
            return await asyncio.wait_for(self.messages.get(), timeout=timeout)
        except TimeoutError:
            return None

    async def aclose(self) -> None:
        self.closed = True


class _QueueingRedis:
    def __init__(self, pubsub: _QueueingPubSub) -> None:
        self._pubsub = pubsub
        self.closed = False

    def pubsub(self, **_kwargs: object) -> _QueueingPubSub:
        return self._pubsub

    async def aclose(self) -> None:
        self.closed = True


def _patch_redis(monkeypatch: pytest.MonkeyPatch, pubsub: _QueueingPubSub) -> list[_QueueingRedis]:
    """Record every client open; a scan failure must never add one."""
    clients: list[_QueueingRedis] = []

    def _open(_url: str) -> _QueueingRedis:
        client = _QueueingRedis(pubsub)
        clients.append(client)
        return client

    from shared import redis_client

    monkeypatch.setattr(redis_client, "open_async_redis", _open)
    return clients


class TestSubscriptionRecovery:
    async def test_db_down_scan_keeps_the_redis_subscription_open(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The durable backstop may be down while the Redis wake path remains healthy."""
        pubsub = _QueueingPubSub()
        clients = _patch_redis(monkeypatch, pubsub)
        failures_seen = asyncio.Event()
        failure_events: list[dict[str, object]] = []
        scans = 0

        def _warning(_message: str, **fields: object) -> None:
            if fields["event"] == "host_dispatcher_scan_failed":
                failure_events.append(fields)

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            nonlocal scans
            scans += 1
            if scans >= 3:
                failures_seen.set()
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(dispatcher.logger, "warning", _warning)
        disp = InboundWakeDispatcher(
            "redis://unused",
            _ScanScheduler(),
            pending_scan=_pending,
            stale_after_s=180.0,
            scan_interval_s=0.02,
            max_scan_backoff_s=0.04,
            subscription_read_timeout_s=0.005,
            reconnect_delay_s=0.0,
        )
        task = asyncio.create_task(disp.run())
        try:
            await asyncio.wait_for(failures_seen.wait(), timeout=1.0)

            assert len(clients) == 1
            assert len(failure_events) >= 3
            assert [event["backoff_s"] for event in failure_events[:3]] == [0.02, 0.04, 0.04]
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def test_failing_scan_does_not_interrupt_the_pubsub_fast_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A DB outage must not create a gap in the subscription's wake delivery."""
        pubsub = _QueueingPubSub()
        clients = _patch_redis(monkeypatch, pubsub)
        first_failure = asyncio.Event()
        scheduler = _ScanScheduler()

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            first_failure.set()
            raise RuntimeError("database unavailable")

        disp = InboundWakeDispatcher(
            "redis://unused",
            scheduler,
            pending_scan=_pending,
            stale_after_s=180.0,
            scan_interval_s=0.02,
            subscription_read_timeout_s=0.005,
            reconnect_delay_s=0.0,
        )
        task = asyncio.create_task(disp.run())
        try:
            await asyncio.wait_for(first_failure.wait(), timeout=1.0)
            pubsub.messages.put_nowait({"type": "pmessage", "channel": "ava:inbound:23"})
            await asyncio.wait_for(scheduler.woken_event.wait(), timeout=1.0)

            assert scheduler.woken == [23]
            assert len(clients) == 1
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def test_scan_backoff_recovers_on_the_existing_subscription(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A recovered scan resets to its normal cadence without reconnecting Redis."""
        pubsub = _QueueingPubSub()
        clients = _patch_redis(monkeypatch, pubsub)
        recovered_twice = asyncio.Event()
        scans = 0
        successful_scans = 0
        successful_scan_times: list[float] = []

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            nonlocal scans, successful_scans
            scans += 1
            if scans <= 2:
                raise RuntimeError("database unavailable")
            successful_scans += 1
            successful_scan_times.append(time.monotonic())
            if successful_scans == 2:
                recovered_twice.set()
            return []

        disp = InboundWakeDispatcher(
            "redis://unused",
            _ScanScheduler(),
            pending_scan=_pending,
            stale_after_s=180.0,
            scan_interval_s=0.02,
            subscription_read_timeout_s=0.005,
            reconnect_delay_s=0.0,
        )
        task = asyncio.create_task(disp.run())
        try:
            await asyncio.wait_for(recovered_twice.wait(), timeout=1.0)

            assert scans >= 4
            assert successful_scan_times[1] - successful_scan_times[0] < 0.06
            assert len(clients) == 1
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def test_scan_restart_required_error_exits_without_reconnecting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stale turn that cannot unwind remains a host-level recovery condition."""
        pubsub = _QueueingPubSub()
        clients = _patch_redis(monkeypatch, pubsub)

        async def _pending(_stale_after_s: float) -> list[dispatcher.PendingInboundWake]:
            raise dispatcher.HostRestartRequiredError("stale turn did not unwind")

        disp = InboundWakeDispatcher(
            "redis://unused",
            _ScanScheduler(),
            pending_scan=_pending,
            stale_after_s=180.0,
        )

        with pytest.raises(dispatcher.HostRestartRequiredError, match="did not unwind"):
            await disp.run()

        assert len(clients) == 1

    async def test_half_open_subscription_read_is_bounded_and_reconnected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A peer may stay TCP-connected yet never answer a subscription read.

        The dispatcher must close that connection and establish a fresh one,
        rather than letting the hosted pending scan and all notifications stop
        behind a hung `PSUBSCRIBE` read.
        """

        class _HalfOpenPubSub:
            def __init__(self) -> None:
                self.closed = False

            async def psubscribe(self, _pattern: str) -> None:
                return None

            async def get_message(self, **_kwargs: object) -> None:
                await asyncio.Event().wait()

            async def aclose(self) -> None:
                self.closed = True

        class _Redis:
            def __init__(self, pubsub: _HalfOpenPubSub) -> None:
                self._pubsub = pubsub
                self.closed = False

            def pubsub(self, **_kwargs: object) -> _HalfOpenPubSub:
                return self._pubsub

            async def aclose(self) -> None:
                self.closed = True

        first_pubsub = _HalfOpenPubSub()
        second_pubsub = _HalfOpenPubSub()
        first = _Redis(first_pubsub)
        second = _Redis(second_pubsub)
        clients = iter([first, second])
        second_opened = asyncio.Event()

        def _open(_url: str) -> _Redis:
            client = next(clients)
            if client is second:
                second_opened.set()
            return client

        from shared import redis_client

        monkeypatch.setattr(redis_client, "open_async_redis", _open)
        disp = InboundWakeDispatcher(
            "redis://unused",
            _ScanScheduler(),
            subscription_read_timeout_s=0.01,
            subscription_read_deadline_grace_s=0.01,
            reconnect_delay_s=0.0,
        )
        task = asyncio.create_task(disp.run())
        try:
            await asyncio.wait_for(second_opened.wait(), timeout=1.0)
            assert first_pubsub.closed, "the half-open pubsub handle must be discarded"
            assert first.closed, "the paired Redis command client must be discarded"
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
