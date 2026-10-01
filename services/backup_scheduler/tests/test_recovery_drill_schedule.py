"""Durable cadence rules for automated recovery drills."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from services.backup_scheduler import recovery_drill


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=UTC)


def test_local_dump_restore_runs_once_after_the_weekly_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(recovery_drill, "_cluster_tz", lambda: UTC)
    before_window = _at(30, 2, 59)
    window = _at(30, 3)

    prior_success = datetime(2026, 8, 23, 3, tzinfo=UTC)
    assert not recovery_drill.local_dump_restore_due(before_window, last_success=prior_success)
    assert recovery_drill.local_dump_restore_due(window, last_success=prior_success)
    assert not recovery_drill.local_dump_restore_due(window, last_success=window)
    assert recovery_drill.local_dump_restore_due(
        datetime(2026, 9, 6, 3, tzinfo=UTC), last_success=window
    )
