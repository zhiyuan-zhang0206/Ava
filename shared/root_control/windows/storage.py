"""Native exclusive lock and write-through custody publication on local Windows storage.

The original non-inheritable file handle is the singleton. Custody is flushed
before process creation; completed custody moves out atomically before deletion.
Failed native operations propagate and never authorize a new generation; only a
momentary sharing conflict with another process's open handle is re-attempted,
within a short bound.
"""

from __future__ import annotations

import ctypes
import os
import sys
import tempfile
import time
import uuid
from ctypes import wintypes
from pathlib import Path

from shared.root_control.windows.native import DWORD, private_security
from shared.winjob import _get_last_error, _kernel32, last_error

# ERROR_ACCESS_DENIED / ERROR_SHARING_VIOLATION while another process (root,
# terminal owner, backend caller) momentarily holds the same record open.
_SHARING_CONFLICTS = frozenset({5, 32})
_SHARING_WAIT_S = 2.0


def acquire_lock(path: Path) -> int:
    """Open the exact lock file with no sharing; return a non-inheritable CRT fd."""
    if sys.platform != "win32":
        raise RuntimeError("the native exclusive lock requires Windows")
    import msvcrt

    kernel = _kernel32()
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        DWORD,
        DWORD,
        ctypes.c_void_p,
        DWORD,
        DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    if path.is_symlink() or path.is_junction():
        raise RuntimeError("root lock cannot be a reparse point")
    with private_security() as security:
        handle = kernel.CreateFileW(
            str(path), 0xC0000000, 0, ctypes.byref(security), 4, 0x00200080, None
        )
    if handle == wintypes.HANDLE(-1).value:
        raise last_error("open exclusive root lock", _get_last_error())
    try:
        return msvcrt.open_osfhandle(int(handle), os.O_RDWR | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(wintypes.HANDLE(handle))
        raise


def _move(source: Path, target: Path, *, replace: bool) -> None:
    """Rename write-through; ride out another process's momentary open handle.

    A reader holding the target open without delete sharing makes the rename
    fail with ERROR_ACCESS_DENIED or ERROR_SHARING_VIOLATION for as long as that
    handle lives. Such a conflict is re-attempted within ``_SHARING_WAIT_S``;
    every other failure, or a conflict that outlasts the bound, propagates.
    """
    kernel = _kernel32()
    kernel.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, DWORD]
    deadline = time.monotonic() + _SHARING_WAIT_S
    while not kernel.MoveFileExW(str(source), str(target), 8 | int(replace)):
        code = _get_last_error()
        if code not in _SHARING_CONFLICTS or time.monotonic() >= deadline:
            raise last_error("publish custody with MoveFileExW", code)
        time.sleep(0.01)


def read_published(path: Path) -> bytes:
    """Read a file that ``publish`` may be replacing at this very moment.

    Opening a name whose previous file is being superseded fails with a sharing
    or access denial instead of returning either version. Retry that within
    ``_SHARING_WAIT_S``; absence, a link, or a lasting denial propagates.
    """
    deadline = time.monotonic() + _SHARING_WAIT_S
    while True:
        try:
            if path.is_symlink() or path.is_junction():
                raise RuntimeError(f"custody must not be a link: {path}")
            return path.read_bytes()
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
        time.sleep(0.01)


def publish(path: Path, text: str, *, exclusive: bool = False) -> None:
    """Flush complete bytes, then publish with native write-through rename."""
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        _move(temporary, path, replace=not exclusive)
    finally:
        temporary.unlink(missing_ok=True)


def clear(path: Path) -> None:
    """Move confirmed custody out durably; leftover tombstones cannot authorize signals."""
    tombstone = path.parent.parent / f".closed-{path.stem}-{uuid.uuid4().hex}"
    _move(path, tombstone, replace=False)
    tombstone.unlink()
