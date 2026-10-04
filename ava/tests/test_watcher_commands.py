"""ava.watcher command parsing, validation, and timezone handling; split from ava/tests/test_watcher.py (task #4922)."""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from ava import watcher
from base.native_process.os_platform import IS_WINDOWS

pytestmark = [
    pytest.mark.skipif(IS_WINDOWS, reason="PTY supervisor is POSIX-only"),
    # `_isolated_agent` is opt-in (mutates global ava.self.AGENT_ID); apply it
    # module-wide here since every watcher session test needs the fake-id +
    # pty cleanup isolation. `pty_service` first — the isolation fixture's
    # own kill_all/list calls hit the daemon.
    pytest.mark.usefixtures("pty_service", "_isolated_agent"),
]


def test_validate_message_rejects_empty() -> None:
    with pytest.raises(ValueError, match="message cannot be empty"):
        watcher.at("2030-01-01T00:00:00Z", "   ", name="test-empty")


@pytest.mark.parametrize(
    "value, expected",
    [
        (90, 90.0),
        (datetime.timedelta(minutes=2), 120.0),
        ("30m", 1800.0),
        ("2h", 7200.0),
        ("1d", 86400.0),
        ("45s", 45.0),
    ],
)
def test_parse_timeout_accepts_forms(
    value: int | datetime.timedelta | str, expected: float
) -> None:
    assert watcher.parse_timeout(value) == expected


@pytest.mark.parametrize("bad", ["", "5x", "later", "-3"])
def test_parse_timeout_rejects_bad_strings(bad: str) -> None:
    with pytest.raises(ValueError):
        watcher.parse_timeout(bad)


def test_parse_timeout_rejects_nonpositive_and_bool() -> None:
    with pytest.raises(ValueError, match="positive"):
        watcher.parse_timeout(0)
    with pytest.raises(TypeError):
        watcher.parse_timeout(True)


def test_at_announcement_uses_cluster_zone_when_authoritative(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """at() passes the cluster timezone to the generated script when the
    process holds an authoritative one (user ruling 2026-08-27)."""
    from base.config import settings
    from base.config.domains.general import GeneralSettings

    monkeypatch.setattr(
        settings, "general", GeneralSettings.model_construct(timezone="Asia/Shanghai")
    )
    captured: dict[str, Any] = {}

    def fake_spawn(code: str, watchdog_secs: float | None, name: str, **kw: object) -> int:
        captured["code"] = code
        return 7

    monkeypatch.setattr(watcher, "_spawn", fake_spawn)
    when = (datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=365)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    watcher.at(when, "ping later", name="test-at-cluster-tz")
    assert "_TZ = ZoneInfo('Asia/Shanghai')" in captured["code"]
    assert "_WHEN.astimezone(_TZ).isoformat()" in captured["code"]


def test_at_announcement_uses_host_clock_without_authoritative_zone(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A settings-lite process (no authoritative cluster timezone) passes
    None: the announcement renders in the watcher's own wall clock — the
    documented lite degradation."""
    from base.config import settings
    from base.config.domains.general import GeneralSettings

    monkeypatch.setattr(settings, "general", GeneralSettings.model_construct())
    captured: dict[str, Any] = {}

    def fake_spawn(code: str, watchdog_secs: float | None, name: str, **kw: object) -> int:
        captured["code"] = code
        return 7

    monkeypatch.setattr(watcher, "_spawn", fake_spawn)
    when = (datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=365)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    watcher.at(when, "ping later", name="test-at-lite")
    assert "ZoneInfo" not in captured["code"]
    assert "_WHEN.astimezone().isoformat()" in captured["code"]


def test_cron_defaults_to_host_zone_without_authoritative_zone(
    _agent_row: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """settings-lite cron (no authoritative cluster timezone) defaults to the
    host's own zone — not the silent America/Los_Angeles field default."""
    from base.config import host_tz_name, settings
    from base.config.domains.general import GeneralSettings

    monkeypatch.setattr(settings, "general", GeneralSettings.model_construct())
    captured: dict[str, Any] = {}

    def fake_spawn(code: str, watchdog_secs: float | None, name: str, **kw: object) -> int:
        captured["code"] = code
        return 7

    monkeypatch.setattr(watcher, "_spawn", fake_spawn)
    watcher.cron("0 * * * *", "tick", name="test-cron-lite")
    expected = host_tz_name()
    assert f"_TZ = ZoneInfo('{expected}')" in captured["code"]
    assert "America/Los_Angeles" not in captured["code"]


def test_cron_invalid_expr_raises(_agent_row: int) -> None:
    from base.daemon.schedules.watcher import CronExprError

    with pytest.raises(CronExprError):
        watcher.cron("not a cron", "msg", name="test-bad-cron")


def test_cron_invalid_timezone_raises(_agent_row: int) -> None:
    with pytest.raises(ValueError, match="timezone"):
        watcher.cron("0 3 * * *", "daily", timezone="Not/A/Real/Timezone", name="test-bad-tz")


def test_at_past_time_raises(_agent_row: int) -> None:
    """at() with a past datetime raises ValueError."""
    from datetime import UTC, datetime

    past = datetime(2020, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="past"):
        watcher.at(past, "too late", name="test-past")


def test_cron_past_end_time_raises(_agent_row: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """cron() with an explicit past `end_time` raises ValueError, aligned
    with at() (issue #2078: a past end otherwise supersedes the live twin
    and registers a watcher that self-terminates immediately). The raise
    fires BEFORE any registration: _spawn must never run for a past end."""
    from datetime import UTC, datetime

    def fail_if_spawned(*_args: object, **_kw: object) -> int:
        raise AssertionError("_spawn must not run for a past end_time")

    monkeypatch.setattr(watcher, "_spawn", fail_if_spawned)
    past = datetime(2020, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="end_time is in the past"):
        watcher.cron("0 3 * * *", "daily", timezone="UTC", end_time=past, name="test-cron-past-end")


def test_cron_future_end_time_ok(_agent_row: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit future `end_time` registers normally (no past rejection)."""
    from datetime import UTC, datetime, timedelta

    captured: dict[str, Any] = {}

    def fake_spawn(code: str, watchdog_secs: float | None, name: str, **kw: object) -> int:
        captured.update(code=code, cron_end_at=kw.get("cron_end_at"))
        return 7

    monkeypatch.setattr(watcher, "_spawn", fake_spawn)
    end = datetime.now(UTC) + timedelta(days=2)
    wid = watcher.cron(
        "0 3 * * *", "daily", timezone="UTC", end_time=end, name="test-cron-future-end"
    )
    assert wid == 7
    assert captured["cron_end_at"] == end
    assert "daily" in captured["code"]

    # timedelta going backwards should also fail
    with pytest.raises(ValueError):
        watcher.at(timedelta(days=-1), "negative delta", name="test-neg-delta")


def test_at_future_time_ok(_agent_row: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """at() with a future time should not raise about the past."""
    from datetime import timedelta

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        watcher,
        "_spawn",
        lambda code, _wd, name, **_kw: captured.update(code=code, name=name) or 7,  # pyright: ignore[reportUnknownArgumentType]
    )

    # Far future
    watcher.at("2099-01-01T00:00:00Z", "far future", name="test-future")
    assert "far future" in captured["code"]

    # timedelta from now
    watcher.at(timedelta(hours=1), "one hour", name="test-delta")
    assert "one hour" in captured["code"]
