"""The existing atomic Job creator with explicit pipe handles for exec ownership.

Only root stdin-read/stdout-write are inherited; the original host's control
writer is never in the HANDLE_LIST. The venv redirector is in the Job at birth.
"""

import contextlib
import ctypes
import os
import subprocess
import sys
from ctypes import wintypes
from typing import BinaryIO

from shared.winjob import WindowsJob, _last_error
from shared.winjob_spawn import _process_api, _start_in_job


class PipedJobChild:
    def __init__(
        self,
        pid: int,
        handle: int,
        source: BinaryIO,
        output: BinaryIO,
        cleanup: contextlib.ExitStack,
    ) -> None:
        self.pid = pid
        self._handle = handle
        self.stdin = source
        self.stdout = output
        self.returncode: int | None = None
        self._cleanup = cleanup

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is not None:
            return self.returncode
        api = _process_api()
        waited = api.WaitForSingleObject(
            self._handle, 0xFFFFFFFF if timeout is None else max(0, int(timeout * 1000))
        )
        if waited == 0x102:
            if timeout is None:
                raise RuntimeError("native infinite wait unexpectedly timed out")
            raise subprocess.TimeoutExpired("owned exec root", timeout)
        if waited != 0:
            raise _last_error("wait owned Job root")
        code = ctypes.c_uint32()
        if not api.GetExitCodeProcess(self._handle, ctypes.byref(code)):
            raise _last_error("read owned Job root exit")
        self.returncode = int(code.value)
        self._cleanup.close()
        return self.returncode

    def poll(self) -> int | None:
        try:
            return self.wait(timeout=0)
        except subprocess.TimeoutExpired:
            return None

    def kill(self) -> None:
        # Poll before TerminateProcess: an exited handle rejects it with ERROR_ACCESS_DENIED.
        if self.returncode is not None or self.poll() is not None:
            return
        api = _process_api()
        api.TerminateProcess.argtypes = [wintypes.HANDLE, ctypes.c_uint32]
        if not api.TerminateProcess(self._handle, 1):
            raise _last_error("terminate owned Job root")


def start_piped_job_process(argv: list[str], job: WindowsJob) -> PipedJobChild:
    if sys.platform != "win32":
        raise RuntimeError("atomic piped Job creation requires Windows")
    import msvcrt

    source_read, source_write = os.pipe()
    output_read, output_write = os.pipe()
    lifetime = contextlib.ExitStack()
    source = os.fdopen(source_write, "wb", buffering=0)
    output = os.fdopen(output_read, "rb", buffering=0)
    try:
        with (
            os.fdopen(source_read, "rb", buffering=0) as child_source,
            os.fdopen(output_write, "wb", buffering=0) as child_output,
        ):
            handles = [
                msvcrt.get_osfhandle(file.fileno())
                for file in (child_source, child_output, child_output)
            ]
            for handle in set(handles):
                os.set_handle_inheritable(handle, True)  # noqa: FBT003 -- native API positional argument.
            process = _start_in_job(_process_api(), job, handles, argv, lifetime)
        return PipedJobChild(process.pid, process.process, source, output, lifetime)
    except BaseException:
        lifetime.close()
        source.close()
        output.close()
        raise
