"""ava.watcher command parsing, validation, and timezone handling; split from ava/tests/namespaces/watcher/test_watcher.py (task #4922)."""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from ava import watcher
from ava.watcher import plans
from base.daemon.schedules.watcher import parse_timeout


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
    assert parse_timeout(value) == expected


@pytest.mark.parametrize("bad", ["", "5x", "later", "-3"])
def test_parse_timeout_rejects_bad_strings(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_timeout(bad)


def test_parse_timeout_rejects_nonpositive_and_bool() -> None:
    with pytest.raises(ValueError, match="positive"):
        parse_timeout(0)
    with pytest.raises(TypeError):
        parse_timeout(True)


_NOW = datetime.datetime(2026, 1, 1, 0, 0, 30, tzinfo=datetime.UTC)
_FAR = "2099-01-01T00:00:00Z"


def test_at_announcement_uses_cluster_zone_when_authoritative() -> None:
    """The plan passes the cluster timezone to the generated script when the
    process holds an authoritative one (user ruling 2026-08-27)."""
    request = plans.plan_at(
        _FAR,
        "ping later",
        name="test-at-cluster-tz",
        notify=None,
        now=_NOW,
        announce_timezone="Asia/Shanghai",
    )
    assert "_TZ = ZoneInfo('Asia/Shanghai')" in request.code
    assert "_WHEN.astimezone(_TZ).isoformat()" in request.code


def test_at_announcement_uses_host_clock_without_authoritative_zone() -> None:
    """A settings-lite process (no authoritative cluster timezone) passes
    None: the announcement renders in the watcher's own wall clock — the
    documented lite degradation."""
    request = plans.plan_at(
        _FAR, "ping later", name="test-at-lite", notify=None, now=_NOW, announce_timezone=None
    )
    assert "ZoneInfo" not in request.code
    assert "_WHEN.astimezone().isoformat()" in request.code


def test_cron_defaults_to_the_given_default_zone() -> None:
    """With no explicit timezone the plan uses the default zone it is handed
    (the cluster zone, else the host's own) — not the silent America/Los_Angeles
    field default."""
    request = plans.plan_cron(
        "0 * * * *",
        "tick",
        timezone=None,
        end_time=None,
        name="test-cron-lite",
        notify=None,
        now=_NOW,
        default_timezone="Europe/Paris",
    )
    assert "_TZ = ZoneInfo('Europe/Paris')" in request.code
    assert "America/Los_Angeles" not in request.code


def _cron(expr: str = "0 3 * * *", **overrides: Any) -> Any:
    args: dict[str, Any] = {
        "timezone": "UTC",
        "end_time": None,
        "name": "test-cron",
        "notify": None,
        "now": _NOW,
        "default_timezone": "UTC",
    }
    args.update(overrides)
    return plans.plan_cron(expr, "daily", **args)


def test_cron_invalid_expr_raises() -> None:
    from base.daemon.schedules.watcher import CronExprError

    with pytest.raises(CronExprError):
        _cron("not a cron")


def test_cron_invalid_timezone_raises() -> None:
    with pytest.raises(ValueError, match="timezone"):
        _cron(timezone="Not/A/Real/Timezone")


def test_at_past_time_raises() -> None:
    """at() with a past datetime raises ValueError."""
    past = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
    with pytest.raises(ValueError, match="past"):
        plans.plan_at(
            past, "too late", name="test-past", notify=None, now=_NOW, announce_timezone=None
        )


def test_cron_past_end_time_raises() -> None:
    """cron() with an explicit past `end_time` raises ValueError, aligned
    with at() (issue #2078: a past end otherwise supersedes the live twin
    and registers a watcher that self-terminates immediately). The plan is
    built before anything is spawned, so nothing registers for a past end."""
    past = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
    with pytest.raises(ValueError, match="end_time is in the past"):
        _cron(end_time=past)


def test_cron_future_end_time_ok() -> None:
    """An explicit future `end_time` plans normally (no past rejection)."""
    end = _NOW + datetime.timedelta(days=2)
    request = _cron(end_time=end)
    assert request.cron_end_at == end
    assert "daily" in request.code

    # timedelta going backwards should also fail
    with pytest.raises(ValueError):
        plans.plan_at(
            datetime.timedelta(days=-1),
            "negative delta",
            name="test-neg-delta",
            notify=None,
            now=datetime.datetime.now(datetime.UTC),
            announce_timezone=None,
        )


def test_at_future_time_ok() -> None:
    """at() with a future time should not raise about the past."""
    far = plans.plan_at(
        "2099-01-01T00:00:00Z",
        "far future",
        name="test-future",
        notify=None,
        now=_NOW,
        announce_timezone=None,
    )
    assert "far future" in far.code
    assert far.name == "test-future"

    soon = plans.plan_at(
        datetime.timedelta(hours=1),
        "one hour",
        name="test-delta",
        notify=None,
        now=_NOW,
        announce_timezone=None,
    )
    assert "one hour" in soon.code
