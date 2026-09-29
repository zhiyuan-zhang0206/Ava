"""Linux process descriptors through libc, independent of Python build headers.

Managed Python 3.12 builds may omit os.pidfd_open and signal.pidfd_send_signal.
Use one native implementation for both installed runtimes and preparation tools;
missing libc/kernel support fails before custody is granted, never via kill(pid).
"""

from __future__ import annotations

import ctypes
import os
import sys
from functools import cache


@cache
def _api() -> ctypes.CDLL:
    if sys.platform != "linux":
        raise RuntimeError("process descriptors require Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        opened = libc.pidfd_open
        sent = libc.pidfd_send_signal
    except AttributeError as exc:
        raise RuntimeError("Linux process custody requires libc pidfd support") from exc
    opened.argtypes = [ctypes.c_int, ctypes.c_uint]
    opened.restype = ctypes.c_int
    sent.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
    sent.restype = ctypes.c_int
    return libc


def open_process(pid: int) -> int:
    """Return a non-inheritable descriptor; the caller owns its close."""
    descriptor = _api().pidfd_open(pid, 0)
    if descriptor < 0:
        code = ctypes.get_errno()
        raise OSError(code, f"pidfd_open({pid}): {os.strerror(code)}")
    return descriptor


def send_signal(descriptor: int, signum: int) -> None:
    """Signal the retained task, preserving native errno for the custody owner."""
    if _api().pidfd_send_signal(descriptor, signum, None, 0) < 0:
        code = ctypes.get_errno()
        raise OSError(code, f"pidfd_send_signal({descriptor}): {os.strerror(code)}")


def require_available() -> None:
    """Prove open and signal support before a finite owner starts any children."""
    descriptor = open_process(os.getpid())
    try:
        send_signal(descriptor, 0)
    finally:
        os.close(descriptor)
