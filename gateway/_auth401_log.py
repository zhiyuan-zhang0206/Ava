"""Severity routing and warning throttling for rejected gateway authentication."""

from __future__ import annotations

import logging
import time

from fastapi import Request

_log = logging.getLogger(__name__)

_AUTH401_WARN_COOLDOWN_S = 300.0
_auth401_last_warn: dict[tuple[str, str], float] = {}
_auth401_suppressed: dict[tuple[str, str], int] = {}


def _is_sse_poll_path(path: str) -> bool:
    """Return whether repeated auth failures are expected SSE reconnect noise."""
    # Live SSE routes end in /stream or /system, except the aggregate
    # system feed at /api/system/all.
    return path.endswith(("/stream", "/system")) or path == "/api/system/all"


def _prune_auth401_throttle(now: float) -> None:
    """Forget client/path keys idle for more than two warning windows."""
    stale_before = now - (2 * _AUTH401_WARN_COOLDOWN_S)
    for key, last_warn in tuple(_auth401_last_warn.items()):
        if last_warn < stale_before:
            _auth401_last_warn.pop(key, None)
            _auth401_suppressed.pop(key, None)


# Task #1635 / PR #610 stopped new bundles from blind-retrying 401'd SSE streams;
# Task #1694 treats SSE 401s as stale old-bundle tabs: expected reconnect noise.
# Uvicorn access logs are WARNING-gated (`shared/log.py` `_install_stdlib_intercept`),
# so without this explicit log 401s are invisible. DEBUG keeps the forensic gateway.log
# trail out of events/Loki (only INFO+ derives). Non-stream sources stay visible at
# WARNING once per (client, path) per 300s while flood repeats are downgraded to DEBUG.
def _log_auth401_rejection(request: Request) -> None:
    """Log an auth rejection at the route-appropriate severity and cadence."""
    path = request.url.path
    client = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent", "-")
    if _is_sse_poll_path(path):
        _log.debug(
            "auth 401: path=%s client=%s ua=%s",
            path,
            client,
            user_agent,
        )
        return

    now = time.monotonic()
    _prune_auth401_throttle(now)
    key = (client, path)
    last_warn = _auth401_last_warn.get(key)
    if last_warn is None:
        _auth401_last_warn[key] = now
        _auth401_suppressed.pop(key, None)
        _log.warning(
            "auth 401: path=%s client=%s ua=%s",
            path,
            client,
            user_agent,
        )
        return

    if now - last_warn < _AUTH401_WARN_COOLDOWN_S:
        suppressed = _auth401_suppressed.get(key, 0) + 1
        _auth401_suppressed[key] = suppressed
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

    suppressed = _auth401_suppressed.pop(key, 0)
    _auth401_last_warn[key] = now
    _log.warning(
        "auth 401: path=%s client=%s ua=%s (suppressed %d repeats in the last %ds)",
        path,
        client,
        user_agent,
        suppressed,
        _AUTH401_WARN_COOLDOWN_S,
    )
