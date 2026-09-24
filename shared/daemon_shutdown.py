"""Route service stop signals through cleanup, then leave without thread joins.

Normal pause/stop requests SIGTERM on POSIX and Ctrl-Break in each Windows
service's verified private console. Both become KeyboardInterrupt, matching
the daemon's existing asyncio.run()/finally cleanup. Normal stop waits for
actual completion and reports an incomplete stop on timeout; only an explicit
force request interrupts the remaining resources.

POSIX launchers exec into the daemon before direct SIGTERM delivery. Windows
delivery reaches the interpreter through its verified private console, whose
recorded root may still be a launcher. SIGINT keeps Python's existing handler.

Where present, the repeated-SIGTERM guard stays at each daemon's call site.
The daemons with an explicit drain cancel and await remaining loop tasks so
``run()``'s cleanup can finish, then call ``hard_exit``. They do
not close ``asyncio.Runner``: its default-executor shutdown can wait up to
CPython's 300-second ``THREAD_JOIN_TIMEOUT``, already the stop flow's entire
budget, and interpreter teardown can join surviving workers without a bound.
Even ``shutdown(wait=False)`` does not prevent that atexit join. The uploader's
own pool and the ops pool have the same risk. A hard exit skips only teardown
after daemon-owned cleanup; it also skips the telemetry emitter's atexit drain
(accepted for this path in task #4320). Explicitly flush logs first because
their atexit cleanup is skipped too.

The in-flight work varies: agent-host recorders, delivery recovery, Feishu
calls, heartbeat grading, page reconciliation, memory search loading, indexer
embedding, event maintenance, watchdog checks, and PITR retention all use
threads. Labeler can use the default executor for DNS even though it declares
no long executor job; backup's child-process dump has no long executor job.
The uploader can block on a GCS upload. Their ``run()`` cleanup handles owned
resources before exit; unfinished reconciliation is re-derived on restart,
indexer failures stay dirty on disk, and PITR deletions are journalled.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
import types
from typing import NoReturn

from shared.log import logger


def cancel_and_drain(runner: asyncio.Runner) -> list[Exception]:
    """Cancel loop tasks and return their ordinary failures after cleanup.

    ``return_exceptions=True`` lets every task's ``finally`` run. Keep the
    exception objects and their gather order for each daemon's existing log;
    ``CancelledError`` is a BaseException and is deliberately not a failure.
    Errors from obtaining or draining the loop still propagate to the caller.
    The default executor is deliberately not drained.
    """
    loop = runner.get_loop()
    tasks = asyncio.all_tasks(loop)
    for task in tasks:
        task.cancel()
    results = loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
    return [result for result in results if isinstance(result, Exception)]


def hard_exit(code: int) -> NoReturn:
    """Flush logs and exit immediately after daemon-owned async cleanup."""
    with contextlib.suppress(Exception):
        from loguru import logger as _loguru

        _loguru.remove()  # closes (and so flushes) every sink
    with contextlib.suppress(Exception):
        logging.shutdown()
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.flush()
    os._exit(code)


def install_graceful_shutdown(label: str) -> None:
    """Route SIGTERM and Windows SIGBREAK into the daemon's cleanup path.

    Call once from a daemon's `main()`, before its loop starts. `label` names
    the daemon in the shutdown log line (e.g. ``"labeler"``).

    SIGINT is left on Python's default handler, which already raises
    ``KeyboardInterrupt``. Windows Ctrl-Break arrives as SIGBREAK, which needs
    an explicit handler to run the same cleanup instead of exiting abruptly.
    """

    def _handler(signum: int, _frame: types.FrameType | None) -> None:
        # `service=`, not `label=`: `shared.log._message_to_params` treats an
        # `extra["label"]` as an event alias, and a daemon name is no registered
        # event — every stop logged "unregistered event_name='pg-backup'" (and
        # its siblings) from inside the loguru handler, losing the row
        # (task #3661 side fix).
        logger.info(
            "[{service}] received {sig}, shutting down",
            service=label,
            sig=signal.Signals(signum).name,
        )
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _handler)
    if sys.platform == "win32":
        signal.signal(signal.SIGBREAK, _handler)
