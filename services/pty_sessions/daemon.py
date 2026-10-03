"""pty-sessions daemon entry: ``python -m services.pty_sessions.daemon``.

Takes the home's instance lock (a second start exits at once), sweeps what a
previous service left running (`ledger.sweep`), serves the unix socket until
SIGTERM, then closes the sessions still alive.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import resource
import sys
from pathlib import Path

from base.log import logger
from base.log.sinks import add_sink
from base.native_process.os_platform import LockTimeoutError, file_lock
from base.sessions.pty.paths import fallback_dir, ledger_path, lock_path, service_socket_path
from services.pty_sessions import ledger, shutdown_budget
from services.pty_sessions.service import PtyService

# The macOS launchd default soft limit (256) holds a hundred sessions at two
# descriptors each; ask for the hard limit, capped at what every platform accepts.
_NOFILE_TARGET = 10240


def _raise_nofile_limit() -> None:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = _NOFILE_TARGET if hard == resource.RLIM_INFINITY else min(hard, _NOFILE_TARGET)
    if soft < wanted:
        with contextlib.suppress(ValueError, OSError):
            resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))


def _own_fallback_dir(directory: Path) -> bool:
    """Create the owner-only directory a too-long socket path lives in; False when it is not ours."""
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        logger.error("{directory} is not an owner-only directory of this user", directory=directory)
        return False
    return True


def main() -> int:
    add_sink(
        sys.stderr,
        format="{time:HH:mm:ss.SSS} <level>{level: <5}</level> {message}",
        level="INFO",
        colorize=False,
    )
    _raise_nofile_limit()
    try:
        # One service per home, for as long as this process lives: a second start must
        # neither sweep the live service's sessions nor take over its socket.
        with file_lock(lock_path(), timeout_s=0):
            return _run()
    except LockTimeoutError:
        logger.error("another pty-sessions service holds {lock}", lock=lock_path())
        return 1


def _run() -> int:
    service = PtyService()
    service.swept = ledger.sweep(ledger_path())
    path = service_socket_path()
    if path.parent == fallback_dir() and not _own_fallback_dir(path.parent):
        return 1
    asyncio.run(
        service.serve(
            path,
            hangup_wait_s=shutdown_budget.SHUTDOWN_HANGUP_WAIT_S,
            kill_s=shutdown_budget.SHUTDOWN_KILL_S,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
