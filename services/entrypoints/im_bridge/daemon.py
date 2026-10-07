"""IM Bridge daemon — runs every configured IM channel adapter.

Each IM channel is one adapter (service) sharing the bridge core: message
envelope, command routing, per-channel session state, and SSE subscription
push. Adapters are optional at import time — a channel whose adapter module
is missing or whose credentials are unset logs "skipped" and the daemon keeps
serving the others.

Usage:
    .venv/bin/python -m services.entrypoints.im_bridge.daemon
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from base.cluster.machine import daemon_acceptance, gateway_auth_headers
from base.config import settings
from base.daemon.endpoints import ServiceEndpoint, ServiceEndpoints
from base.daemon.health import Liveness, start_health_server, stop_health_server
from base.daemon.shutdown import cancel_and_drain, install_graceful_shutdown
from base.daemon.shutdown import hard_exit as _hard_exit
from base.db import Database
from base.deploy.maintenance import admission
from base.log import init_gateway_process
from services.entrypoints.im_bridge.config import (
    FeishuCredentialsConfig,
    ImBridgeConfig,
    TelegramCredentialsConfig,
)
from services.entrypoints.im_bridge.gateway_client import GatewayClient
from services.pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile

_log = logging.getLogger("services.entrypoints.im_bridge.daemon")

# Staleness ceiling for the healthz liveness — generous: the daemon is
# mostly idle waiting on long polls; only a wedged event loop should trip it.
_LIVENESS_TIMEOUT_S = 120.0
# The daemon's main loop never iterates (it waits on an event once the
# adapters are launched), so a background task carries the heartbeat; the
# interval sits well under the staleness ceiling.
_LIVENESS_BEAT_INTERVAL_S = 30.0


def _endpoint() -> ServiceEndpoint:
    return ServiceEndpoints.from_settings().of("im_bridge")


def _pidfile() -> Path:
    return _endpoint().pidfile


def _write_pidfile() -> None:
    if not acquire_pidfile(_pidfile(), "services.entrypoints.im_bridge.daemon"):
        _log.info("[im_bridge] daemon already running (pidfile=%s), exiting", _pidfile())
        sys.exit(1)


def _remove_pidfile() -> None:
    remove_pidfile(_pidfile())


def _is_running() -> bool:
    """Whether a daemon is already running (via its pidfile).

    Pid-reuse-safe: a live pid whose argv does not name this daemon's module
    is a recycled pid, not a running instance (audit round 2, P1)."""
    return pidfile_holds_daemon(_pidfile(), "services.entrypoints.im_bridge.daemon")


# -- composition root --------------------------------------------------------
# This module is the only one of the package that reads `settings`
# (scripts/structure/ambient_state: SLICED_PACKAGES). It builds the configuration
# slices from the flat field values and hands each to the component that uses it.


def im_bridge_config() -> ImBridgeConfig:
    return ImBridgeConfig(
        im_disabled_adapters=tuple(settings.services.im_disabled_adapters),
        im_send_retry_delays=tuple(settings.services.im_send_retry_delays),
        im_push_retry_backoff_seconds=settings.services.im_push_retry_backoff_seconds,
        im_push_retry_jitter_seconds=settings.services.im_push_retry_jitter_seconds,
        im_sse_read_timeout_seconds=settings.services.im_sse_read_timeout_seconds,
        im_bridge_timeline_window=settings.services.im_bridge_timeline_window,
        im_bridge_replay_messages=settings.services.im_bridge_replay_messages,
        im_bridge_notice_reply_window_seconds=settings.services.im_bridge_notice_reply_window_seconds,
        im_bridge_notice_open_limit=settings.services.im_bridge_notice_open_limit,
        notices_open_default_limit=settings.display.notices_open_default_limit,
    )


def telegram_config() -> TelegramCredentialsConfig:
    return TelegramCredentialsConfig(
        telegram_bot_token=settings.telegram.telegram_bot_token,
        telegram_owner_id=settings.telegram.telegram_owner_id,
        telegram_poll_timeout_seconds=settings.telegram.telegram_poll_timeout_seconds,
        telegram_reconnect_base_delay_seconds=settings.telegram.telegram_reconnect_base_delay_seconds,
        telegram_reconnect_max_delay_seconds=settings.telegram.telegram_reconnect_max_delay_seconds,
    )


def feishu_config() -> FeishuCredentialsConfig:
    return FeishuCredentialsConfig(
        feishu_app_id=settings.feishu.feishu_app_id,
        feishu_app_secret=settings.feishu.feishu_app_secret,
        feishu_rest_timeout_seconds=settings.feishu.feishu_rest_timeout_seconds,
        feishu_poll_interval_seconds=settings.feishu.feishu_poll_interval_seconds,
        feishu_poll_chat_id=settings.feishu.feishu_poll_chat_id,
        delivery_watchdog_stale_claimed_threshold_seconds=(
            settings.daemon.delivery_watchdog_stale_claimed_threshold_seconds
        ),
    )


def gateway_client(config: ImBridgeConfig) -> GatewayClient:
    return GatewayClient(
        config,
        gateway_url=settings.gateway.gateway_url,
        auth_headers=gateway_auth_headers(),
    )


# The adapters the daemon runs, in load order, each with the builder of the slice
# its constructor takes (None: the adapter reads no configuration slice).
_ADAPTERS: tuple[tuple[str, Callable[[], Any] | None], ...] = (
    ("telegram", telegram_config),
    ("weixin", None),
    ("feishu", feishu_config),
)


def _import_adapter(name: str) -> Any:
    """Import one adapter module by channel name (seam for tests)."""

    return __import__(f"services.entrypoints.im_bridge.adapters.{name}", fromlist=["*"])


def _load_adapters(core: Any, disabled: frozenset[str]) -> list[Any]:
    """Import each channel adapter; a missing module or failed import logs and
    skips — one broken channel must not take down the bridge."""

    loaded: list[Any] = []
    for name, build_config in _ADAPTERS:
        if name in disabled:
            _log.info("im_bridge: adapter %s disabled by config (AVA_IM_DISABLED_ADAPTERS)", name)
            continue
        try:
            mod = _import_adapter(name)
            adapter_cls = mod.ADAPTER_CLASS
            adapter = adapter_cls(core, *(() if build_config is None else (build_config(),)))
            core.register(adapter)
            loaded.append(adapter)
            _log.info("im_bridge: adapter %s loaded", name)
        except ImportError as exc:
            _log.warning("im_bridge: adapter %s unavailable (skipped): %r", name, exc)
        except Exception:
            _log.exception("im_bridge: adapter %s failed to load (skipped)", name)
    return loaded


async def _contained(work: Awaitable[None], what: str) -> None:
    """Run one daemon-long loop so a failure ends that loop alone, logged, never the daemon."""
    try:
        await work
    except Exception:
        _log.exception("im_bridge: %s failed", what)


async def _liveness_loop(liveness: Liveness) -> None:
    """Beat the healthz liveness while the daemon serves.

    The main loop's work happens in adapter background tasks and the final
    ``asyncio.Event().wait()`` never iterates, so this task is the heartbeat:
    a wedged event loop stops scheduling it and the staleness ceiling trips.
    """

    # quiesce-exempt: beats the health liveness; no database
    while True:
        liveness.beat()
        await asyncio.sleep(_LIVENESS_BEAT_INTERVAL_S)


async def _notice_loop(core: Any) -> None:
    """Poll the gateway for new fleet notices and push them to the owner
    chat (Task #884). Cursor + filter persist across restarts."""

    while True:
        if not admission.quiesced():
            await core.notice_bridge.poll_once()
        await asyncio.sleep(3.0)


async def _timeline_outbound_loop(core: Any) -> None:
    """One service-owned dispatcher and periodic committed-tail wakeup."""
    core.timeline_worker.validate_pool()
    while True:
        if not admission.quiesced():
            try:
                await core.poll_timeline_outbound()
            except Exception as exc:
                _log.warning("im_bridge: outbound round failed class=%s", type(exc).__name__)
        await asyncio.sleep(3.0)


async def _handle_send(core: Any) -> Any:
    """Route handler for the daemon's ``POST /send`` RPC (ops-alerts fan-out).

    Body: ``{"text": str}`` — fanned out to every loaded adapter's owner chat
    via ``core.notify_user``. The gateway calls this with the cluster secret
    as Bearer (the health server's ``auth_digests``). Returns per-channel
    results; a channel that failed to send is reported, not fatal. When
    EVERY channel failed (or none is loaded) the route answers 502 instead
    of 200 — the caller (base/telemetry/alerts/__init__.py) keys ``notified_at`` off the status
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
            # treat the fan-out as delivered (base/telemetry/alerts/__init__.py keeps notified_at NULL
            # and retries on the next Grafana re-send).
            return 502, json.dumps({"results": results}).encode(), "application/json"
        return 200, json.dumps({"results": results}).encode(), "application/json"

    return handle


async def run() -> None:
    """Start the daemon: healthz -> pidfile -> load adapters -> serve."""
    if _is_running():
        _log.info("[im_bridge] daemon already running (pidfile=%s), exiting", _pidfile())
        sys.exit(1)

    _write_pidfile()
    _log.info("[im_bridge] pidfile written: %s", _pidfile())

    # The notice bridge reads agent_notices directly (R3 door ④ — decoupled
    # from gateway availability, so a paused cluster cannot stall notice
    # delivery); the pool is created here and owned by the daemon.
    from services.entrypoints.im_bridge.core import IMBridgeCore

    db_pool = Database.from_settings().pool()
    config = im_bridge_config()
    core = IMBridgeCore(config, gateway_client(config), db_pool=db_pool)
    liveness = Liveness(_LIVENESS_TIMEOUT_S)
    endpoint = _endpoint()
    try:
        health = await start_health_server(
            "im_bridge",
            endpoint.health_port,
            liveness=liveness,
            extra_routes={("POST", "/send"): await _handle_send(core)},
            # Bearer = a machine API token of the write generation (the gateway's, or this
            # unit's); an open cluster (no secret) gets no auth — consistent with the gateway.
            auth_digests=daemon_acceptance(),
        )
    except Exception:
        _remove_pidfile()
        raise
    _log.info("[im_bridge] healthz listening on :%s", endpoint.health_port)

    adapters = _load_adapters(core, frozenset(config.im_disabled_adapters))
    if not adapters:
        _log.warning("im_bridge: no adapters loaded — nothing to serve")

    try:
        # Drain anything the previous process outboxed when the gateway was
        # down (Task #1032: user messages must not drop across restarts).
        core.ensure_outbox_replay()
        # Rebuild SSE push subscriptions from persisted switch state — they
        # are memory-only and a restart drops them (Task #804: agent replies
        # silently stopped reaching Telegram after the 04:32 restart).
        await core.restore_subscriptions()
        # One TaskGroup owns the heartbeat and the notice poll; leaving the
        # block (SIGTERM/SIGINT cancels it) cancels both before the finally.
        async with asyncio.TaskGroup() as loops:
            loops.create_task(_contained(_liveness_loop(liveness), "liveness loop"))
            loops.create_task(_contained(_notice_loop(core), "notice loop"))
            await asyncio.gather(*(a.start() for a in adapters))
            if adapters:
                core.timeline_worker.validate_pool()
                loops.create_task(_timeline_outbound_loop(core))
            # Every adapter's start() returns once its connection loop is launched
            # (long polls / ws threads run in the background). The daemon now stays
            # alive forever; SIGTERM/SIGINT unwinds through the finally below.
            await asyncio.Event().wait()
    finally:
        for a in adapters:
            try:
                await a.stop()
            except Exception:
                _log.warning(
                    "[im_bridge] adapter %s failed to stop cleanly", type(a).__name__, exc_info=True
                )
        await stop_health_server(health)
        db_pool.close()
        _remove_pidfile()
        _log.info("[im_bridge] daemon stopped")


def _gate_httpx_info_logs() -> None:
    """Gate httpx's per-request INFO lines in this process.

    httpx logs ``HTTP Request: GET <url>`` at INFO, and the Telegram Bot API
    carries the bot token inside the URL path (``.../bot<id>:<token>/...``):
    every long poll therefore persisted the live token into this daemon's
    structured log in cleartext (task #4067). Raising this one third-party
    logger to WARNING keeps genuine library warnings/errors while dropping
    the URL-bearing INFO lines. The adapters separately keep httpx
    *exceptions* (which can embed the same URL) out of their own log calls.
    """
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> None:
    init_gateway_process("im_bridge")
    _gate_httpx_info_logs()
    install_graceful_shutdown("im_bridge")
    code = 0
    # `asyncio.Runner`, not `asyncio.run`: `run` closes in a `finally` that
    # awaits `shutdown_default_executor`, joining the default executor's
    # workers — adapters hand blocking REST calls to it via `asyncio.to_thread`
    # — and a stop signal must never wait on those (see `_hard_exit`). The
    # runner is therefore never closed: after the explicit drain below,
    # teardown is skipped by the hard exit.
    runner = asyncio.Runner()
    try:
        runner.run(run())
    except KeyboardInterrupt:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # a retry must not abort the bounded exit
        _log.info("[im_bridge] interrupted")
        # The signal path skips Runner's own cancellation, so drain the loop's
        # tasks explicitly: `run`'s finally still stops the adapters, the
        # liveness/notice tasks and the health server, and closes the DB pool.
        # The executor is deliberately NOT drained.
        failures = cancel_and_drain(runner)
        if failures:
            _log.error("[im_bridge] async shutdown failed: %r", failures)
            code = 1
    except Exception:
        _log.exception("[im_bridge] daemon crashed — uncaught exception escaped run()")
        code = 1
    _hard_exit(code)


if __name__ == "__main__":
    main()
