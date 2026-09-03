"""Bounded one-shot Windows process family, assigned to a Job at creation.

Windows 10+ PROC_THREAD_ATTRIBUTE_JOB_LIST closes the CreateProcess→Assign
window, including venv redirectors which create their interpreter immediately.
This is only the console helper transport, not a persistent session backend.
"""

import contextlib
import ctypes
import os
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

from shared.winjob import WindowsJob, _kernel32, _last_error


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("reserved", wintypes.LPWSTR),
        ("desktop", wintypes.LPWSTR),
        ("title", wintypes.LPWSTR),
        ("x", ctypes.c_uint32),
        ("y", ctypes.c_uint32),
        ("x_size", ctypes.c_uint32),
        ("y_size", ctypes.c_uint32),
        ("x_chars", ctypes.c_uint32),
        ("y_chars", ctypes.c_uint32),
        ("fill", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("show", ctypes.c_uint16),
        ("reserved_size", ctypes.c_uint16),
        ("reserved_bytes", ctypes.c_void_p),
        ("stdin", wintypes.HANDLE),
        ("stdout", wintypes.HANDLE),
        ("stderr", wintypes.HANDLE),
    ]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [("info", _StartupInfo), ("attributes", ctypes.c_void_p)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [
        ("process", wintypes.HANDLE),
        ("thread", wintypes.HANDLE),
        ("pid", ctypes.c_uint32),
        ("tid", ctypes.c_uint32),
    ]


def _process_api() -> Any:
    api = _kernel32()
    api.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    api.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    api.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    api.DeleteProcThreadAttributeList.restype = None
    api.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_uint32,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(_StartupInfoEx),
        ctypes.POINTER(_ProcessInfo),
    ]
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, ctypes.c_uint32]
    api.WaitForSingleObject.restype = ctypes.c_uint32
    api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_uint32)]
    return api


def _start_in_job(
    api: Any, job: WindowsJob, handles: list[int], argv: list[str], cleanup: contextlib.ExitStack
) -> _ProcessInfo:
    size = ctypes.c_size_t()
    api.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
    if not size.value:
        raise _last_error("size process attribute list")
    buffer = ctypes.create_string_buffer(size.value)
    if not api.InitializeProcThreadAttributeList(buffer, 2, 0, ctypes.byref(size)):
        raise _last_error("initialize process attribute list")
    with contextlib.ExitStack() as attributes:
        attributes.callback(api.DeleteProcThreadAttributeList, buffer)
        jobs = (wintypes.HANDLE * 1)(job.handle)
        unique_handles = list(dict.fromkeys(handles))
        inherited = (wintypes.HANDLE * len(unique_handles))(*unique_handles)
        for attribute, values in ((0x2000D, jobs), (0x20002, inherited)):
            if not api.UpdateProcThreadAttribute(
                buffer, 0, attribute, values, ctypes.sizeof(values), None, None
            ):
                raise _last_error("update process attribute")
        startup = _StartupInfoEx()
        startup.info.cb = ctypes.sizeof(startup)
        startup.info.flags = 0x100  # STARTF_USESTDHANDLES
        startup.info.stdin, startup.info.stdout, startup.info.stderr = handles
        startup.attributes = ctypes.addressof(buffer)
        process = _ProcessInfo()
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
        if not api.CreateProcessW(
            argv[0],
            command,
            None,
            None,
            1,
            0x08080000,
            None,
            None,
            ctypes.byref(startup),
            ctypes.byref(process),
        ):
            raise _last_error("create helper in job")
        cleanup.callback(api.CloseHandle, process.process)
        cleanup.callback(api.CloseHandle, process.thread)
        return process


def run_job_process(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """Run the fixed loaded-image helper; timeout closes its complete Job family."""
    if sys.platform != "win32":
        raise RuntimeError("atomic Job process creation requires Windows 10 or later")
    import msvcrt

    if not argv or not Path(argv[0]).is_absolute():
        raise ValueError("helper interpreter must be an absolute loaded-image path")
    deadline = time.monotonic() + timeout
    api = _process_api()
    with contextlib.ExitStack() as cleanup:
        job = WindowsJob.create()
        cleanup.callback(job.close)
        source = cleanup.enter_context(Path(os.devnull).open("rb"))
        output = cleanup.enter_context(tempfile.TemporaryFile())
        errors = cleanup.enter_context(tempfile.TemporaryFile())
        handles = [msvcrt.get_osfhandle(file.fileno()) for file in (source, output, errors)]
        for handle in handles:
            os.set_handle_inheritable(handle, True)  # noqa: FBT003 - positional-only Win32 API
        process = _start_in_job(api, job, handles, argv, cleanup)
        remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
        waited = api.WaitForSingleObject(process.process, remaining_ms)
        if waited == 258:  # WAIT_TIMEOUT: no job handle is inherited by children.
            job.close()
            if api.WaitForSingleObject(process.process, 2000) != 0:
                raise RuntimeError("timed-out helper did not exit after Job close")
            raise subprocess.TimeoutExpired(argv, timeout)
        if waited != 0:
            raise _last_error("wait for helper")
        code = ctypes.c_uint32()
        if not api.GetExitCodeProcess(process.process, ctypes.byref(code)):
            raise _last_error("read helper exit code")
        job.close()  # also remove descendants if a helper unexpectedly spawned any
        output.seek(0)
        errors.seek(0)
        return subprocess.CompletedProcess(
            argv,
            code.value,
            output.read().decode("utf-8", errors="replace"),
            errors.read().decode("utf-8", errors="replace"),
        )
