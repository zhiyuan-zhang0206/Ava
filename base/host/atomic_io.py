"""Same-directory atomic replacement, separate from post-commit policy.

Replacing the path commits a complete, visible file. A later parent-directory
fsync can still fail; callers choose whether that durability failure raises,
warns, or is suppressed. This module never creates or validates private
directories; secret storage keeps using ``base.host.private_storage``.

The three calls whose failure a test must cause (``replace``, ``fsync`` and
``fchmod``) are the fields of :class:`Fs`; an :class:`AtomicWriter` runs every
write through one. This module's own tests build ``AtomicWriter(Fs(...))`` with
the failure they need. Every other module calls ``write_text_atomic``,
``write_bytes_atomic`` and ``fsync_parent`` (the writer over ``SYSTEM_FS``) and
its tests use real files under ``tmp_path``: the commit, cleanup and durability
contract is this module's, so it is proved here and nowhere else.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Fs:
    """The filesystem calls an atomic write makes whose failure a test has to cause."""

    # Rename `source` onto `target` (both in one directory), replacing it atomically.
    replace: Callable[[Path, Path], None]
    # Flush a file descriptor (a file or a directory) to stable storage.
    fsync: Callable[[int], None]
    # Set the permission bits of an open file.
    fchmod: Callable[[int, int], None]


def _replace(source: Path, target: Path) -> None:
    source.replace(target)


def _fsync(fd: int) -> None:
    os.fsync(fd)


def _fchmod(fd: int, mode: int) -> None:
    os.fchmod(fd, mode)


# The real calls. Each looks the `os` function (or `Path.replace`) up when it is called, so it
# never captures a function a test has replaced.
SYSTEM_FS = Fs(replace=_replace, fsync=_fsync, fchmod=_fchmod)


def _fsync_parent(fs: Fs, path: Path) -> None:
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        fs.fsync(fd)
    finally:
        os.close(fd)


def _write_atomic(
    fs: Fs,
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
        if mode is not None:
            fs.fchmod(fd, mode)
        if isinstance(data, bytes):
            stream = os.fdopen(fd, "wb")
            fd = -1
            with stream:
                stream.write(data)
                stream.flush()
                if sync_file:
                    fs.fsync(stream.fileno())
        else:
            text_stream = os.fdopen(fd, "w", encoding=encoding)
            fd = -1
            with text_stream:
                text_stream.write(data)
                text_stream.flush()
                if sync_file:
                    fs.fsync(text_stream.fileno())
        fs.replace(tmp, path)
        replaced = True
        if sync_parent:
            _fsync_parent(fs, path)
    finally:
        if fd != -1:
            os.close(fd)
        if not replaced:
            if suppress_cleanup_error:
                with suppress(OSError):
                    tmp.unlink(missing_ok=True)
            else:
                tmp.unlink(missing_ok=True)


@dataclass(frozen=True)
class AtomicWriter:
    """Atomic writes through one :class:`Fs`; the methods are the module functions' contract."""

    fs: Fs

    def fsync_parent(self, path: Path) -> None:
        """Sync the directory containing a committed path, without a platform guard."""
        _fsync_parent(self.fs, path)

    def write_bytes(
        self,
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
            self.fs,
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

    def write_text(
        self,
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
            self.fs,
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


_SYSTEM_WRITER = AtomicWriter(SYSTEM_FS)


def fsync_parent(path: Path) -> None:
    """Sync the directory containing a committed path, without a platform guard."""
    _SYSTEM_WRITER.fsync_parent(path)


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
    _SYSTEM_WRITER.write_bytes(
        path,
        data,
        mode=mode,
        sync_file=sync_file,
        sync_parent=sync_parent,
        prefix=prefix,
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
    _SYSTEM_WRITER.write_text(
        path,
        data,
        encoding=encoding,
        mode=mode,
        sync_file=sync_file,
        sync_parent=sync_parent,
        prefix=prefix,
        suffix=suffix,
        suppress_cleanup_error=suppress_cleanup_error,
    )
