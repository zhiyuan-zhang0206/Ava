"""Graceful-stop signal handling for service daemons.

The stop path (`ava stop`, and the update's graceful leg) asks a daemon to end
by sending SIGTERM to its supervised pid and waiting before escalating to
SIGKILL. Python's default disposition for SIGTERM kills the process outright —
fast, but no `finally` runs, so a daemon that holds a pidfile, a DB pool or a
child process tears down exactly as abruptly as under the SIGKILL it was meant
to avoid.

`install_graceful_shutdown` turns SIGTERM into the interrupt the daemons are
already written against: every one of them runs `asyncio.run(run())` under
`except KeyboardInterrupt`, the shape Ctrl-C produces. Raising
``KeyboardInterrupt`` from the handler unwinds the loop through those same
`finally` blocks, so a supervisor's SIGTERM and an operator's Ctrl-C end the
process by one path with one set of cleanup.

This only matters because the signal now arrives at all: a service session's
login shell `exec`s into the daemon (`shared.session_env.exec_into`), so the pid
the supervisor records and signals IS this process. While a wrapper shell sat in
between, no daemon ever observed SIGTERM and every graceful stop ran to its full
timeout and hard-killed instead.
"""

from __future__ import annotations

import signal
import types

from shared.log import logger


def install_graceful_shutdown(label: str) -> None:
    """Route SIGTERM into the daemon's `KeyboardInterrupt` shutdown path.

    Call once from a daemon's `main()`, before its loop starts. `label` names
    the daemon in the shutdown log line (e.g. ``"labeler"``).

    SIGINT is left on Python's default handler, which already raises
    ``KeyboardInterrupt`` — the two signals converge on the same unwind.
    """

    def _handler(signum: int, _frame: types.FrameType | None) -> None:
        logger.info(
            "[{label}] received {sig}, shutting down", label=label, sig=signal.Signals(signum).name
        )
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _handler)
