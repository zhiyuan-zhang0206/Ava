"""Durable cadence rules for automated recovery drills."""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo

import pytest

from base.clock import Clock, ClockConfig
from services.backup.scheduler import recovery_drill


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=UTC)


def test_local_dump_restore_runs_once_after_the_weekly_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def utc_zone(**_inputs: object) -> tzinfo:
        return UTC

    monkeypatch.setattr(recovery_drill, "_cluster_tz", utc_zone)
    before_window = _at(30, 2, 59)
    window = _at(30, 3)

    prior_success = datetime(2026, 8, 23, 3, tzinfo=UTC)
    assert not recovery_drill.local_dump_restore_due(
        before_window,
        last_success=prior_success,
        clock_factory=lambda: Clock(ClockConfig("UTC", "UTC", False)),
        hour_reader=lambda: 3,
    )
    assert recovery_drill.local_dump_restore_due(
        window,
        last_success=prior_success,
        clock_factory=lambda: Clock(ClockConfig("UTC", "UTC", False)),
        hour_reader=lambda: 3,
    )
    assert not recovery_drill.local_dump_restore_due(
        window,
        last_success=window,
        clock_factory=lambda: Clock(ClockConfig("UTC", "UTC", False)),
        hour_reader=lambda: 3,
    )
    assert recovery_drill.local_dump_restore_due(
        datetime(2026, 9, 6, 3, tzinfo=UTC),
        last_success=window,
        clock_factory=lambda: Clock(ClockConfig("UTC", "UTC", False)),
        hour_reader=lambda: 3,
    )
