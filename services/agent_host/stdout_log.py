"""Raw stdout transcript size rotation for the agent-host daemon.

Split out of `daemon.py` so that file stays inside the per-file line budget
(task #4224); the mechanics and bounds are unchanged.

The launcher redirects the daemon's fd 1/2 straight at
`$AVA_HOME/logs/ava-agent-host.out.log` — no pty transcript cap, no loguru
rotation, nothing owns the file but the process itself — so a crash storm
that floods the transcript with tracebacks can balloon it to a disk-filling
size (2026-09-03: 13.5 GB in ~6 minutes, task #2356). The daemon re-points
its own fds once the file crosses a ceiling; see `_rotate_stdout_log_if_needed`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path

from shared import paths
from shared.log import logger

# Same logger name as `daemon.py` uses: splitting the code out must not change
# log attribution or routing.
_log = logging.getLogger("services.agent_host.daemon")

_STDOUT_LOG_ROTATE_BYTES = 1 << 30  # 1 GiB — rotate the raw transcript past this
_STDOUT_LOG_ROTATE_POLL_S = 60.0  # cadence of the size check
_STDOUT_LOG_NAME = "ava-agent-host"  # names $AVA_HOME/logs/<name>.out.log


def _stdout_log_path() -> Path:
    """The daemon's raw stdout/stderr transcript.

    The launcher opens this file and redirects fd 1/2 onto it before the
    daemon starts (`$AVA_HOME/logs/<name>.out.log`, the posix-session
    convention); nothing in the logging stack owns it, which is exactly why it
    needs the size rotation below.
    """
    return paths.logs_dir() / f"{_STDOUT_LOG_NAME}.out.log"


def _rotate_stdout_log_files(log_path: Path) -> None:
    """Roll `log_path` over to `log_path.1`, keeping one rotated generation.

    The previous `.1` chunk is atomically replaced (a rename over it), and a
    stray `.2` — left by an older generation scheme or a manual ops roll — is
    dropped. Two generations on disk total: the live file plus the last
    rotated chunk. Replacement rather than a `.1 -> .2` shift is what bounds a
    crash storm: every rotation swaps the previous chunk instead of stacking
    generations, so a flood that would otherwise fill the disk keeps at most
    `ceiling + one poll overshoot` per file.
    """
    one = log_path.with_name(log_path.name + ".1")
    two = log_path.with_name(log_path.name + ".2")
    with contextlib.suppress(OSError):
        two.unlink()
    log_path.replace(one)


def _rotate_stdout_log_if_needed() -> int | None:
    """Rotate the raw transcript once fd 1's file crosses the ceiling.

    Returns the size the file had reached when it was rotated, or None when no
    rotation happened. The rename runs first and fd 1/2 are then dup2'ed onto a
    freshly opened file at the original path, so every later write — `os.write`
    and file objects bound to fd 1/2 alike — lands in the new file; the window
    between the two steps is at most one line, which is logged and accepted. A
    safe no-op when fd 1 is not a regular file (a pipe or tty reports size 0)
    or when the path has gone away.
    """
    try:
        size = os.fstat(1).st_size
    except OSError:
        return None
    if size < _STDOUT_LOG_ROTATE_BYTES:
        return None
    log_path = _stdout_log_path()
    rotated = False
    try:
        _rotate_stdout_log_files(log_path)
        rotated = True
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    except OSError:
        if rotated:
            # fd 1/2 still name the moved chunk; put it back under the
            # original path so the next pass can retry — without this a
            # failed open (disk full / EMFILE) strands the transcript under
            # `.1` forever, growing past every bound and never self-healing.
            with contextlib.suppress(OSError):
                one = log_path.with_name(log_path.name + ".1")
                one.replace(log_path)
        _log.exception(
            "[agent-host] stdout log rotation failed — fd 1 still points at the old file"
        )
        return None
    try:
        os.dup2(fd, 1)
        os.dup2(fd, 2)
    finally:
        if fd > 2:
            os.close(fd)
    return size


async def _rotate_stdout_log_forever() -> None:
    """Bound the raw stdout transcript by size for the daemon's whole life.

    The first pass runs immediately so a file that already crossed the ceiling
    (a storm before this code shipped, or between a crash and the watchdog's
    respawn) is absorbed at boot; afterwards the size is checked on a fixed
    cadence. Self-protecting like the other daemon loops: a failed pass is
    logged and the loop waits for the next interval.
    """
    _rotate_stdout_log_if_needed()
    while True:
        await asyncio.sleep(_STDOUT_LOG_ROTATE_POLL_S)
        try:
            rotated_at = _rotate_stdout_log_if_needed()
        except Exception:
            _log.exception("[agent-host] stdout log rotation pass failed — retrying next interval")
            continue
        if rotated_at is not None:
            logger.info(
                "[agent-host] rotated the raw stdout transcript at {size} bytes "
                "(ceiling {ceiling})",
                event="host_stdout_log_rotated",
                size=rotated_at,
                ceiling=_STDOUT_LOG_ROTATE_BYTES,
            )
