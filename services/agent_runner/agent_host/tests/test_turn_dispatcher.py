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
    return [r["extra"] for r in records if r["level"].name == level]


class TestBasicDispatch:
    async def test_wake_starts_a_turn_and_the_task_ends_when_idle(self) -> None:
        rec = Recorder()
        sched = TurnScheduler(rec)

        assert sched.active_agents == frozenset(), "an idle agent must have no task"
        sched.wake(7)
        await settle()

        assert rec.started == [7]
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
        rec = Recorder()
        sched = TurnScheduler(rec)

        assert sched.active_agents == frozenset()
        sched.wake(7)
        assert 7 in sched.active_agents, "the turn task must exist before any await"
        await settle()


class TestSerializationAndConcurrency:
    async def test_one_agents_turns_never_overlap(self) -> None:
        rec = Recorder()
        rec.gate(7)
        sched = TurnScheduler(rec)

        sched.wake(7)
        await settle()
        assert rec.started == [7]

        sched.wake(7)
        await settle()
        assert rec.started == [7], "a second concurrent turn was started for one agent"
        assert len(sched.active_agents) == 1

        rec.gate(7).set()
        await settle()
        assert rec.started == [7, 7], "the mid-turn wake did not produce a follow-up turn"

    async def test_different_agents_run_concurrently(self) -> None:
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
        rec = Recorder()
        rec.gate(7)
        sched = TurnScheduler(rec)

        sched.wake(7)
        await settle()
        assert rec.started == [7]

        rec.gate(7).set()
        sched.wake(7)
        await settle()

        assert rec.started == [7, 7], "the wake was swallowed by the winding-down turn"

    async def test_wake_after_the_task_is_gone_starts_a_new_one(self) -> None:
        rec = Recorder()
        sched = TurnScheduler(rec)

        sched.wake(7)
        await settle()
        assert sched.active_agents == frozenset()

        sched.wake(7)
        await settle()
        assert rec.started == [7, 7]

    async def test_two_wakes_during_one_turn_coalesce(self) -> None:
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
        calls: list[int] = []

        async def boom(agent_id: int) -> None:
            calls.append(agent_id)
            raise RuntimeError("turn exploded")

        sched = TurnScheduler(boom)
        sched.wake(7)
        await settle()

        assert calls == [7]
        assert sched.active_agents == frozenset(), "a crashed turn left its agent wedged"

        sched.wake(7)
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
        sched.wake(7)
        await settle()
        assert sched.active_agents == frozenset()


class TestUncancellableTurn:
    async def test_aclose_returns_even_when_a_turn_refuses_to_unwind(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
