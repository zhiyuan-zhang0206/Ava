"""Windows logon-session identity helpers (import-safe on every platform).

The session boundary is the one thing `AttachConsole` cannot cross: a console
object lives in exactly one session, so a helper spawned in the caller's
session can attach the console of a target process in the *same* session only.
`winproc.graceful_signal` uses this module to decide which control channel a
target is reachable through, and the permissions-helper converge uses it to
say plainly when no interactive session exists to run GUI work in.

Session numbers are machine observations, never product constants: they change
across logins and are not stable identifiers. This module only ever *compares*
them (same / different) and detects the active console session.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Any

# WTSGetActiveConsoleSessionId returns this when nobody is logged on
# interactively (WinStation / WTS headers call it 0xFFFFFFFF).
_NO_ACTIVE_SESSION = 0xFFFFFFFF


def _kernel32() -> Any:
    if sys.platform != "win32":
        raise RuntimeError("Windows session identity is Windows-only")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel.ProcessIdToSessionId.restype = wintypes.BOOL
    kernel.WTSGetActiveConsoleSessionId.argtypes = []
    kernel.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD
    kernel.GetCurrentProcessId.argtypes = []
    kernel.GetCurrentProcessId.restype = wintypes.DWORD
    return kernel


def process_session_id(pid: int) -> int | None:
    """The session a process lives in, or None when it is gone / unreadable.

    None here means "identity unknown" — callers must treat that as a refusal
    (never as same-session), because guessing wrong would either deliver to an
    unrelated console or silently skip a delivery that cannot happen.
    """
    kernel = _kernel32()
    session = wintypes.DWORD()
    if not kernel.ProcessIdToSessionId(pid, ctypes.byref(session)):
        return None
    return int(session.value)


def current_session_id() -> int | None:
    """This process's own session, or None when it cannot be determined."""
    return process_session_id(_kernel32().GetCurrentProcessId())


def active_console_session_id() -> int | None:
    """The physical console session, or None when nobody is logged on there.

    None means no interactive session exists: GUI-dependent capabilities
    (permissions helper desktop automation) must report unavailable rather
    than launch into an empty hidden desktop.
    """
    session = _kernel32().WTSGetActiveConsoleSessionId()
    if session == _NO_ACTIVE_SESSION:
        return None
    return int(session)
