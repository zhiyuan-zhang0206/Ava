"""In-memory per-IP login failure rate limiting for the gateway.

The login endpoint is the gateway's only unauthenticated credential check,
so it is the one surface an online password guesser can hit without a
session. This module locks an IP out for a fixed window after a run of
consecutive failures.

State lives in a process-local dict — the gateway runs as a single uvicorn
process, so that dict IS the whole cluster. A gateway restart drops every
lockout and failure counter; acceptable here because brute force is a slow
online attack and a restart mid-attack merely re-arms the counter. No Redis
dependency on purpose: the point is to keep this small and obvious.

Preconditions (audit round-2 security P2 — break these and the limiter
silently degrades): single-process gateway, and NO reverse proxy in front —
the key is `request.client.host`, which behind a proxy is the proxy's
address (one user tripping the lockout would lock everyone) and the
failure counters would be shared across every proxied user. Both hold
today by deployment shape; re-check before adding a proxy or a second
gateway worker.

Policy (constants below — adjust deliberately, with justification):

- ``MAX_FAILURES`` consecutive failed logins per IP lock the IP for
  ``LOCKOUT_SECONDS`` (15 minutes). Five is the standard lockout threshold
  for admin surfaces: a handful of typos never locks anyone out, while a
  scripted guesser hits the wall on its fifth attempt. 15 minutes caps a
  single-IP attack at 96 attempts/day and forces a new IP to continue, yet
  a genuinely confused user is back in after a coffee break.
- A successful login resets the IP's failure counter.
- While locked, the login endpoint returns 429 + ``Retry-After`` instead of
  401: 401 reads as "wrong password" and invites the retry loop the lockout
  exists to stop, while 429 + Retry-After states plainly that the credential
  was not evaluated and when to retry.
- Lockout expiry resets the streak: the first attempt after the window
  starts a fresh counter.
"""

from __future__ import annotations

import math
import threading
import time

MAX_FAILURES = 5
"""Consecutive failed logins per IP that trigger a lockout."""

LOCKOUT_SECONDS = 900
"""How long an IP stays locked after hitting ``MAX_FAILURES`` (15 minutes)."""

_MAX_ENTRIES = 10_000
"""Soft cap on tracked IPs: when exceeded, expired entries are swept on the
next failure record. Bounds memory for a subnet scan without a background
timer."""


class _Entry:
    """Per-IP failure state."""

    __slots__ = ("failures", "last_failure_at", "locked_until")

    def __init__(self, failures: int, locked_until: float, last_failure_at: float) -> None:
        self.failures = failures
        self.locked_until = locked_until  # epoch seconds; 0 when not locked
        self.last_failure_at = last_failure_at  # epoch seconds of most recent failure


class LoginRateLimiter:
    """Per-IP consecutive-failure lockout, safe for concurrent requests.

    The gateway is single-process, so one instance covers the whole cluster;
    ``login_limiter`` below is the process-wide singleton the login endpoint
    uses. Methods are idempotent under races: a lock guards the dict, and
    worst-case concurrent failures just count a couple of attempts early.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def lockout_remaining(self, ip: str) -> int:
        """Whole seconds until ``ip``'s lockout expires; 0 when not locked."""
        now = time.time()
        with self._lock:
            entry = self._entries.get(ip)
            if entry is None or entry.locked_until <= now:
                return 0
            return math.ceil(entry.locked_until - now)

    def record_failure(self, ip: str) -> None:
        """Count one failed login for ``ip``, locking it at ``MAX_FAILURES``.

        A stale streak (lockout expired, or no failure for a full
        ``LOCKOUT_SECONDS`` window) restarts from one — failures are only
        "consecutive" while they keep coming.
        """
        now = time.time()
        with self._lock:
            entry = self._entries.get(ip)
            if entry is None or self._is_stale(entry, now):
                entry = _Entry(failures=0, locked_until=0.0, last_failure_at=now)
                self._entries[ip] = entry
            entry.failures += 1
            entry.last_failure_at = now
            if entry.failures >= MAX_FAILURES:
                entry.locked_until = now + LOCKOUT_SECONDS
            if len(self._entries) > _MAX_ENTRIES:
                self._sweep(now)

    def record_success(self, ip: str) -> None:
        """Clear ``ip``'s failure state — a successful login proves the IP is
        a legitimate user, at least for now."""
        with self._lock:
            self._entries.pop(ip, None)

    def reset(self) -> None:
        """Drop all state. Test isolation only — no production caller."""
        with self._lock:
            self._entries.clear()

    @staticmethod
    def _is_stale(entry: _Entry, now: float) -> bool:
        """True when a streak no longer counts: any lockout has expired AND no
        failure happened within a full lockout window, so the run is not
        consecutive anymore."""
        return entry.locked_until <= now and now - entry.last_failure_at > LOCKOUT_SECONDS

    def _sweep(self, now: float) -> None:
        """Drop entries whose streak is stale; over the soft cap, also trim the
        oldest active entries (audit 2026-08-08 P3: stale-only sweeping left
        the dict unbounded under a many-IP attack — each new IP can stay
        non-stale for a full lockout window by failing just under
        MAX_FAILURES per window). Caller holds ``self._lock``."""
        stale = [ip for ip, entry in self._entries.items() if self._is_stale(entry, now)]
        for ip in stale:
            del self._entries[ip]
        over = len(self._entries) - _MAX_ENTRIES
        if over > 0:
            # oldest failure first — the entries least likely to be an active
            # attacker right now
            oldest = sorted(self._entries, key=lambda ip: self._entries[ip].last_failure_at)[:over]
            for ip in oldest:
                del self._entries[ip]


login_limiter = LoginRateLimiter()
"""Process-wide limiter for the login endpoint (the gateway is single-process)."""
