"""Gateway-session (cookie) maintenance for the shared browser-mcp daemon.

The shared Chrome must carry a valid gateway web session so gateway URLs opened
in it do not hit a login wall. This module owns that cookie's whole lifecycle:
mint it through the gateway login endpoint and inject it into Chrome over CDP
at daemon start and then every ``_SESSION_REFRESH_INTERVAL_S``. A session
revoked or lost between ticks stays invalid until the next tick; nothing
re-checks it on navigation.

Every path is best-effort: a failure is logged, the daemon keeps serving, and
the next tick retries.
"""

from __future__ import annotations

import asyncio
import signal
from contextlib import suppress

from base.cluster.machine import (
    GatewayApiTokenMissing,
    gateway_api_base,
    gateway_bearer,
)
from base.cluster.machine.ambient import gateway_access_from_settings
from base.config import settings
from base.log import logger
from services.desktop.browser.mcp_upstream import await_stop_or_timeout, contained
from services.desktop.browser.session import inject_session_cookie

# Gateway session cookie refresh: the default server-side lifetime is 24h;
# refreshing every 6h leaves a comfortable margin and self-heals a lost,
# expired, or revoked managed-browser session within one interval.
_SESSION_REFRESH_INTERVAL_S = 6 * 3600


def gateway_session_params() -> tuple[str, str] | None:
    """(gateway_url, login credential) when a gateway session can be minted, else
    None (with a logged reason) — the daemon keeps serving either way, the
    browser just cannot open auth-gated gateway URLs without the cookie.

    The credential is this daemon's delivered runner API token (the login
    accepts the active generation's runner token). The human secret never
    stands in for a missing token: a daemon launched without one is logged as
    a launch defect and left without the cookie (best effort, like every
    other path here), except on a remote-managed data plane, which delivers no
    token and whose gateway home presents the human secret."""
    access = gateway_access_from_settings()  # ambient: S
    try:
        gateway_url = gateway_api_base(access.gateway_url)
    except Exception as e:  # gateway URL unset on this unit
        logger.warning(f"[browser-mcp] gateway session injection disabled: {e}")
        return None
    try:
        credential = gateway_bearer(access)
    except GatewayApiTokenMissing as e:
        logger.error(f"[browser-mcp] gateway session injection disabled: {e}")
        return None
    return gateway_url, credential


class GatewaySession:
    """State of the gateway session cookie the daemon keeps in the shared Chrome.

    Built once by the daemon's composition root (``mcp_daemon.run``). It owns
    the one-shot injection tasks, which live in the root's `TaskGroup` (`tasks`,
    owned by ``mcp_daemon.run``) and are tracked here so shutdown can cancel them
    (each removes itself on completion).
    """

    def __init__(self, tasks: asyncio.TaskGroup) -> None:
        self.inject_tasks: set[asyncio.Task[None]] = set()
        self._tasks = tasks

    def spawn_inject(self) -> None:
        """Schedule a one-shot gateway-session injection (best effort)."""
        task = self._tasks.create_task(contained(self.inject_once(), "gateway session injection"))
        self.inject_tasks.add(task)
        task.add_done_callback(self.inject_tasks.discard)

    def cancel_one_shots(self) -> None:
        """Cancel every in-flight one-shot so the owning group can close at shutdown."""
        for task in self.inject_tasks:
            task.cancel()

    async def inject_once(self) -> None:
        """Log in + inject the gateway session cookie into Chrome (best effort).

        Never raises: every failure is logged and left for the next tick to retry.
        """
        params = gateway_session_params()
        if params is None:
            return
        gateway_url, secret = params
        try:
            cdp_port = settings.services.browser_cdp_port
            await inject_session_cookie(cdp_port, gateway_url, secret)
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
            await await_stop_or_timeout(stop, _SESSION_REFRESH_INTERVAL_S)


def stop_on_signals() -> asyncio.Event:
    """The event SIGTERM/SIGINT set; every loop of the daemon waits on it."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    return stop
