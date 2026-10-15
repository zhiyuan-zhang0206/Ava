from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from base.clock import Clock


def fix_now_timestamp(monkeypatch: pytest.MonkeyPatch, stamp: str) -> None:

    def now_timestamp(_clock: Clock) -> str:
        return stamp

    monkeypatch.setattr(Clock, "now_timestamp", now_timestamp)


def fix_zone(monkeypatch: pytest.MonkeyPatch, zone: ZoneInfo | None) -> None:

    def fixed(_clock: Clock) -> ZoneInfo | None:
        return zone

    monkeypatch.setattr(Clock, "zone", fixed)
