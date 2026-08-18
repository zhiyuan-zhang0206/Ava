"""Shared pidfile discipline for the daemon family.

Every daemon claims a pidfile at boot and refuses to start while one is
already held, so a watchdog respawn can never double up on the same
port/socket. Two historical weaknesses are fixed here once for the whole
family (audit round 2, P1):

1. **pid reuse.** A pidfile records a pid; a stale pidfile (daemon crashed
   without cleanup) whose pid the OS recycled for an unrelated process made
   ``_is_running`` misjudge "already running" — every later start exited
   until the file was removed by hand, and the watchdog spun on the failed
   revive. The liveness check therefore verifies *identity*: the pid must
   name a live process whose argv contains this daemon's module path.

2. **write race.** ``write_text`` truncates in place, so two daemons that
   both passed the guard could both write and both run. The claim is
   atomic (``O_CREAT | O_EXCL``): the loser of the race sees ``EEXIST``,
   re-checks identity, and exits; a stale file is reclaimed by unlink +
   retry.
"""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path

from shared.proc import process_alive, process_cmdline


def pidfile_holds_daemon(path: Path, module: str) -> bool:
    """True when ``path`` names a live process that is running ``module``.

    Pid-reuse-safe: a live pid whose argv does not contain ``module`` is an
    unrelated process that recycled the pid — the pidfile is stale, not the
    daemon running. A pidfile that cannot be read, a dead pid, or a live
    pid whose argv is unreadable (permission / zombie) all count as not
    holding the daemon.
    """
    try:
        pid = int(path.read_text().strip())
    except (FileNotFoundError, ValueError):
        return False
    if not process_alive(pid):
        return False
    cmdline = process_cmdline(pid)
    if not cmdline:
        return False
    return module in " ".join(cmdline)


def acquire_pidfile(path: Path, module: str) -> bool:
    """Atomically claim ``path`` for the current process as daemon ``module``.

    Returns True when this process now owns the pidfile; False when another
    live instance of the same daemon already holds it (the file is left
    untouched — the caller should log and exit). A stale pidfile — a dead
    pid, or a live pid that is not running ``module`` (pid reuse) — is
    reclaimed and the claim retried once.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if pidfile_holds_daemon(path, module):
                return False
            with suppress(OSError):
                path.unlink()
            continue
        try:
            os.write(fd, f"{os.getpid()}\n".encode())
        finally:
            os.close(fd)
        return True
    return False


def remove_pidfile(path: Path) -> None:
    """Best-effort unlink of the pidfile (idempotent)."""

    with suppress(OSError):
        path.unlink(missing_ok=True)
