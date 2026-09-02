"""Minimal Windows Job Object ownership for disposable exec subprocesses.

The job handle, not a pid lookup, is the durable identity of the execution
tree. Closing a ``KILL_ON_JOB_CLOSE`` job stops every member even after the
root exited. Persistent Ava sessions explicitly break away from this job.

This module is import-safe off Windows; Win32 is loaded only by ``create``.
"""

from __future__ import annotations

import contextlib
import ctypes
import functools
import os
import subprocess
import time
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from typing import Any, cast

EXEC_JOB_GATE_ENV = "AVA_EXEC_JOB_GATE"
EXEC_JOB_GATE_TIMEOUT_S = 10.0


class _ExecJobState:
    attached = False


_exec_job_state = _ExecJobState()

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

_DWORD = ctypes.c_uint32
_BOOL = ctypes.c_int32
_SIZE_T = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", _DWORD),
        ("MinimumWorkingSetSize", _SIZE_T),
        ("MaximumWorkingSetSize", _SIZE_T),
        ("ActiveProcessLimit", _DWORD),
        ("Affinity", _SIZE_T),
        ("PriorityClass", _DWORD),
        ("SchedulingClass", _DWORD),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", _SIZE_T),
        ("JobMemoryLimit", _SIZE_T),
        ("PeakProcessMemoryUsed", _SIZE_T),
        ("PeakJobMemoryUsed", _SIZE_T),
    ]


@functools.lru_cache(maxsize=1)
def _kernel32() -> Any:
    loader = cast(
        "Callable[..., Any]",
        ctypes.WinDLL,  # type: ignore[attr-defined]  # Windows-only
    )
    api = loader("kernel32", use_last_error=True)
    api.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    api.CreateJobObjectW.restype = wintypes.HANDLE
    api.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        _DWORD,
    ]
    api.SetInformationJobObject.restype = _BOOL
    api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    api.AssignProcessToJobObject.restype = _BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = _BOOL
    return api


def _get_last_error() -> int:
    get_last_error = cast(
        "Callable[[], int]",
        ctypes.get_last_error,  # type: ignore[attr-defined]  # Windows-only
    )
    return get_last_error()


def _last_error(action: str, code: int | None = None) -> OSError:
    if code is None:
        code = _get_last_error()
    return OSError(code, f"{action} failed with Win32 error {code}")


class WindowsJob:
    """One non-inheritable, close-once Job Object handle."""

    def __init__(self, handle: int) -> None:
        self._handle: int | None = handle

    @classmethod
    def create(cls) -> WindowsJob:
        """Create an empty job that kills members when its last handle closes."""
        api = _kernel32()
        raw_handle = api.CreateJobObjectW(None, None)
        if not raw_handle:
            raise _last_error("CreateJobObjectW")
        handle = int(raw_handle)
        info = _ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | _JOB_OBJECT_LIMIT_BREAKAWAY_OK
        )
        ok = api.SetInformationJobObject(
            wintypes.HANDLE(handle),
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            code = _get_last_error()
            api.CloseHandle(wintypes.HANDLE(handle))
            raise _last_error("SetInformationJobObject", code)
        return cls(handle)

    @property
    def closed(self) -> bool:
        return self._handle is None

    @property
    def handle(self) -> int:
        """Borrow the non-inheritable handle for atomic process-creation attributes."""
        if self._handle is None:
            raise RuntimeError("cannot borrow a closed Job Object")
        return self._handle

    def assign(self, proc: subprocess.Popen[bytes]) -> None:
        """Attach ``proc`` through Popen's already-open process handle."""
        handle = self._handle
        if handle is None:
            raise RuntimeError("cannot assign a process to a closed Job Object")
        process_handle = getattr(proc, "_handle", None)
        if process_handle is None:
            raise RuntimeError("Windows Popen did not expose its process handle")
        if not _kernel32().AssignProcessToJobObject(
            wintypes.HANDLE(handle), wintypes.HANDLE(int(process_handle))
        ):
            raise _last_error("AssignProcessToJobObject")

    def close(self) -> None:
        """Close exactly once; the close hard-stops all non-breakaway members."""
        handle = self._handle
        if handle is None:
            return
        # Take ownership before the OS call. Retrying a failed CloseHandle with
        # a recycled numeric handle is more dangerous than surfacing the error.
        self._handle = None
        if not _kernel32().CloseHandle(wintypes.HANDLE(handle)):
            raise _last_error("CloseHandle")


def publish_parent_job_gate(gate: Path) -> None:
    """Publish ``ready`` through an exclusive file that is 0600 at creation."""
    fd = os.open(gate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        if os.write(fd, b"ready") != len(b"ready"):
            raise OSError(f"short write while publishing exec Job gate {gate}")
    finally:
        os.close(fd)


def await_parent_job_gate(value: str | None) -> None:
    """Block the Windows child entry until its parent attached the Job Object.

    The gate closes the Popen→Assign race: imports may run before assignment,
    but agent code cannot. If the parent disappears before opening the gate,
    its non-inheritable job handle closes and kills the child; the timeout is a
    final fail-closed guard for an attach path that failed before assignment.
    """
    if value is None:
        return
    gate = Path(value)
    deadline = time.monotonic() + EXEC_JOB_GATE_TIMEOUT_S
    while time.monotonic() < deadline:
        with contextlib.suppress(FileNotFoundError):
            if gate.read_text(encoding="ascii") == "ready":
                _exec_job_state.attached = True
                return
        time.sleep(0.01)
    os._exit(125)


def in_attached_exec_job() -> bool:
    """Whether this process crossed the parent-opened post-attach gate."""
    return _exec_job_state.attached


__all__ = [
    "EXEC_JOB_GATE_ENV",
    "WindowsJob",
    "await_parent_job_gate",
    "in_attached_exec_job",
    "publish_parent_job_gate",
]
