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
import contextlib
import logging
import os
import signal
import sys
from typing import NoReturn

import uvicorn

from services._pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile
from services.memory_indexer.embeddings.factory import get_provider
from services.memory_search.app import build_app
from services.memory_search.store import MemoryStore
from shared.config import settings
from shared.daemon_shutdown import install_graceful_shutdown
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


def _hard_exit(code: int) -> NoReturn:
    """End the process now, skipping interpreter teardown. Never returns.

    Teardown is precisely what hangs: the store load runs on the default
    executor (``asyncio.to_thread``) — an npz load with no small bound, exactly
    what a stop can land on during a slow boot — and ``asyncio.Runner.close``
    joins that executor behind CPython's ``THREAD_JOIN_TIMEOUT`` cap (300 s),
    the stop flow's entire budget (`PAUSE_TIMEOUT_SECONDS`); a load still in
    flight at SIGTERM then keeps interpreter teardown waiting with no bound at
    all (measured: a ``shutdown(wait=False)`` worker is still joined at exit,
    task #3940). Nothing after ``run()`` needs it — the pidfile is removed
    below and a half-loaded store dies with the process. Logs are flushed
    first: they are the one thing a skipped teardown would lose. Same shape as
    services/agent_ops/daemon.py and services/pitr/uploader_daemon.py.
    """
    with contextlib.suppress(Exception):
        from loguru import logger as _loguru

        _loguru.remove()  # closes (and so flushes) every sink
    with contextlib.suppress(Exception):
        logging.shutdown()
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.flush()
    os._exit(code)


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
    # the explicit drain below, teardown is skipped by the hard exit. While
    # uvicorn is serving, SIGTERM is uvicorn's own graceful exit and `run()`
    # returns normally into the same hard exit.
    runner = asyncio.Runner()
    try:
        runner.run(run())
    except KeyboardInterrupt:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # a retry must not abort the bounded exit
        _log.info("[memory-search] interrupted, shutting down")
        # The signal path skips Runner's own cancellation, so drain the loop's
        # tasks explicitly: uvicorn's serve task unwinds; the pidfile is
        # removed by the finally below either way. The executor is
        # deliberately NOT drained.
        loop = runner.get_loop()
        tasks = asyncio.all_tasks(loop)
        for task in tasks:
            task.cancel()
        results = loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        failures = [result for result in results if isinstance(result, Exception)]
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
