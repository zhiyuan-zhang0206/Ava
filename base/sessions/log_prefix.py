"""Unique session prefixes prevent filelog content-fingerprint collisions.

Identical shell or CLI banners otherwise give separate logs the same leading
content. Only newly created files receive a header; existing logs stay intact.
"""

import os
from datetime import datetime, timezone
from pathlib import Path


def session_log_header(name: str, *, pid: int | None = None) -> bytes:
    """Return a UTF-8 session identity line with a UTC start timestamp."""
    start = datetime.now(timezone.utc).isoformat()  # noqa: UP017 - task specifies timezone.utc
    process = f" pid={pid}" if pid is not None else ""
    return f"--- ava session {name} start={start}{process} ---\n".encode()


def open_session_log(path: Path, name: str, *, pid: int | None = None) -> tuple[int, bool]:
    """Open a transcript, writing its identity only when exclusively created."""
    flags = os.O_WRONLY | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return os.open(path, flags | os.O_APPEND), False
    try:
        header = session_log_header(name, pid=pid)
        while header:
            written = os.write(fd, header)
            if written == 0:
                raise OSError("Session log header write made no progress")  # noqa: TRY301 - close fd on failure
            header = header[written:]
    except BaseException:
        os.close(fd)
        raise
    return fd, True
