"""Sweep of closed backup intermediates from a backup directory.

A dump pipeline writes plaintext and encrypted `.partial` files and a
`.backup-key-*` passphrase file; a cross-filesystem publication writes a
`.<artifact>.*.copy`. A run killed before its own cleanup leaves them behind.
Every backup run sweeps them under the backup lock, which excludes new
writers; only a file no process holds open is removed.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from pathlib import Path

import psutil

_log = logging.getLogger(__name__)


def _intermediate(name: str) -> bool:
    """A dump pipeline partial, a key file, or a cross-filesystem publish copy."""
    return (
        name.endswith(".partial")
        or name.startswith(".backup-key-")
        or (name.startswith(".") and name.endswith(".copy"))
    )


def sweep_closed_partials(directory: Path) -> None:
    """Remove intermediates whose writers closed; the caller excludes new writers.

    Only a process holding a partial open can still extend it, so a partial no
    process holds open has a closed writer and is removed. One still open (a
    killed run's orphaned tool, a publish copy not yet linked) stays, is
    reported, and goes on a later run.
    """
    stale = [path for path in directory.iterdir() if path.is_file() and _intermediate(path.name)]
    if not stale:
        return
    held = _held_open({str(path.resolve()) for path in stale})
    for path in stale:
        if str(path.resolve()) in held:
            _log.error("[backup] %s is still held open by a live writer; kept for now", path.name)
        else:
            path.unlink(missing_ok=True)


def _held_open(paths: set[str]) -> set[str]:
    held: set[str] = set()
    for process in psutil.process_iter():
        with suppress(psutil.Error):
            held.update(item.path for item in process.open_files() if item.path in paths)
    return held
