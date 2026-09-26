"""An application generation owns its original non-breakaway Job handle.

CreateProcess attaches the Job before any target instruction. Graceful stop
signals only captured private consoles and waits for native zero membership;
explicit force terminates the Job. A failed observation retains the handle and
custody. Root death closes the handle and kills members without claiming receipt.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import psutil

from services.ava_root.custody import ServiceCustody
from shared.native_process.ownership import OwnedProcess
from shared.winjob import WindowsJob, _last_error
from shared.winjob_spawn import _process_api, _start_in_job, run_job_process


class ApplicationProcess:
    def __init__(
        self, pid: int, handle: int, job: WindowsJob, handles: contextlib.ExitStack
    ) -> None:
        self.pid, self._handle, self.job, self._handles = pid, handle, job, handles
        self.returncode: int | None = None

    async def wait(self) -> int:
        while self.returncode is None:
            self.returncode = self._poll()
            if self.returncode is None:
                await asyncio.sleep(0.02)
        self._handles.close()
        return self.returncode

    def _poll(self) -> int | None:
        api = _process_api()
        outcome = api.WaitForSingleObject(self._handle, 0)
        if outcome == 258:  # WAIT_TIMEOUT: no blocked executor thread outlives cancellation.
            return None
        if outcome != 0:
            raise _last_error("wait application generation")
        code = ctypes.c_uint32()
        if not api.GetExitCodeProcess(self._handle, ctypes.byref(code)):
            raise _last_error("read application generation exit")
        return int(code.value)

    def members(self) -> set[OwnedProcess]:
        result: set[OwnedProcess] = set()
        for pid in self.job.member_pids():
            try:
                result.add(OwnedProcess.capture(psutil.Process(pid)))
            except psutil.NoSuchProcess:
                continue
        return result

    async def close(self, custody: ServiceCustody, *, timeout: float, force: bool) -> None:
        deadline = time.monotonic() + timeout
        members = self.members()
        custody.retain(members)
        if force:
            self.job.terminate()
        elif self.job.active_processes():
            await self._graceful(custody, members, deadline)
        while self.job.active_processes() or any(member.live() for member in members):
            members |= self.members()
            custody.retain(members)
            if time.monotonic() >= deadline:
                raise TimeoutError("application Job still has members; custody retained")
            await asyncio.sleep(0.02)
        # The original Job remains open through zero accounting and observed
        # native exits; retained PID objects are not mistaken for running code.

    async def _graceful(
        self, custody: ServiceCustody, members: set[OwnedProcess], deadline: float
    ) -> None:
        # A detached/new-console descendant is still a Job member. Signal each
        # remaining captured console; a GUI-only/unobservable console is unknown.
        delivered: set[int] = set()
        for identity in sorted(members, key=lambda item: item.pid):
            if identity.pid in delivered or not identity.live():
                continue
            budget = min(5.0, deadline - time.monotonic())
            if budget <= 0:
                raise TimeoutError("application graceful stop deadline expired")
            content = custody.path.read_bytes()
            argv = [
                sys.executable,
                "-I",
                str(Path(__file__).with_name("console.py")),
                str(custody.path),
                hashlib.sha256(content).hexdigest(),
                str(identity.pid),
                str(deadline),
            ]
            result = await asyncio.to_thread(run_job_process, argv, timeout=budget)
            if result.returncode and identity.live():
                raise RuntimeError(f"application console stop refused: {result.stderr.strip()}")
            if not result.returncode:
                delivered.update(json.loads(result.stdout)["delivered_pids"])
            if not self.job.active_processes():
                return


def spawn(
    argv: list[str],
    env: dict[str, str],
    log_fd: int,
    *,
    cwd: Path | None = None,
    command_line: str | None = None,
) -> ApplicationProcess:
    """Create the private-console application atomically in its retained Job."""
    if sys.platform != "win32":
        raise RuntimeError("native application Job requires Windows")
    import msvcrt

    if not argv or not Path(argv[0]).is_absolute():
        raise ValueError("Windows application executable must be absolute")
    job = WindowsJob.create(allow_breakaway=False)
    handles = contextlib.ExitStack()
    try:
        with Path(os.devnull).open("rb") as source:
            stdio = [
                msvcrt.get_osfhandle(source.fileno()),
                msvcrt.get_osfhandle(log_fd),
                msvcrt.get_osfhandle(log_fd),
            ]
            for handle in set(stdio):
                os.set_handle_inheritable(handle, True)  # noqa: FBT003 -- native positional API.
            process = _start_in_job(
                _process_api(),
                job,
                stdio,
                argv,
                handles,
                env=env,
                private_console=True,
                cwd=None if cwd is None else str(cwd),
                command_line=command_line,
            )
        return ApplicationProcess(int(process.pid), int(process.process), job, handles)
    except BaseException:
        handles.close()
        job.close()
        raise
