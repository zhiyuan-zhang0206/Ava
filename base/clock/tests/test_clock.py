"""`Clock`: the handle binds one config; the format and the host-zone fallback are the contract."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from base.clock import Clock, ClockConfig


def _clock(
    timezone: str = "Asia/Shanghai",
    authoritative: str | None = "Asia/Shanghai",
    weekday: bool = False,
) -> Clock:
    return Clock(ClockConfig(timezone, authoritative, weekday))


def test_the_zone_is_the_authoritative_name_and_none_without_one() -> None:
    assert _clock().zone() == ZoneInfo("Asia/Shanghai")
    assert _clock(authoritative=None).zone() is None


def test_an_unparseable_authoritative_name_degrades_to_the_host_zone_signal() -> None:
    assert _clock(authoritative="Not/AZone").zone() is None


def test_the_explicit_zone_follows_the_field_even_when_it_is_the_default() -> None:
    assert _clock(authoritative=None).explicit_zone() == ZoneInfo("Asia/Shanghai")


def test_format_converts_to_the_cluster_zone_with_and_without_the_weekday() -> None:
    moment = datetime(2026, 5, 6, 6, 32, 5, tzinfo=UTC)
    assert _clock().format_timestamp(moment) == "[2026-05-06 14:32:05]"
    assert _clock(weekday=True).format_timestamp(moment) == "[2026-05-06 Wed 14:32:05]"


def test_now_is_the_injected_source() -> None:
    moment = datetime(2026, 5, 6, 6, 32, 5, tzinfo=UTC)
    clock = Clock(ClockConfig("Asia/Shanghai", None, False), now=lambda: moment)
    assert clock.now() == moment
    assert clock.now_timestamp() == "[2026-05-06 14:32:05]"


def test_from_settings_reads_the_live_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from base.config import settings

    monkeypatch.setattr(settings.general, "timezone", "Europe/Paris")
    monkeypatch.setattr(settings.general, "message_timestamp_weekday", True)
    clock = Clock.from_settings()
    assert clock.timezone == "Europe/Paris"
    assert clock.explicit_zone() == ZoneInfo("Europe/Paris")


def test_boot_clock_reads_keep_two_owners_and_previous_handles_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    import time

    from base.clock import clock_config_from_boot
    from base.config import ConfigBoot

    # ConfigBoot applies TZ when a timezone overlay changes; restore process state.
    with monkeypatch.context() as env:
        env.setattr(os, "environ", os.environ.copy())
        first, second = ConfigBoot(), ConfigBoot()
        first.boot()
        second.boot()
        first.set_field("timezone", "UTC")
        first.set_field("message_timestamp_weekday", False)
        second.set_field("timezone", "Asia/Shanghai")
        second.set_field("message_timestamp_weekday", True)
        old = Clock(clock_config_from_boot(first))
        other = Clock(clock_config_from_boot(second))
        moment = datetime(2026, 5, 6, 6, 32, 5, tzinfo=UTC)
        assert old.format_timestamp(moment) == "[2026-05-06 06:32:05]"
        assert other.format_timestamp(moment) == "[2026-05-06 Wed 14:32:05]"
        first.set_field("timezone", "Europe/Paris")
        current = Clock(clock_config_from_boot(first))
        assert current.authoritative_timezone == "Europe/Paris"
        assert current.format_timestamp(moment) == "[2026-05-06 08:32:05]"
        assert old.timezone == "UTC"
        assert other.timezone == "Asia/Shanghai"
    if hasattr(time, "tzset"):
        time.tzset()
