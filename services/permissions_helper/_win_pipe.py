# pyright: reportAttributeAccessIssue=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportReturnType=false
"""Windows named-pipe transport for the permissions-helper client.

Isolated in its own module (and from pyright's POSIX ctypes stubs) because
every API here — WinDLL, WaitNamedPipeW, PeekNamedPipe, msvcrt.open_osfhandle,
os.O_BINARY — exists only on Windows, where the stdlib stubs are thin enough
that strict checking is noise. The client imports this only to dial the fixed pipe when `_IS_WINDOWS`.

Reads are polled with PeekNamedPipe against a caller-supplied deadline because
a pipe handle (unlike a socket) has no usable read timeout.
"""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from typing import Any

PIPE_NAME = "ava-permissions-helper"


def pipe_path(name: str = PIPE_NAME) -> str:
    """The full Win32 pipe path: \\.\\pipe\\<name>.

    Single backslashes throughout — a doubled one after the pipe name makes
    WaitNamedPipeW fail with ERROR_BAD_PATHNAME (161)."""
    return rf"\\.\pipe\{name}"


_CONNECT_ATTEMPTS = 5
_CONNECT_DELAY_S = 0.2


def connect(name: str = PIPE_NAME) -> tuple[Any, Any]:
    """Connect to the named pipe, returning (file, handle).

    WaitNamedPipeW bounds the connect wait so a missing helper surfaces as a
    timeout instead of a hang.
    """
    import msvcrt  # Windows-only; imported lazily so this module imports on POSIX

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.WaitNamedPipeW.restype = wintypes.BOOL
    kernel32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    full = pipe_path(name)
    connected = False
    for _ in range(_CONNECT_ATTEMPTS):
        if kernel32.WaitNamedPipeW(full, 500):
            connected = True
            break
        if ctypes.get_last_error() not in (2, 121):  # ERROR_FILE_NOT_FOUND / ERROR_SEM_TIMEOUT
            break
        time.sleep(_CONNECT_DELAY_S)
    if not connected:
        raise ConnectionError(f"permissions helper not reachable at pipe {name!r}")
    handle = kernel32.CreateFileW(full, 0xC0000000, 0, None, 3, 0, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ConnectionError(
            f"permissions helper pipe {name!r} open failed: {ctypes.get_last_error()}"
        )
    fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
    return os.fdopen(fd, "rb+", buffering=0), handle


def read_available(handle: Any, deadline: float) -> bytes:
    """Read whatever bytes are in the pipe, polling PeekNamedPipe till deadline.

    Returns b"" when the deadline passes with no data or the pipe is closed —
    the caller treats an empty read as EOF, matching the socket path.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.PeekNamedPipe.restype = wintypes.BOOL
    kernel32.PeekNamedPipe.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPDWORD,
        wintypes.LPDWORD,
        wintypes.LPDWORD,
    ]
    kernel32.ReadFile.restype = wintypes.BOOL
    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPDWORD,
        wintypes.LPVOID,
    ]
    avail = wintypes.DWORD(0)
    while time.monotonic() < deadline:
        ok = kernel32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(avail), None)
        if ok and avail.value > 0:
            buf = (ctypes.c_char * avail.value)()
            nread = wintypes.DWORD(0)
            kernel32.ReadFile(handle, buf, avail.value, ctypes.byref(nread), None)
            return bytes(buf[: nread.value])
        time.sleep(0.05)
    return b""
