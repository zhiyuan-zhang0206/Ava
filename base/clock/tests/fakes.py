"""Stand-ins for the cluster `Clock`, for tests of code that builds its own with
`Clock.from_settings()`."""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from base.clock import Clock


def fix_now_timestamp(monkeypatch: pytest.MonkeyPatch, stamp: str) -> None:
    """Make every `Clock.now_timestamp()` return `stamp`."""

    def now_timestamp(_clock: Clock) -> str:
        return stamp

    monkeypatch.setattr(Clock, "now_timestamp", now_timestamp)


def fix_zone(monkeypatch: pytest.MonkeyPatch, zone: ZoneInfo | None) -> None:
    """Make every `Clock.zone()` return `zone` (None = the host-zone fallback signal)."""

    def fixed(_clock: Clock) -> ZoneInfo | None:
        return zone

    monkeypatch.setattr(Clock, "zone", fixed)
