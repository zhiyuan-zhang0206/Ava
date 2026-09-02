"""Bounded read-only bytes with regular-file and read-stability checks."""

import os
import stat
from pathlib import Path

from shared.runtime_release import ReleaseRejectedError


def regular_bytes(path: Path) -> bytes:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
        raise ReleaseRejectedError("inventory member is not a bounded regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            info.st_dev,
            info.st_ino,
        ):
            raise ReleaseRejectedError("inventory member changed while opening")
        body = stream.read(1024 * 1024 + 1)
        after = os.fstat(stream.fileno())
    current = path.lstat()
    if (
        len(body) > 1024 * 1024
        or (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
        or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        raise ReleaseRejectedError("inventory member changed while reading")
    return body
