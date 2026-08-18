"""IM Bridge daemon — runs every configured IM channel adapter.

Each IM channel is one adapter (service) sharing the bridge core: message
envelope, command routing, per-channel session state, and SSE subscription
push. Adapters are optional at import time — a channel whose adapter module
is missing or whose credentials are unset logs "skipped" and the daemon keeps
serving the others.

Usage:
    .venv/bin/python -m services.im_bridge.daemon
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from contextlib import suppress
from typing import Any

from services._pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile
from shared.config import settings
from shared.daemon_health import Liveness, health_port, start_health_server, stop_health_server
from shared.daemon_shutdown import install_graceful_shutdown
from shared.log import init_gateway_process
from shared.paths import legacy_pid_path

_log = logging.getLogger("services.im_bridge.daemon")

# Staleness ceiling for the healthz liveness — generous: the daemon is
# mostly idle waiting on long polls; only a wedged event loop should trip it.
_LIVENESS_TIMEOUT_S = 120.0
# The daemon's main loop never iterates (it waits on an event once the
# adapters are launched), so a background task carries the heartbeat; the
# interval sits well under the staleness ceiling.
_LIVENESS_BEAT_INTERVAL_S = 30.0
_PIDFILE = settings.services.im_bridge_pidfile


def _write_pidfile() -> None:
    if not acquire_pidfile(_PIDFILE, "services.im_bridge.daemon"):
        _log.info("[im_bridge] daemon already running (pidfile=%s), exiting", _PIDFILE)
        sys.exit(1)


def _remove_pidfile() -> None:
    remove_pidfile(_PIDFILE)


def _is_running() -> bool:
    """Whether a daemon is already running (via pidfile, new + legacy paths).

    Pid-reuse-safe: a live pid whose argv does not name this daemon's module
    is a recycled pid, not a running instance (audit round 2, P1)."""
    return pidfile_holds_daemon(_PIDFILE, "services.im_bridge.daemon") or pidfile_holds_daemon(
        legacy_pid_path("im_bridge"), "services.im_bridge.daemon"
    )


def _import_adapter(name: str) -> Any:
    """Import one adapter module by channel name (seam for tests)."""

    return __import__(f"services.im_bridge.adapters.{name}", fromlist=["*"])


def _load_adapters(core: Any) -> list[Any]:
    """Import each channel adapter; a missing module or failed import logs and
    skips — one broken channel must not take down the bridge."""

    loaded: list[Any] = []
    disabled = set(settings.services.im_disabled_adapters)
    for name in ("telegram", "weixin", "feishu"):
        if name in disabled:
            _log.info("im_bridge: adapter %s disabled by config (AVA_IM_DISABLED_ADAPTERS)", name)
            continue
        try:
            mod = _import_adapter(name)
            adapter_cls = mod.ADAPTER_CLASS
            adapter = adapter_cls(core)
            core.register(adapter)
            loaded.append(adapter)
            _log.info("im_bridge: adapter %s loaded", name)
        except ImportError as exc:
            _log.warning("im_bridge: adapter %s unavailable (skipped): %r", name, exc)
        except Exception:
            _log.exception("im_bridge: adapter %s failed to load (skipped)", name)
    return loaded


async def _gateway_login_with_retry(core: Any, liveness: Liveness) -> None:
    """Login to the gateway with backoff — the gateway may be mid-restart."""
    delay = 2.0
    while True:
        liveness.beat()
        try:
            await core.gateway.login()
            _log.info("im_bridge: gateway login ok")
            return
        except Exception as exc:
            _log.warning("im_bridge: gateway login failed: %r (retry in %.0fs)", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)


async def _liveness_loop(liveness: Liveness) -> None:
    """Beat the healthz liveness while the daemon serves.

    The main loop's work happens in adapter background tasks and the final
    ``asyncio.Event().wait()`` never iterates, so this task is the heartbeat:
    a wedged event loop stops scheduling it and the staleness ceiling trips.
    """

    while True:
        liveness.beat()
        await asyncio.sleep(_LIVENESS_BEAT_INTERVAL_S)


async def _notice_loop(core: Any) -> None:
    """Poll the gateway for new fleet notices and push them to the owner
    chat (Task #884). Cursor + filter persist across restarts."""

    while True:
        await core.notice_bridge.poll_once()
        await asyncio.sleep(3.0)


async def _handle_send(core: Any) -> Any:
    """Route handler for the daemon's ``POST /send`` RPC (ops-alerts fan-out).

    Body: ``{"text": str}`` — fanned out to every loaded adapter's owner chat
    via ``core.notify_user``. The gateway calls this with the cluster secret
    as Bearer (the health server's ``auth_token``). Returns per-channel
    results; a channel that failed to send is reported, not fatal. When
    EVERY channel failed (or none is loaded) the route answers 502 instead
    of 200 — the caller (shared/alerts.py) keys ``notified_at`` off the status
    code, and a fake 200 would stamp a message that never reached the user.
    """

    async def handle(body: bytes) -> tuple[int, bytes, str]:
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            return 400, b'{"error": "invalid json body"}', "application/json"
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return 400, b'{"error": "text (non-empty string) required"}', "application/json"
        results = await core.notify_user(text)
        delivered = any(v == "ok" for v in results.values())
        if not delivered:
            # Nothing reached the user — report failure so the caller does not
            # treat the fan-out as delivered (shared/alerts.py keeps notified_at NULL
            # and retries on the next Grafana re-send).
            return 502, json.dumps({"results": results}).encode(), "application/json"
        return 200, json.dumps({"results": results}).encode(), "application/json"

    return handle


async def run() -> None:
    """Start the daemon: healthz -> pidfile -> load adapters -> serve."""
    if _is_running():
        _log.info("[im_bridge] daemon already running (pidfile=%s), exiting", _PIDFILE)
        sys.exit(1)

    _write_pidfile()
    _log.info("[im_bridge] pidfile written: %s", _PIDFILE)

    # The notice bridge reads agent_notices directly (R3 door ④ — decoupled
    # from gateway availability, so a paused cluster cannot stall notice
    # delivery); the pool is created here and owned by the daemon.
    import shared.db as _db
    from services.im_bridge.core import IMBridgeCore

    db_pool = _db.pool()
    core = IMBridgeCore(db_pool=db_pool)
    liveness = Liveness(_LIVENESS_TIMEOUT_S)
    try:
        health = await start_health_server(
            "im_bridge",
            liveness=liveness,
            extra_routes={("POST", "/send"): await _handle_send(core)},
            # Bearer = the cluster secret; empty-secret clusters (single-box
            # no-auth posture) get no auth — consistent with the gateway.
            auth_token=settings.data_plane.cluster_secret or None,
        )
    except Exception:
        _remove_pidfile()
        raise
    _log.info("[im_bridge] healthz listening on :%s", health_port("im_bridge"))

    adapters = _load_adapters(core)
    if not adapters:
        _log.warning("im_bridge: no adapters loaded — nothing to serve")

    liveness_task: asyncio.Task[None] | None = None
    notice_task: asyncio.Task[None] | None = None
    try:
        await _gateway_login_with_retry(core, liveness)
        # Drain anything the previous process outboxed when the gateway was
        # down (Task #1032: user messages must not drop across restarts).
        core.ensure_outbox_replay()
        # Rebuild SSE push subscriptions from persisted switch state — they
        # are memory-only and a restart drops them (Task #804: agent replies
        # silently stopped reaching Telegram after the 04:32 restart).
        await core.restore_subscriptions()
        liveness_task = asyncio.create_task(_liveness_loop(liveness))
        notice_task = asyncio.create_task(_notice_loop(core))
        tasks = [asyncio.create_task(a.start()) for a in adapters]
        await asyncio.gather(*tasks)
        # Every adapter's start() returns once its connection loop is launched
        # (long polls / ws threads run in the background). The daemon now stays
        # alive forever; SIGTERM/SIGINT unwinds through the finally below.
        await asyncio.Event().wait()
    finally:
        if liveness_task is not None:
            liveness_task.cancel()
        if notice_task is not None:
            notice_task.cancel()
        for a in adapters:
            with suppress(Exception):
                await a.stop()
        await stop_health_server(health)
        db_pool.close()
        _remove_pidfile()
        _log.info("[im_bridge] daemon stopped")


def main() -> None:
    init_gateway_process("im_bridge")
    install_graceful_shutdown("im_bridge")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        _log.info("[im_bridge] interrupted")


if __name__ == "__main__":
    main()
