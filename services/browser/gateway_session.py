"""Gateway-session (cookie) maintenance for the shared browser-mcp daemon.

The shared Chrome must carry a valid gateway web session so gateway URLs opened
in it do not hit a login wall. This module owns that cookie's whole lifecycle:
mint it through the gateway login endpoint and inject it into Chrome over CDP,
refresh it every ``_SESSION_REFRESH_INTERVAL_S``, and re-verify it on demand
when a navigation lands on the gateway's own origin -- a revoked session
surfaces as a 401 exactly there, so the check heals it immediately instead of
waiting out the refresh interval.

Every path is best-effort: a failure is logged, the daemon keeps serving, and
the next tick or gateway navigation retries.
"""

from __future__ import annotations

import asyncio
import signal
from contextlib import suppress
from typing import Any
from urllib.parse import urlsplit

from services.browser.mcp_upstream import _await_stop_or_timeout
from services.browser.session import (
    gateway_session_is_valid,
    inject_session_cookie,
    last_injected_cookie,
)
from shared.config import settings
from shared.log import logger
from shared.machine import gateway_api_base

# Gateway session cookie refresh: the default server-side lifetime is 24h;
# refreshing every 6h leaves a comfortable margin and self-heals a lost,
# expired, or revoked managed-browser session. A navigation to a gateway URL
# additionally triggers an immediate validity check (_spawn_verify), so a
# revoked session heals on the next gateway page instead of waiting out the
# interval.
_SESSION_REFRESH_INTERVAL_S = 6 * 3600


def _gateway_session_params() -> tuple[str, str] | None:
    """(gateway_url, cluster_secret) when a gateway session can be minted, else
    None (with a logged reason) — the daemon keeps serving either way, the
    browser just cannot open auth-gated gateway URLs without the cookie."""
    try:
        gateway_url = gateway_api_base()
    except Exception as e:  # gateway URL unset on this unit
        logger.warning(f"[browser-mcp] gateway session injection disabled: {e}")
        return None
    secret = settings.data_plane.cluster_secret
    if not secret:
        logger.warning("[browser-mcp] gateway session injection disabled: empty cluster secret")
        return None
    return gateway_url, secret


async def _inject_gateway_session_once() -> None:
    """Log in + inject the gateway session cookie into Chrome (best effort).

    Never raises: every failure is logged and left for the next tick / the
    next upstream connect to retry.
    """
    params = _gateway_session_params()
    if params is None:
        return
    gateway_url, secret = params
    try:
        await inject_session_cookie(settings.services.browser_cdp_port, gateway_url, secret)
    except Exception as e:
        logger.warning(f"[browser-mcp] gateway session cookie injection failed: {e}")
        return
    logger.info(f"[browser-mcp] gateway session cookie injected for {gateway_url}")


async def _gateway_session_loop(stop: asyncio.Event) -> None:
    """Refresh the gateway session cookie every _SESSION_REFRESH_INTERVAL_S.

    The refresh cadence stays inside the configured session lifetime and
    self-heals a lost/revoked cookie within one interval. The loop never
    raises — failures are logged and retried on the next tick.
    """
    while not stop.is_set():
        await _inject_gateway_session_once()
        await _await_stop_or_timeout(stop, _SESSION_REFRESH_INTERVAL_S)


# Fire-and-forget injection tasks are tracked so the event loop never reaps
# them before they finish (RUF006); each task removes itself on completion.
_inject_tasks: set[asyncio.Task[None]] = set()


def _spawn_inject() -> None:
    """Schedule a one-shot gateway-session injection (best effort)."""
    task = asyncio.create_task(_inject_gateway_session_once())
    _inject_tasks.add(task)
    task.add_done_callback(_inject_tasks.discard)


def _navigates_to_gateway(name: str, args: dict[str, Any]) -> bool:
    """True when the call opens a URL under the gateway's own origin.

    Gateway-served pages are exactly where a revoked or expired managed
    session surfaces as a 401, so a navigation there is the early-refresh
    trigger. Best effort: an unresolvable gateway base simply means no check.
    """
    if name not in ("navigate_page", "new_page"):
        return False
    url = args.get("url")
    if not isinstance(url, str):
        return False
    try:
        gateway_base = gateway_api_base()
    except Exception:
        return False
    target = urlsplit(url)
    gateway = urlsplit(gateway_base)
    return target.scheme in ("http", "https") and (target.scheme, target.netloc) == (
        gateway.scheme,
        gateway.netloc,
    )


_verify_lock = asyncio.Lock()


async def _verify_gateway_session_once() -> None:
    """Re-inject the gateway session cookie when the stored one no longer
    authenticates (revoked or expired), so a 401 heals on the next gateway
    navigation instead of waiting out the refresh interval.

    Best effort like ``_inject_gateway_session_once``: failures are logged and
    left for the next trigger to retry. With no stored cookie (fresh daemon)
    this falls back to a plain injection. Concurrent verifies are collapsed
    onto the in-flight one — a single re-injection is enough.
    """
    if _verify_lock.locked():
        return
    async with _verify_lock:
        params = _gateway_session_params()
        if params is None:
            return
        gateway_url, _ = params
        cookie = last_injected_cookie()
        if cookie is None:
            await _inject_gateway_session_once()
            return
        _, value = cookie
        try:
            valid = await gateway_session_is_valid(gateway_url, value)
        except Exception as e:
            logger.warning(f"[browser-mcp] gateway session validity check failed: {e}")
            return
        if not valid:
            logger.info("[browser-mcp] gateway session no longer valid — refreshing early")
            await _inject_gateway_session_once()


# Fire-and-forget verify tasks are tracked like injections (RUF006).
_verify_tasks: set[asyncio.Task[None]] = set()


def _spawn_verify() -> None:
    """Schedule a one-shot gateway-session validity check (best effort)."""
    task = asyncio.create_task(_verify_gateway_session_once())
    _verify_tasks.add(task)
    task.add_done_callback(_verify_tasks.discard)


async def _start_session_maintenance() -> tuple[asyncio.Event, asyncio.Task[None]]:
    """SIGTERM/SIGINT stop event + the long-lived session-refresh task.

    The refresh loop keeps a valid gateway session cookie in the shared
    Chrome: inject at startup, then refresh before the server-side row
    expires. It is independent of the upstream session — a Chrome restart
    that drops the upstream does not lose the cookie (the profile persists),
    and the periodic tick heals anything that did change (fresh profile,
    revocation, expiry).
    """
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    session_task = asyncio.create_task(_gateway_session_loop(stop))
    return stop, session_task
