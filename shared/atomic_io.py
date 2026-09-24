"""Same-directory atomic replacement, separate from post-commit policy.

Replacing the path commits a complete, visible file. A later parent-directory
fsync can still fail; callers choose whether that durability failure raises,
warns, or is suppressed. This module never creates or validates private
directories; secret storage keeps using ``shared.private_storage``.
"""

from __future__ import annotations

import os
import tempfile
from contextlib import suppress
from pathlib import Path


def fsync_parent(path: Path) -> None:
    """Sync the directory containing a committed path, without a platform guard."""
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_atomic(
    path: Path,
    data: bytes | str,
    *,
    encoding: str | None,
    mode: int | None,
    sync_file: bool,
    sync_parent: bool,
    prefix: str,
    suffix: str,
    suppress_cleanup_error: bool,
) -> None:
    fd, raw_tmp = tempfile.mkstemp(dir=path.parent, prefix=prefix, suffix=suffix)
    tmp = Path(raw_tmp)
    replaced = False
    try:
        if mode is not None and os.name != "nt":
            os.fchmod(fd, mode)
        if isinstance(data, bytes):
            stream = os.fdopen(fd, "wb")
            fd = -1
            with stream:
                stream.write(data)
                stream.flush()
                if sync_file:
                    os.fsync(stream.fileno())
        else:
            text_stream = os.fdopen(fd, "w", encoding=encoding)
            fd = -1
            with text_stream:
                text_stream.write(data)
                text_stream.flush()
                if sync_file:
                    os.fsync(text_stream.fileno())
        # Path.replace reaches os.replace and preserves both test injection seams.
        tmp.replace(path)
        replaced = True
        if sync_parent:
            fsync_parent(path)
    finally:
        if fd != -1:
            os.close(fd)
        if not replaced:
            if suppress_cleanup_error:
                with suppress(OSError):
                    tmp.unlink(missing_ok=True)
            else:
                tmp.unlink(missing_ok=True)


def write_bytes_atomic(
    path: Path,
    data: bytes,
    *,
    mode: int | None = None,
    sync_file: bool = True,
    sync_parent: bool = False,
    prefix: str | None = None,
    suffix: str = ".tmp",
    suppress_cleanup_error: bool = False,
) -> None:
    """Publish complete bytes through a unique sibling temporary file."""
    _write_atomic(
        path,
        data,
        encoding=None,
        mode=mode,
        sync_file=sync_file,
        sync_parent=sync_parent,
        prefix=prefix if prefix is not None else f".{path.name}.",
        suffix=suffix,
        suppress_cleanup_error=suppress_cleanup_error,
    )


def write_text_atomic(
    path: Path,
    data: str,
    *,
    encoding: str | None = None,
    mode: int | None = None,
    sync_file: bool = True,
    sync_parent: bool = False,
    prefix: str | None = None,
    suffix: str = ".tmp",
    suppress_cleanup_error: bool = False,
) -> None:
    """Publish complete text using the requested text-stream encoding."""
    _write_atomic(
        path,
        data,
        encoding=encoding,
        mode=mode,
        sync_file=sync_file,
        sync_parent=sync_parent,
        prefix=prefix if prefix is not None else f".{path.name}.",
        suffix=suffix,
        suppress_cleanup_error=suppress_cleanup_error,
    )
