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

from base.cluster.machine import (
    GatewayApiBaseMissing,
    GatewayApiTokenMissing,
    gateway_api_base,
    gateway_bearer,
)
from base.config import settings
from base.log import logger
from services.desktop.browser.mcp_upstream import _await_stop_or_timeout, contained
from services.desktop.browser.session import gateway_session_is_valid, inject_session_cookie

# Gateway session cookie refresh: the default server-side lifetime is 24h;
# refreshing every 6h leaves a comfortable margin and self-heals a lost,
# expired, or revoked managed-browser session. A navigation to a gateway URL
# additionally triggers an immediate validity check (`spawn_verify`), so a
# revoked session heals on the next gateway page instead of waiting out the
# interval.
_SESSION_REFRESH_INTERVAL_S = 6 * 3600


def _gateway_session_params() -> tuple[str, str] | None:
    """(gateway_url, login credential) when a gateway session can be minted, else
    None (with a logged reason) — the daemon keeps serving either way, the
    browser just cannot open auth-gated gateway URLs without the cookie.

    The credential is this daemon's delivered runner API token (the login
    accepts the active generation's runner token). The human secret never
    stands in for a missing token: a daemon launched without one is logged as
    a launch defect and left without the cookie (best effort, like every
    other path here), except on a remote-managed data plane, which delivers no
    token and whose gateway home presents the human secret."""
    try:
        gateway_url = gateway_api_base()
    except Exception as e:  # gateway URL unset on this unit
        logger.warning(f"[browser-mcp] gateway session injection disabled: {e}")
        return None
    try:
        credential = gateway_bearer()
    except GatewayApiTokenMissing as e:
        logger.error(f"[browser-mcp] gateway session injection disabled: {e}")
        return None
    if not credential:
        logger.warning("[browser-mcp] gateway session injection disabled: the cluster API is open")
        return None
    return gateway_url, credential


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
    except GatewayApiBaseMissing:
        return False
    target = urlsplit(url)
    gateway = urlsplit(gateway_base)
    return target.scheme in ("http", "https") and (target.scheme, target.netloc) == (
        gateway.scheme,
        gateway.netloc,
    )


class GatewaySession:
    """State of the gateway session cookie the daemon keeps in the shared Chrome.

    Built once by the daemon's composition root (``mcp_daemon.run``) and handed
    to every ``ChromeMcpDaemon`` it creates, so it survives upstream reconnects:
    the cookie most recently handed to Chrome (the early-refresh check compares
    it with the gateway after a gateway-URL navigation, so a revoked or expired
    managed session heals immediately instead of waiting out the next refresh
    tick), the verify lock that collapses concurrent verifies, and the
    fire-and-forget tasks, which live in the root's `TaskGroup` (`tasks`, owned by
    ``mcp_daemon.run``) and are tracked here so shutdown can cancel them (each removes
    itself on completion).
    """

    def __init__(self, tasks: asyncio.TaskGroup) -> None:
        self.cookie: tuple[str, str] | None = None
        self.inject_tasks: set[asyncio.Task[None]] = set()
        self.verify_tasks: set[asyncio.Task[None]] = set()
        self._tasks = tasks
        self._verify_lock = asyncio.Lock()

    def spawn_inject(self) -> None:
        """Schedule a one-shot gateway-session injection (best effort)."""
        task = self._tasks.create_task(contained(self.inject_once(), "gateway session injection"))
        self.inject_tasks.add(task)
        task.add_done_callback(self.inject_tasks.discard)

    def spawn_verify(self) -> None:
        """Schedule a one-shot gateway-session validity check (best effort)."""
        task = self._tasks.create_task(contained(self.verify_once(), "gateway session verify"))
        self.verify_tasks.add(task)
        task.add_done_callback(self.verify_tasks.discard)

    def cancel_one_shots(self) -> None:
        """Cancel every in-flight one-shot so the owning group can close at shutdown."""
        for task in (*self.inject_tasks, *self.verify_tasks):
            task.cancel()

    async def inject_once(self) -> None:
        """Log in + inject the gateway session cookie into Chrome (best effort).

        Never raises: every failure is logged and left for the next tick / the
        next upstream connect to retry.
        """
        params = _gateway_session_params()
        if params is None:
            return
        gateway_url, secret = params
        try:
            self.cookie = await inject_session_cookie(
                settings.services.browser_cdp_port, gateway_url, secret
            )
        except Exception as e:
            logger.warning(f"[browser-mcp] gateway session cookie injection failed: {e}")
            return
        logger.info(f"[browser-mcp] gateway session cookie injected for {gateway_url}")

    async def refresh_loop(self, stop: asyncio.Event) -> None:
        """Refresh the gateway session cookie every _SESSION_REFRESH_INTERVAL_S.

        The refresh cadence stays inside the configured session lifetime and
        self-heals a lost/revoked cookie within one interval. The loop never
        raises — failures are logged and retried on the next tick.
        """
        while not stop.is_set():
            await self.inject_once()
            await _await_stop_or_timeout(stop, _SESSION_REFRESH_INTERVAL_S)

    async def verify_once(self) -> None:
        """Re-inject the gateway session cookie when the stored one no longer
        authenticates (revoked or expired), so a 401 heals on the next gateway
        navigation instead of waiting out the refresh interval.

        Best effort like ``inject_once``: failures are logged and left for the
        next trigger to retry. With no stored cookie (fresh daemon) this falls
        back to a plain injection. Concurrent verifies are collapsed onto the
        in-flight one — a single re-injection is enough.
        """
        if self._verify_lock.locked():
            return
        async with self._verify_lock:
            params = _gateway_session_params()
            if params is None:
                return
            gateway_url, _ = params
            cookie = self.cookie
            if cookie is None:
                await self.inject_once()
                return
            _, value = cookie
            try:
                valid = await gateway_session_is_valid(gateway_url, value)
            except Exception as e:
                logger.warning(f"[browser-mcp] gateway session validity check failed: {e}")
                return
            if not valid:
                logger.info("[browser-mcp] gateway session no longer valid — refreshing early")
                await self.inject_once()


def stop_on_signals() -> asyncio.Event:
    """The event SIGTERM/SIGINT set; every loop of the daemon waits on it."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    return stop
