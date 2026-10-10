"""Unit tests for LoginRateLimiter's cap-trim behavior (audit 2026-08-08 P3).

The dict used to be bounded only by the stale sweep: under a many-IP attack
each fresh IP can stay non-stale for a full lockout window (failing just
under the configured login_max_failures per window), so the entry
count kept climbing past the
soft cap forever.
"""

from __future__ import annotations

import pytest

from base.cluster.rate_limit import LoginRateLimiter, _Entry
from base.config import ConfigBoot, settings


def test_sweep_trims_oldest_active_entries_over_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Over the soft cap, the OLDEST entries are dropped even when none are
    stale — the memory bound holds under a sustained many-IP attack."""
    limiter = LoginRateLimiter(
        max_failures_reader=lambda: settings.gateway.login_max_failures,
        lockout_seconds_reader=lambda: settings.gateway.login_lockout_seconds,
    )
    # shrink the cap so the test needs no 10k entries
    monkeypatch.setattr("base.cluster.rate_limit._MAX_ENTRIES", 5)
    now = 1000.0
    for i in range(8):
        ip = f"10.0.0.{i}"
        limiter._entries[ip] = _Entry(
            failures=settings.gateway.login_max_failures - 1,
            locked_until=0.0,
            last_failure_at=now - (8 - i),
        )
    assert len(limiter._entries) == 8
    limiter._sweep(now, settings.gateway.login_lockout_seconds)
    # cap is 5: the 3 oldest (last_failure_at smallest) are gone
    remaining = sorted(limiter._entries)
    assert len(remaining) == 5
    assert "10.0.0.0" not in remaining and "10.0.0.1" not in remaining
    assert "10.0.0.2" not in remaining
    assert "10.0.0.7" in remaining


def test_sweep_stale_removed_first() -> None:
    """Stale entries are dropped before the oldest-active trim — a stale
    streak is the first candidate for reclamation."""
    limiter = LoginRateLimiter(
        max_failures_reader=lambda: settings.gateway.login_max_failures,
        lockout_seconds_reader=lambda: settings.gateway.login_lockout_seconds,
    )
    now = 1000.0
    limiter._entries["stale-ip"] = _Entry(failures=1, locked_until=0.0, last_failure_at=now - 99999)
    limiter._entries["active-ip"] = _Entry(failures=1, locked_until=0.0, last_failure_at=now)
    limiter._sweep(now, settings.gateway.login_lockout_seconds)
    assert "stale-ip" not in limiter._entries
    assert "active-ip" in limiter._entries


def test_policy_readers_are_lazy_and_read_in_order_for_each_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def now() -> float:
        calls.append("now")
        return 1000.0

    def maximum() -> int:
        calls.append("max_failures")
        return 2

    def window() -> float:
        calls.append("lockout_seconds")
        return 17.5

    monkeypatch.setattr("base.cluster.rate_limit.time.time", now)
    limiter = LoginRateLimiter(max_failures_reader=maximum, lockout_seconds_reader=window)
    assert calls == []
    limiter.record_failure("ip")
    limiter.record_failure("ip")
    assert calls == ["now", "max_failures", "lockout_seconds"] * 2
    assert limiter.lockout_remaining("ip") == 18
    calls.clear()
    limiter.record_success("ip")
    limiter.reset()
    assert calls == []


def test_independent_config_owners_update_policy_at_failure_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = ConfigBoot(), ConfigBoot()
    first.set_field("login_max_failures", 2)
    first.set_field("login_lockout_seconds", 100)
    second.set_field("login_max_failures", 3)
    second.set_field("login_lockout_seconds", 30)
    left = LoginRateLimiter(
        max_failures_reader=lambda: first.view.gateway.login_max_failures,
        lockout_seconds_reader=lambda: first.view.gateway.login_lockout_seconds,
    )
    right = LoginRateLimiter(
        max_failures_reader=lambda: second.view.gateway.login_max_failures,
        lockout_seconds_reader=lambda: second.view.gateway.login_lockout_seconds,
    )
    now = [1000.0]
    monkeypatch.setattr("base.cluster.rate_limit.time.time", lambda: now[0])
    for limiter in (left, right):
        limiter.record_failure("ip")
        limiter.record_failure("ip")
    assert left.lockout_remaining("ip") == 100
    assert right.lockout_remaining("ip") == 0
    first.set_field("login_max_failures", 4)
    first.set_field("login_lockout_seconds", 7)
    left.record_success("ip")
    for _ in range(3):
        left.record_failure("ip")
    assert left.lockout_remaining("ip") == 0
    left.record_failure("ip")
    assert left.lockout_remaining("ip") == 7
    right.record_failure("ip")
    assert right.lockout_remaining("ip") == 30
    first.set_field("login_lockout_seconds", 11)
    left.record_failure("ip")
    assert left.lockout_remaining("ip") == 11
    assert right.lockout_remaining("ip") == 30
    now[0] += 12
    assert left.lockout_remaining("ip") == 0
    left.record_failure("ip")
    assert left.lockout_remaining("ip") == 0
    assert right.lockout_remaining("ip") == 18
