from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from base.deploy.maintenance import pause_owner
from base.deploy.maintenance.state import MaintenanceHold, MaintenancePhase
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from services.supervision.healthchecks.maintenance_hold import MaintenanceHoldWatch

ACQUIRED = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
THRESHOLD_S = 1800.0
EVENT = "maintenance_hold_overdue"


class _Recorder:
    def __init__(self, records: LogRecords) -> None:
        self._records = records

    def events(self) -> list[dict[str, object]]:
        return [
            {"message": r["message"], **r["extra"]}
            for r in self._records
            if r["extra"].get("event") == EVENT
        ]


@pytest.fixture
def recorder(loguru_records: LogRecords) -> _Recorder:
    return _Recorder(loguru_records)


@pytest.fixture(autouse=True)
def journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(pause_owner, "state_path", lambda: tmp_path / "pause.json")
    monkeypatch.setattr(pause_owner, "lock_path", lambda: tmp_path / "pause.lock")
    return tmp_path / "pause.json"


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now.timestamp()


def _watch(now: datetime) -> tuple[MaintenanceHoldWatch, Callable[[datetime], None]]:
    clock = _Clock(now)

    def move(moved: datetime) -> None:
        clock.now = moved

    return MaintenanceHoldWatch(alert_after_s=THRESHOLD_S, clock=clock), move


def _round(watch: MaintenanceHoldWatch) -> bool:
    result = watch.probe()
    watch.report(result)
    return result.alive


def _hold(holder: str = "ava-stop-7", phase: MaintenancePhase = MaintenancePhase.STOPPED) -> None:
    admission = pause_owner.begin_maintenance(holder, ACQUIRED)
    assert admission.snapshot.maintenance is not None
    pause_owner.change_maintenance(
        holder, ACQUIRED, admission.snapshot.maintenance, MaintenanceHold(phase)
    )


def test_a_hold_past_the_threshold_raises_one_event_naming_the_recovery(
    recorder: _Recorder,
) -> None:
    _hold()
    watch, _ = _watch(ACQUIRED + timedelta(minutes=31))
    assert not _round(watch)
    (event,) = recorder.events()
    assert event["holder"] == "ava-stop-7"
    assert event["phase"] == "stopped"
    assert event["state"] == "paused"
    assert event["acquired_at"] == ACQUIRED.isoformat()
    assert event["held_seconds"] == 31 * 60
    assert "ava start" in str(event["message"])


def test_a_hold_under_the_threshold_raises_nothing(recorder: _Recorder) -> None:
    _hold()
    watch, move = _watch(ACQUIRED + timedelta(minutes=29))
    assert _round(watch)
    move(ACQUIRED + timedelta(seconds=THRESHOLD_S - 1))
    assert _round(watch)
    assert recorder.events() == []


def test_the_threshold_is_the_configured_age_not_a_constant(recorder: _Recorder) -> None:
    _hold()
    watch = MaintenanceHoldWatch(alert_after_s=60.0, clock=_Clock(ACQUIRED + timedelta(minutes=2)))
    assert not _round(watch)
    assert len(recorder.events()) == 1


def test_no_hold_or_a_released_hold_raises_nothing(recorder: _Recorder, journal: Path) -> None:
    watch, _ = _watch(ACQUIRED + timedelta(days=3))
    assert _round(watch)
    _hold()
    snapshot = pause_owner.read()
    assert snapshot.maintenance is not None
    pause_owner.change_maintenance(
        "ava-stop-7", ACQUIRED, snapshot.maintenance, snapshot.maintenance, resumed=True
    )
    assert pause_owner.read().status == "resumed"
    assert _round(watch)
    assert recorder.events() == []


def test_one_hold_raises_one_event_however_many_rounds_observe_it(recorder: _Recorder) -> None:
    _hold()
    watch, move = _watch(ACQUIRED + timedelta(minutes=31))
    for minutes in range(31, 40):
        move(ACQUIRED + timedelta(minutes=minutes))
        assert not _round(watch)
    assert len(recorder.events()) == 1
    assert recorder.events()[0]["held_seconds"] == 31 * 60


def test_a_released_hold_rearms_the_alert_for_the_next_hold(
    recorder: _Recorder, journal: Path
) -> None:
    _hold()
    watch, move = _watch(ACQUIRED + timedelta(minutes=31))
    assert not _round(watch)
    journal.unlink()
    assert _round(watch)
    later = ACQUIRED + timedelta(hours=2)
    admission = pause_owner.begin_maintenance("ava-restart-9", later)
    assert admission.created_here
    move(later + timedelta(minutes=45))
    assert not _round(watch)
    holders = [event["holder"] for event in recorder.events()]
    assert holders == ["ava-stop-7", "ava-restart-9"]


def test_an_unreadable_journal_is_timed_from_its_mtime_and_never_raises(
    recorder: _Recorder, journal: Path
) -> None:
    journal.write_text("{not json")
    written = ACQUIRED.timestamp()
    os.utime(journal, (written, written))
    watch, move = _watch(ACQUIRED + timedelta(minutes=5))
    assert _round(watch)
    move(ACQUIRED + timedelta(minutes=31))
    assert not _round(watch)
    assert not _round(watch)
    (event,) = recorder.events()
    assert event["state"] == "invalid"
    assert event["holder"] is None and event["phase"] is None and event["acquired_at"] is None
    assert event["held_seconds"] == 31 * 60
    assert "deploy-pause-owner.json" in str(event["message"])
