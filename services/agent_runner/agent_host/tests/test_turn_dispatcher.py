"""Hosted-runner wake dispatch — `services/agent_runner/agent_host/dispatcher.py`.

Phase 1 work item (a) of `future/infra/lifecycle/agent-runner-as-server.md`. The contracts
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
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from services.agent_runner.agent_host import dispatcher
from services.agent_runner.agent_host.dispatcher import (
    TurnScheduler,
)
from services.agent_runner.agent_host.tests.turn_dispatcher.helpers import (
    Recorder,
    StuckTurn,
    aclose_without_hanging,
    settle,
)


def _logged_fields(records: LogRecords, level: str) -> list[dict[str, Any]]:
    """The structured fields of every record logged at `level` so far."""
    return [r["extra"] for r in records if r["level"].name == level]


class TestBasicDispatch:
    async def test_wake_starts_a_turn_and_the_task_ends_when_idle(self) -> None:
        rec = Recorder()
        sched = TurnScheduler(rec)

        assert sched.active_agents == frozenset(), "an idle agent must have no task"
        sched.wake(7)
        await settle()

        assert rec.started == [7]
        # The turn returned (nothing gated it), so the agent is idle again —
        # idle costs nothing because there is nothing.
        assert sched.active_agents == frozenset()

    async def test_second_wake_after_idle_starts_a_fresh_turn(self) -> None:
        rec = Recorder()
        sched = TurnScheduler(rec)

        sched.wake(7)
        await settle()
        sched.wake(7)
        await settle()

        assert rec.started == [7, 7]

    async def test_a_wake_materializes_the_task_without_any_await(self) -> None:
        """The wake -> turn-task path is pure in-memory: `wake()` creates the
        task synchronously, with no await between the wake and task creation.
        That is the ms-level wake latency the hosted design calls for, asserted
        structurally (a wall-clock bound would be CI flake bait)."""
        rec = Recorder()
        sched = TurnScheduler(rec)

        assert sched.active_agents == frozenset()
        sched.wake(7)
        assert 7 in sched.active_agents, "the turn task must exist before any await"
        await settle()


class TestSerializationAndConcurrency:
    async def test_one_agents_turns_never_overlap(self) -> None:
        """The same agent's turns must serialize — they share a checkpointer
        thread. A wake during a running turn may not start a second task."""
        rec = Recorder()
        rec.gate(7)  # turn 7 blocks until released
        sched = TurnScheduler(rec)

        sched.wake(7)
        await settle()
        assert rec.started == [7]

        sched.wake(7)  # arrives mid-turn
        await settle()
        assert rec.started == [7], "a second concurrent turn was started for one agent"
        assert len(sched.active_agents) == 1

        rec.gate(7).set()
        await settle()
        assert rec.started == [7, 7], "the mid-turn wake did not produce a follow-up turn"

    async def test_different_agents_run_concurrently(self) -> None:
        """Cross-agent concurrency is the point of the model — one blocked agent
        must not hold up another."""
        rec = Recorder()
        rec.gate(1)
        rec.gate(2)
        sched = TurnScheduler(rec)

        sched.wake(1)
        sched.wake(2)
        await settle()

        assert sorted(rec.started) == [1, 2]
        assert sched.active_agents == frozenset({1, 2})

        rec.gate(1).set()
        rec.gate(2).set()
        await settle()
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
        rec = Recorder()
        rec.gate(7)
        sched = TurnScheduler(rec)

        sched.wake(7)
        await settle()
        assert rec.started == [7]

        # The turn is about to finish AND a wake arrives — the exact overlap.
        rec.gate(7).set()
        sched.wake(7)
        await settle()

        assert rec.started == [7, 7], "the wake was swallowed by the winding-down turn"

    async def test_wake_after_the_task_is_gone_starts_a_new_one(self) -> None:
        """The other side of the same window: once the task has been removed, a
        wake must start a fresh task rather than be recorded against nothing."""
        rec = Recorder()
        sched = TurnScheduler(rec)

        sched.wake(7)
        await settle()
        assert sched.active_agents == frozenset()

        sched.wake(7)
        await settle()
        assert rec.started == [7, 7]

    async def test_two_wakes_during_one_turn_coalesce(self) -> None:
        """Two wakes are one reason to run again: the follow-up turn drains
        whatever is pending, so a chat burst must not queue a turn burst."""
        rec = Recorder()
        rec.gate(7)
        sched = TurnScheduler(rec)

        sched.wake(7)
        await settle()
        sched.wake(7)
        sched.wake(7)
        sched.wake(7)
        rec.gate(7).set()
        await settle()

        assert rec.started == [7, 7], f"wakes did not coalesce: {rec.started}"


class TestFailureIsolation:
    async def test_crash_event_carries_exception_type_and_config_fingerprint(
        self, loguru_records: LogRecords
    ) -> None:

        async def boom(_agent_id: int) -> None:
            raise ValueError("turn exploded")

        sched = TurnScheduler(
            boom, config_fingerprint=lambda _agent_id: "stored-config-fingerprint"
        )
        sched.wake(7)
        await settle()

        [record] = loguru_records.events("host_turn_crashed")
        assert record["level"].name == "ERROR"
        assert record["exception"] is not None
        assert record["exception"].type is ValueError
        assert record["exception"].traceback is not None
        report = record["extra"]
        assert report["exception_type"] == "ValueError"
        assert report["config_fingerprint"] == "stored-config-fingerprint"

    async def test_crash_event_omits_unavailable_config_fingerprint(
        self, loguru_records: LogRecords
    ) -> None:

        async def boom(_agent_id: int) -> None:
            raise ValueError("turn exploded")

        sched = TurnScheduler(boom)
        sched.wake(7)
        await settle()

        [record] = loguru_records.events("host_turn_crashed")
        assert record["level"].name == "ERROR"
        assert record["exception"] is not None
        assert record["exception"].type is ValueError
        assert record["exception"].traceback is not None
        report = record["extra"]
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
        await settle()

        assert calls == [7]
        assert sched.active_agents == frozenset(), "a crashed turn left its agent wedged"

        sched.wake(7)  # the agent is still serviceable
        await settle()
        assert calls == [7, 7]

    async def test_aclose_cancels_running_turns(self) -> None:
        rec = Recorder()
        rec.gate(7)
        sched = TurnScheduler(rec)
        sched.wake(7)
        await settle()
        assert sched.active_agents == frozenset({7})

        await sched.aclose()

        assert sched.active_agents == frozenset()
        # Closed: further wakes are ignored rather than resurrecting a task
        # during shutdown.
        sched.wake(7)
        await settle()
        assert sched.active_agents == frozenset()


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
        stuck = StuckTurn()
        sched = TurnScheduler(stuck)
        sched.wake(7)
        await asyncio.wait_for(stuck.entered.wait(), 2)

        returned = await aclose_without_hanging(sched, stuck)

        assert returned, (
            "aclose never returned — a turn that refuses to unwind is hanging shutdown, "
            "which is the failure the bounded wait exists to prevent"
        )
        assert sched.active_agents == frozenset(), "aclose must not leave the registry populated"

    async def test_the_straggler_is_named_in_the_event_river(
        self, monkeypatch: pytest.MonkeyPatch, loguru_records: LogRecords
    ) -> None:
        """ "Is anything wedged right now" must be answerable without reading
        logs, so the report carries the agent id and how long the cancel was
        pending as event fields."""
        monkeypatch.setattr(dispatcher, "CANCEL_UNWIND_TIMEOUT_S", 0.05)
        turn = StuckTurn()
        sched = TurnScheduler(turn)
        sched.wake(31)
        await asyncio.wait_for(turn.entered.wait(), 2)

        assert await aclose_without_hanging(sched, turn), "aclose never returned"

        reports = [
            r
            for r in _logged_fields(loguru_records, "ERROR")
            if r.get("event") == "host_turn_uncancellable"
        ]
        assert len(reports) == 1, "exactly one report per stuck agent"
        assert reports[0]["agent_id"] == 31
        assert isinstance(reports[0]["waited_s"], float)

    async def test_a_turn_that_unwinds_cleanly_reports_nothing(
        self, monkeypatch: pytest.MonkeyPatch, loguru_records: LogRecords
    ) -> None:
        """The ordinary case must stay silent, or the report is noise nobody
        reads by the time it matters."""
        monkeypatch.setattr(dispatcher, "CANCEL_UNWIND_TIMEOUT_S", 0.05)

        rec = Recorder()
        rec.gate(7)
        sched = TurnScheduler(rec)
        sched.wake(7)
        await settle()
        await asyncio.wait_for(sched.aclose(), 5)

        assert [
            r
            for r in _logged_fields(loguru_records, "ERROR")
            if r.get("event") == "host_turn_uncancellable"
        ] == []

    async def test_the_report_carries_the_real_activity_clock(
        self, monkeypatch: pytest.MonkeyPatch, loguru_records: LogRecords
    ) -> None:
        """A wedge report has to answer "how long has this agent actually been
        silent", or a slow shutdown and a genuine wedge look identical.

        The clock is `agents_meta.last_active_at` — written on every completed
        LLM step — NOT the `/api/agents` field of the same name, which is
        `MAX(inbound_messages.created_at)` and goes stale during exactly the long
        turns where the question is real (issue #183).
        """
        monkeypatch.setattr(dispatcher, "CANCEL_UNWIND_TIMEOUT_S", 0.05)
        silent_since = datetime.now(UTC) - timedelta(minutes=20)

        async def _clock(_agent_id: int) -> datetime:
            return silent_since

        turn = StuckTurn()
        sched = TurnScheduler(turn, activity_clock=_clock)
        sched.wake(5)
        await asyncio.wait_for(turn.entered.wait(), 2)

        assert await aclose_without_hanging(sched, turn), "aclose never returned"

        report = next(
            r
            for r in _logged_fields(loguru_records, "ERROR")
            if r.get("event") == "host_turn_uncancellable"
        )
        assert report["last_active_at"] == silent_since.isoformat()
        idle_s = report["idle_s"]
        assert isinstance(idle_s, float)
        assert idle_s > 1000, "a 20-minute-silent agent must not read as freshly active"

    async def test_a_failing_clock_read_still_reports_the_wedge(
        self, monkeypatch: pytest.MonkeyPatch, loguru_records: LogRecords
    ) -> None:
        """The clock is an enrichment; the agent id and pending duration are the
        part that cannot be reconstructed later. A DB that is unreachable — quite
        possibly what the turn is stuck on — must not cost the whole report."""
        monkeypatch.setattr(dispatcher, "CANCEL_UNWIND_TIMEOUT_S", 0.05)

        async def _broken_clock(_agent_id: int) -> datetime:
            raise RuntimeError("db unreachable")

        turn = StuckTurn()
        sched = TurnScheduler(turn, activity_clock=_broken_clock)
        sched.wake(9)
        await asyncio.wait_for(turn.entered.wait(), 2)

        assert await aclose_without_hanging(sched, turn), "aclose never returned"

        report = next(
            r
            for r in _logged_fields(loguru_records, "ERROR")
            if r.get("event") == "host_turn_uncancellable"
        )
        assert report["agent_id"] == 9
        assert isinstance(report["waited_s"], float)
        assert report["last_active_at"] is None, "an unreadable clock reports as absent"
        assert report["idle_s"] is None

    async def test_a_hanging_clock_read_does_not_delay_shutdown(
        self, monkeypatch: pytest.MonkeyPatch, loguru_records: LogRecords
    ) -> None:
        """Same reasoning one step further: a clock read that never returns is
        the shutdown-hang this whole class exists to prevent, reintroduced
        through the diagnostic. It is bounded too."""
        monkeypatch.setattr(dispatcher, "CANCEL_UNWIND_TIMEOUT_S", 0.05)
        monkeypatch.setattr(dispatcher, "CLOCK_READ_TIMEOUT_S", 0.05)

        async def _hanging_clock(_agent_id: int) -> datetime:
            await asyncio.sleep(3600)
            raise AssertionError("unreachable")

        turn = StuckTurn()
        sched = TurnScheduler(turn, activity_clock=_hanging_clock)
        sched.wake(12)
        await asyncio.wait_for(turn.entered.wait(), 2)

        assert await aclose_without_hanging(sched, turn), "a hanging clock read blocked shutdown"

        report = next(
            r
            for r in _logged_fields(loguru_records, "ERROR")
            if r.get("event") == "host_turn_uncancellable"
        )
        assert report["agent_id"] == 12
        assert report["last_active_at"] is None
