"""Route service stop signals through cleanup, then leave without thread joins.

Normal stop requests SIGTERM, which becomes KeyboardInterrupt, matching the
daemon's existing asyncio.run()/finally cleanup. Normal stop waits for actual
completion and reports an incomplete stop on timeout; only an explicit force
request interrupts the remaining resources.

Launchers exec into the daemon before direct SIGTERM delivery. SIGINT keeps
Python's existing handler.

Where present, the repeated-SIGTERM guard stays at each daemon's call site.
The daemons with an explicit drain cancel and await remaining loop tasks so
``run()``'s cleanup can finish, then call ``base.host.exit_codes.hard_exit``, which
leaves without joining threads (see its docstring for why the runner is not closed).

The in-flight work varies: agent-host recorders, delivery recovery, Feishu
calls, heartbeat grading, page reconciliation, memory search loading, indexer
embedding, event maintenance, and watchdog checks all use threads. Labeler can
use the default executor for DNS even though it declares no long executor job;
backup's child-process dump has no long executor job. Their ``run()`` cleanup
handles owned resources before exit; unfinished reconciliation is re-derived on
restart and indexer failures stay dirty on disk.
"""

from __future__ import annotations

import asyncio
import signal
import types

from base.log import logger


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


def install_graceful_shutdown(label: str) -> None:
    """Route SIGTERM into the daemon's cleanup path.

    Call once from a daemon's `main()`, before its loop starts. `label` names
    the daemon in the shutdown log line (e.g. ``"labeler"``).

    SIGINT is left on Python's default handler, which already raises
    ``KeyboardInterrupt``.
    """

    def _handler(signum: int, _frame: types.FrameType | None) -> None:
        logger.info(
            "[{service}] received {sig}, shutting down",
            service=label,
            sig=signal.Signals(signum).name,
        )
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _handler)
