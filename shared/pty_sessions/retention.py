"""Bound local PTY transcript storage without burdening every host start."""

from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path

from shared.log import logger

# Local transcript retention matches Loki's seven-day raw-log window. The
# daily stamp keeps frequent shell starts from repeatedly scanning the same
# per-machine logs directory.
_TRANSCRIPT_RETENTION_DAYS = 7
_TRANSCRIPT_PRUNE_INTERVAL_S = 24 * 60 * 60
_TRANSCRIPT_PRUNE_STAMP = ".transcript-retention.stamp"


def prune_stale_transcripts(logs_dir: Path, days: int = _TRANSCRIPT_RETENTION_DAYS) -> None:
    """Delete old top-level ``*.out.log`` transcripts at most once per day.

    The scan is deliberately non-recursive: sibling ``logs-trash-*``
    directories belong to the separate cleanup flow, and host diagnostics or
    any other suffix are outside this retention policy. A locked success stamp
    serializes concurrent host starts and is advanced only after a completed
    directory scan, so a failed scan is retried by the next host.
    """
    if days <= 0:
        raise ValueError("transcript retention days must be positive")

    stamp = logs_dir / _TRANSCRIPT_PRUNE_STAMP
    try:
        fd = os.open(stamp, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        logger.warning(
            "pty transcript retention could not open stamp {stamp}: {exc}",
            stamp=stamp,
            exc=exc,
        )
        return

    try:
        with os.fdopen(fd, "r+", encoding="ascii") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            now = time.time()
            raw_stamp = handle.read().strip()
            try:
                last_pruned_at = float(raw_stamp) if raw_stamp else 0.0
            except ValueError:
                last_pruned_at = 0.0
            if now - last_pruned_at < _TRANSCRIPT_PRUNE_INTERVAL_S:
                return

            cutoff = now - days * 24 * 60 * 60
            with os.scandir(logs_dir) as entries:
                for entry in entries:
                    if not entry.name.endswith(".out.log"):
                        continue
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                            Path(entry.path).unlink()
                    except OSError as exc:
                        logger.warning(
                            "pty transcript retention could not inspect or remove {path}: {exc}",
                            path=entry.path,
                            exc=exc,
                        )

            handle.seek(0)
            handle.truncate()
            handle.write(f"{now:.6f}\n")
            handle.flush()
    except OSError as exc:
        logger.warning(
            "pty transcript retention failed in {logs_dir}: {exc}",
            logs_dir=logs_dir,
            exc=exc,
        )
