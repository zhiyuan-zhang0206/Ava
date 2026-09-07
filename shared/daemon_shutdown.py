"""Route normal service stop signals through the daemon's cleanup path.

Normal pause/stop requests SIGTERM on POSIX and Ctrl-Break in each Windows
service's verified private console. Both become KeyboardInterrupt, matching
the daemon's existing asyncio.run()/finally cleanup. Normal stop waits for
actual completion and reports an incomplete stop on timeout; only an explicit
force request interrupts the remaining resources.

POSIX launchers exec into the daemon before direct SIGTERM delivery. Windows
delivery reaches the interpreter through its verified private console, whose
recorded root may still be a launcher. SIGINT keeps Python's existing handler.
"""

from __future__ import annotations

import signal
import sys
import types

from shared.log import logger


def install_graceful_shutdown(label: str) -> None:
    """Route SIGTERM and Windows SIGBREAK into the daemon's cleanup path.

    Call once from a daemon's `main()`, before its loop starts. `label` names
    the daemon in the shutdown log line (e.g. ``"labeler"``).

    SIGINT is left on Python's default handler, which already raises
    ``KeyboardInterrupt``. Windows Ctrl-Break arrives as SIGBREAK, which needs
    an explicit handler to run the same cleanup instead of exiting abruptly.
    """

    def _handler(signum: int, _frame: types.FrameType | None) -> None:
        logger.info(
            "[{label}] received {sig}, shutting down", label=label, sig=signal.Signals(signum).name
        )
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _handler)
    if sys.platform == "win32":
        signal.signal(signal.SIGBREAK, _handler)
