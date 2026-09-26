"""Native exclusive lock and write-through custody publication on local Windows storage.

The original non-inheritable file handle is the singleton. Custody is flushed
before process creation; completed custody moves out atomically before deletion.
Failed native operations propagate and never authorize a new generation.
"""

from __future__ import annotations

import ctypes
import os
import sys
import tempfile
import uuid
from ctypes import wintypes
from pathlib import Path

from shared.root_control.windows.native import DWORD, private_security
from shared.winjob import _get_last_error, _kernel32, _last_error


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
        raise _last_error("open exclusive root lock", _get_last_error())
    try:
        return msvcrt.open_osfhandle(int(handle), os.O_RDWR | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(wintypes.HANDLE(handle))
        raise


def _move(source: Path, target: Path, *, replace: bool) -> None:
    kernel = _kernel32()
    kernel.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, DWORD]
    if not kernel.MoveFileExW(str(source), str(target), 8 | int(replace)):
        raise _last_error("publish custody with MoveFileExW")


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
