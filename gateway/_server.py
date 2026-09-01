"""Gateway process entry point, kept separate from ASGI application wiring."""

from __future__ import annotations

import atexit
import faulthandler
import logging
import os
import signal
from contextlib import suppress

import uvicorn

from shared.config import settings
from shared.log import init_gateway_process
from shared.machine import is_gateway
from shared.migrations import assert_schema_current
from shared.platform import raise_fd_limit
from shared.transport_encryption import verify_transport_encryption

_log = logging.getLogger(__name__)
_GATEWAY_UVICORN_WORKERS = 1


def main() -> None:
    """Prepare the gateway process and start its ASGI server."""
    assert_schema_current(settings.data_plane.db_url)

    # pidfile — `services/healthchecks/gateway.py` uses it to probe.
    # SIGKILL does not trigger atexit; the healthcheck uses kill -0 to
    # judge liveness, so a stale pidfile is not fatal (kill -0 fail -> restart).
    pidfile = settings.services.gateway_pidfile
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(str(os.getpid()))

    def _cleanup_pidfile() -> None:
        with suppress(OSError):
            pidfile.unlink(missing_ok=True)

    atexit.register(_cleanup_pidfile)

    # Raise the fd limit: it covers the gateway's own SSE long connections +
    # Redis pubsub + HTTP keepalive, which on a long run would otherwise blow
    # the launchd 256 default (errno 24 -> requests connection-reset, surfaced
    # in the frontend as "Failed to fetch"), and the session children it spawns
    # inherit the raised ceiling.
    raise_fd_limit(65536)

    init_gateway_process()

    # Thread dump on SIGUSR1: the watchdog's gateway healthcheck sends this
    # before respawning a frozen gateway, so a stall lands a stack trace in
    # the pane log instead of a silent black box (2026-08-03: 13 freezes in
    # 8h, none left a trace between the last log line and the kill). uvicorn
    # does not touch SIGUSR1, so the registration survives into the loop.
    faulthandler.register(signal.SIGUSR1)

    # Bind address depends on role.
    #
    # - **gateway**: "" = all interfaces, BOTH address families (asyncio binds
    #   a wildcard socket per family for an empty host). Dual-stack matters
    #   because browsers resolving the host's DNS name try the AAAA first — a
    #   v4-only bind refuses that first dial on every request. NOT "::": asyncio
    #   sets IPV6_V6ONLY on an explicit "::" bind, which refuses plain-IPv4
    #   clients (healthchecks, SDK) outright — verified on macOS.
    #   A no-secret gateway binds 127.0.0.1 instead: its API is unauthenticated,
    #   and the no-secret posture is single-box (the data plane is loopback-only
    #   too — `_bind_addrs`), so an all-interfaces bind would expose the
    #   unauthenticated API to the LAN.
    # - **agent-runner**: 127.0.0.1. The gateway does not reach an
    #   agent-runner's gateway directly — gateway→agent-runner RPC goes
    #   to the separate ava-ops server (services/agent_ops), which dispatches
    #   each op in-process via gateway.ops_*. The only callers of an
    #   agent-runner's gateway :8000 are local SDK + local agent processes,
    #   so bind 127.0.0.1.
    #
    # reload defaults to False — prod-safe. reload=True forks workers via
    # multiprocessing.spawn, and the worker's `PPID=1` is fully detached
    # from the session: when the session closes the worker does
    # not die, leaving a zombie holding :8000; the next graceful kill on
    # ava cluster update cannot catch it, and the new gateway boot gets
    # [Errno 48] Address already in use. For dev hot-reload, set
    # AVA_GATEWAY_RELOAD=1 (usually in a dev clone's .env or shell). Reload
    # mode binds through uvicorn's own bind_socket, which maps "" to a
    # v4-only wildcard — dev-only, and browsers fall back from the refused
    # IPv6 dial instantly.
    host = "" if is_gateway() and settings.data_plane.cluster_secret else "127.0.0.1"
    if host != "127.0.0.1":
        verify_transport_encryption(settings.data_plane.cluster_secret, host)
    reload = settings.gateway.gateway_reload
    if _GATEWAY_UVICORN_WORKERS != 1:
        raise RuntimeError(
            "gateway must run one uvicorn worker because rate limiters are process-local"
        )
    _log.warning("gateway starts with one uvicorn worker because rate limiters are process-local")
    # log_config=None: uvicorn's default LOGGING_CONFIG dictConfig would
    # clobber the root-handler install (`_StdlibInterceptHandler`) that
    # init_gateway_process set up above, sending uvicorn's own records
    # (ASGI tracebacks, startup/shutdown) to a bare stderr handler instead
    # of through loguru → gateway.log + the events pipeline. With None,
    # uvicorn leaves the logging system alone; uvicorn.error propagates to
    # the root intercept handler, and uvicorn.access is gated to WARNING in
    # `_install_stdlib_intercept` (per-request INFO is noise). #970: an
    # unhandled ASGI exception used to land only in the session log and die
    # with the session — now it reaches gateway.log and the events table.
    uvicorn.run(
        "gateway.app:app",
        host=host,
        port=settings.gateway.gateway_port,
        reload=reload,
        reload_dirs=["gateway", "shared", "ava", "agent"] if reload else None,
        log_config=None,
        workers=_GATEWAY_UVICORN_WORKERS,
    )
