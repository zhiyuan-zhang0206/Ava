"""Unit tests for LoginRateLimiter's cap-trim behavior (audit 2026-08-08 P3).

The dict used to be bounded only by the stale sweep: under a many-IP attack
each fresh IP can stay non-stale for a full lockout window (failing just
under MAX_FAILURES per window), so the entry count kept climbing past the
soft cap forever.
"""

from __future__ import annotations

import pytest

from shared.rate_limit import MAX_FAILURES, LoginRateLimiter, _Entry


def test_sweep_trims_oldest_active_entries_over_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Over the soft cap, the OLDEST entries are dropped even when none are
    stale — the memory bound holds under a sustained many-IP attack."""
    limiter = LoginRateLimiter()
    # shrink the cap so the test needs no 10k entries
    monkeypatch.setattr("shared.rate_limit._MAX_ENTRIES", 5)
    now = 1000.0
    for i in range(8):
        ip = f"10.0.0.{i}"
        limiter._entries[ip] = _Entry(
            failures=MAX_FAILURES - 1, locked_until=0.0, last_failure_at=now - (8 - i)
        )
    assert len(limiter._entries) == 8
    limiter._sweep(now)
    # cap is 5: the 3 oldest (last_failure_at smallest) are gone
    remaining = sorted(limiter._entries)
    assert len(remaining) == 5
    assert "10.0.0.0" not in remaining and "10.0.0.1" not in remaining
    assert "10.0.0.2" not in remaining
    assert "10.0.0.7" in remaining


def test_sweep_stale_removed_first() -> None:
    """Stale entries are dropped before the oldest-active trim — a stale
    streak is the first candidate for reclamation."""
    limiter = LoginRateLimiter()
    now = 1000.0
    limiter._entries["stale-ip"] = _Entry(failures=1, locked_until=0.0, last_failure_at=now - 99999)
    limiter._entries["active-ip"] = _Entry(failures=1, locked_until=0.0, last_failure_at=now)
    limiter._sweep(now)
    assert "stale-ip" not in limiter._entries
    assert "active-ip" in limiter._entries
