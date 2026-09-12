"""Single-instance ownership of one root tree (I1 invariant).

A root supervisor owns exactly one tree per (machine x home); the instance
lock makes a second launch refuse instead of racing the first. The lock is a
`flock` on `<run_dir>/ava-root.lock`: the kernel ties it to the owning file
description, so it is released automatically when the owning process dies —
no stale-lock reclaim loop and no pid-identity heuristics.

The lock fd is opened O_CLOEXEC, and units are spawned with `close_fds=True`
elsewhere in this package, so a unit process can never inherit — and keep
alive — the tree's lock.

The POSIX mechanism (`fcntl.flock`) is what ships first; a platform-native
equivalent (a named mutex) plugs in behind `acquire_instance_lock` when the
platform adapter work lands.
"""

from __future__ import annotations

import fcntl
import logging
import os
from pathlib import Path

_LOCK_NAME = "ava-root.lock"

_log = logging.getLogger(__name__)


class AlreadyRunningError(RuntimeError):
    """Another root supervisor already owns this run directory."""


def acquire_instance_lock(run_dir: Path) -> int:
    """Take the instance lock; return the fd that owns it.

    The fd must stay open while this process owns the tree — closing it (or
    process exit) releases the lock. Raises AlreadyRunningError when another
    live owner holds it.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    lock_path = run_dir / _LOCK_NAME
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        raise AlreadyRunningError(
            f"another root supervisor already owns {run_dir} (lock {lock_path})"
        ) from exc
    # Record the owning pid for humans; the lock itself is the source of truth.
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    _log.info("instance lock acquired at %s", lock_path)
    return fd


def release_instance_lock(fd: int) -> None:
    """Release the instance lock and close its fd (idempotent at process exit)."""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError as exc:
        _log.debug("instance lock release failed: %s", exc)
    os.close(fd)
