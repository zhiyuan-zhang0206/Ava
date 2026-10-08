"""Severity routing and warning throttling for rejected gateway authentication."""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import Request

from base import telemetry

_log = logging.getLogger(__name__)

# Central auth-401 observability (task #1712): the per-request log line is
# DEBUG (SSE reconnect storms, PR #665) / WARNING-throttled per (client, path),
# so the count vanished from the event-derived metrics too. This process-local
# counter is incremented on EVERY middleware rejection — including the flooded
# and throttled ones — and flushed as ONE `auth401_rejected` aggregate event
# per FLUSH_INTERVAL_S. Bounded row rate (1/min), never per rejection.
_AUTH401_FLUSH_INTERVAL_S = 60.0

_AUTH401_WARN_COOLDOWN_S = 300.0


def _is_sse_poll_path(path: str) -> bool:
    """Return whether repeated auth failures are expected SSE reconnect noise."""
    # Live SSE routes end in /stream or /system, except the aggregate
    # system feed at /api/system/all.
    return path.endswith(("/stream", "/system")) or path == "/api/system/all"


def _is_browser_user_agent(user_agent: str) -> bool:
    """Return whether the UA names a browser engine (the console's own tabs).

    A rollout invalidates the browser session, and every open console tab
    retries its requests once with a 401 the UI itself surfaces — expected
    post-rollout noise, not a client misconfiguration (2026-10-03 triage #7).
    Scripted clients (curl / httpx / wget) do not carry `Mozilla/`."""
    return "Mozilla/" in user_agent


# Task #1635 / PR #610 stopped new bundles from blind-retrying 401'd SSE streams;
# Task #1694 treats SSE 401s as stale old-bundle tabs: expected reconnect noise.
# Uvicorn access logs are WARNING-gated (`base/log/__init__.py` `_install_stdlib_intercept`),
# so without this explicit log 401s are invisible. DEBUG keeps the forensic gateway.log
# trail out of events/Loki (only INFO+ derives). Non-stream sources stay visible at
# WARNING once per (client, path) per 300s while flood repeats are downgraded to DEBUG.


class AuthRejectionLog:
    """Authentication rejection counters and throttles owned by one gateway lifespan."""

    def __init__(self) -> None:
        self.total = 0
        self.last_warn: dict[tuple[str, str], float] = {}
        self.suppressed: dict[tuple[str, str], int] = {}

    def _prune(self, now: float) -> None:
        """Forget client/path keys idle for more than two warning windows."""
        stale_before = now - (2 * _AUTH401_WARN_COOLDOWN_S)
        for key, last_warn in tuple(self.last_warn.items()):
            if last_warn < stale_before:
                self.last_warn.pop(key, None)
                self.suppressed.pop(key, None)

    def log(self, request: Request) -> None:
        """Log an auth rejection at the route-appropriate severity and cadence.

        Counts EVERY rejection (flooded SSE reconnects and throttled repeats
        included) into the process-local aggregate — the log severity cadence
        decides what a human sees, never whether the count is observable.
        """
        self.total += 1
        path = request.url.path
        client = request.client.host if request.client else "unknown"
        user_agent = request.headers.get("user-agent", "-")
        if _is_sse_poll_path(path) or _is_browser_user_agent(user_agent):
            _log.debug(
                "auth 401: path=%s client=%s ua=%s",
                path,
                client,
                user_agent,
            )
            return

        now = time.monotonic()
        self._prune(now)
        key = (client, path)
        last_warn = self.last_warn.get(key)
        if last_warn is None:
            self.last_warn[key] = now
            self.suppressed.pop(key, None)
            _log.warning(
                "auth 401: path=%s client=%s ua=%s",
                path,
                client,
                user_agent,
            )
            return

        if now - last_warn < _AUTH401_WARN_COOLDOWN_S:
            suppressed = self.suppressed.get(key, 0) + 1
            self.suppressed[key] = suppressed
            _log.debug(
                "auth 401: path=%s client=%s ua=%s "
                "(suppressed repeat %d until the %ds warning cooldown elapses)",
                path,
                client,
                user_agent,
                suppressed,
                _AUTH401_WARN_COOLDOWN_S,
            )
            return

        suppressed = self.suppressed.pop(key, 0)
        self.last_warn[key] = now
        _log.warning(
            "auth 401: path=%s client=%s ua=%s (suppressed %d repeats in the last %ds)",
            path,
            client,
            user_agent,
            suppressed,
            _AUTH401_WARN_COOLDOWN_S,
        )

    def drain(self) -> int:
        """Take the aggregate rejection count since the last flush (event-loop only).

        The middleware and the flusher both run on the gateway's single event
        loop, so no locking is needed — same convention as `gateway.http.middleware.latency`.
        """
        count = self.total
        self.total = 0
        return count


def emit_auth401_count(count: int) -> None:
    """Emit one `auth401_rejected` aggregate event for `count` rejections.

    A zero window emits nothing — keep the Prometheus series continuous but
    avoid minting zero-valued datapoints for idle gateways. Exposed separate
    from the flusher so tests can drive it directly (mirrors `latency.emit_bucket`).
    """
    if count <= 0:
        return
    telemetry.emit(
        "telemetry",
        "auth401_rejected",
        attributes={"count": count},
    )


async def auth401_flusher(rejections: AuthRejectionLog) -> None:
    """Drain the aggregate counter every `_AUTH401_FLUSH_INTERVAL_S` and emit.

    Runs as a lifespan background task. A failed flush never kills the loop —
    the next tick retries (a dropped bucket is only a monitoring gap, and the
    emit pipeline itself is already best-effort).
    """
    # quiesce-exempt: drains an in-process counter into telemetry; no database
    while True:
        await asyncio.sleep(_AUTH401_FLUSH_INTERVAL_S)
        try:
            emit_auth401_count(rejections.drain())
        except Exception:
            _log.warning("auth401 count flush failed", exc_info=True)
