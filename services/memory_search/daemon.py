"""Memory search service daemon — uvicorn over the MemoryStore on 19531.

`ava start` spawns this session right after milvus (the indexer's
cold-start connects to whichever backend `AVA_MEMORY_SEARCH_BACKEND`
names, so the storage service must be up first). The store loads its npz
at boot; the indexer daemon then reconciles disk against it, so a fresh
backend needs no hand-copied data.

Usage:
    .venv/bin/python -m services.memory_search.daemon

Kept alive by the watchdog via `services.memory_search.healthcheck`
(a real POST /search probe, not a bare TCP connect).
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

import uvicorn

from services._pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile
from services.memory_indexer.embeddings.factory import get_provider
from services.memory_search.app import build_app
from services.memory_search.store import MemoryStore
from shared.config import settings
from shared.daemon_shutdown import cancel_and_drain, install_graceful_shutdown
from shared.daemon_shutdown import hard_exit as _hard_exit
from shared.log import init_gateway_process

_PIDFILE = settings.services.memory_search_pidfile
_DATA_FILE = settings.services.memory_search_data_dir / "vectors.npz"
_PORT = settings.services.memory_search_port

_log = logging.getLogger("services.memory_search.daemon")


def _is_running() -> bool:
    return pidfile_holds_daemon(_PIDFILE, "services.memory_search.daemon")


async def run() -> None:
    """Load the store, then serve until the graceful-shutdown signal fires.

    The provider config (`AVA_EMBEDDING_BACKEND`) is read at boot: the
    store's matrix width and wire bound must match the provider that
    produced the vectors, and an unknown provider value fails fast instead
    of serving a half-mismatched search surface."""
    provider = get_provider()
    store = MemoryStore(_DATA_FILE, dim=provider.dim, fingerprint=provider.fingerprint)
    await asyncio.to_thread(store.load)
    server = uvicorn.Server(
        uvicorn.Config(
            build_app(store),
            host="127.0.0.1",
            port=_PORT,
            log_level="warning",
            access_log=False,
            log_config=None,
        )
    )
    await server.serve()


def main() -> None:
    """Entry point: pidfile -> log init -> serve -> cleanup."""
    if _is_running():
        sys.exit(1)
    if not acquire_pidfile(_PIDFILE, "services.memory_search.daemon"):
        sys.exit(1)
    init_gateway_process(name="memory_search")
    install_graceful_shutdown("memory_search")
    code = 0
    # `asyncio.Runner`, not `asyncio.run`: `run` closes in a `finally` that
    # awaits `shutdown_default_executor`, joining the default executor's
    # workers — the store load among them — and a stop signal must never wait
    # on those (see `_hard_exit`). The runner is therefore never closed: after
    # the explicit drain below, teardown is skipped by the hard exit. During
    # serving, SIGTERM first drives uvicorn's own graceful stop; on the way
    # out `capture_signals` restores this daemon's handler and re-raises the
    # signal (uvicorn/server.py, 0.52.4), so it still lands in this
    # KeyboardInterrupt branch — not a normal return from `run()`.
    runner = asyncio.Runner()
    try:
        runner.run(run())
    except KeyboardInterrupt:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # a retry must not abort the bounded exit
        _log.info("[memory-search] interrupted, shutting down")
        # The signal path skips Runner's own cancellation, so drain the loop's
        # tasks explicitly: uvicorn's serve coroutine unwinds through the
        # re-raised signal; the pidfile is removed by the finally below either
        # way. The executor is deliberately NOT drained.
        failures = cancel_and_drain(runner)
        if failures:
            _log.error("[memory-search] async shutdown failed: %r", failures)
            code = 1
    except Exception:
        _log.exception("[memory-search] daemon crashed — uncaught exception escaped run()")
        code = 1
    finally:
        remove_pidfile(_PIDFILE)
    _hard_exit(code)


if __name__ == "__main__":
    main()
