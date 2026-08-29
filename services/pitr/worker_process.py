"""Worker process lifecycle for PITR daemons: ownership handshake and reaping."""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from typing import NoReturn, Protocol

import psutil

from services.pitr.base_candidate import StopSignal

# Ownership gate (worker must not fork before controller adoption).
ADOPTION_TIMEOUT_S = 30


_LINUX_CHILD_ADOPTION_OPTION = 36


class WorkerQueue(Protocol):
    def put(self, item: object) -> None: ...

    def get(self, timeout: float | None = None) -> object: ...

    def get_nowait(self) -> object: ...

    def close(self) -> None: ...

    def join_thread(self) -> None: ...


class OwnedProcess(Protocol):
    def is_alive(self) -> bool: ...

    def join(self, timeout: float | None = None) -> None: ...


def reap_restore_subprocess_group(process: subprocess.Popen[str], leader_created_at: float) -> None:
    if process.pid == os.getpgrp():
        raise RuntimeError("refusing to signal the controller process group")
    try:
        leader = psutil.Process(process.pid)
        if abs(leader.create_time() - leader_created_at) >= 0.01:
            raise RuntimeError("restricted restore worker PID identity changed")
    except psutil.NoSuchProcess as exc:
        if group_members(process.pid):
            raise RuntimeError(
                "restricted restore descendants outlived their verifiable leader"
            ) from exc
        process.wait(timeout=1)
        return
    deadline = time.monotonic() + 20
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    grace = min(deadline, time.monotonic() + 5)
    while group_members(process.pid) and time.monotonic() < grace:
        time.sleep(0.1)
    while group_members(process.pid) and time.monotonic() < deadline:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        time.sleep(0.1)
    process.wait(timeout=max(0.1, deadline - time.monotonic()))
    if group_members(process.pid):
        raise RuntimeError("restricted restore worker process group could not be emptied")


def reject_restore_descendants() -> NoReturn:
    raise RuntimeError("restricted restore worker left live descendants")


def raise_surviving_restore_group() -> NoReturn:
    raise RuntimeError("restricted restore worker group survived its owned leader")


def worker_bootstrap(
    target: Callable[[StopSignal, WorkerQueue], None],
    stop: StopSignal,
    output: WorkerQueue,
    adopted: StopSignal,
) -> None:
    os.setsid()
    process = psutil.Process()
    output.put(
        (
            "ready",
            str(process.pid),
            str(os.getpgrp()),
            repr(process.create_time()),
        )
    )
    # No target work (no fork) before the controller adopts this worker.
    if not adopted.wait(timeout=ADOPTION_TIMEOUT_S):
        raise SystemExit("base candidate worker timed out waiting for controller adoption")
    target(stop, output)


def group_members(pgid: int) -> list[psutil.Process]:
    members: list[psutil.Process] = []
    for process in psutil.process_iter(["pid"]):
        try:
            if os.getpgid(process.pid) == pgid:
                members.append(process)
        except (ProcessLookupError, PermissionError, psutil.NoSuchProcess):
            continue
    return members


def enable_child_subreaper() -> None:
    if sys.platform != "linux":
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_LINUX_CHILD_ADOPTION_OPTION, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, "could not own orphaned base candidate descendants")


def reap_exited_group_children(process: OwnedProcess, pgid: int) -> None:
    process.join(timeout=0)
    while True:
        try:
            pid, _status = os.waitpid(-pgid, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def reap_job_group(
    process: OwnedProcess,
    *,
    worker_pid: int,
    pgid: int,
    leader_created_at: float,
    grace_s: float = 5,
    deadline_s: float = 20,
) -> None:
    if pgid != worker_pid or pgid == os.getpgrp():
        raise RuntimeError("refusing to signal an unowned base candidate process group")
    leader: psutil.Process | None = None
    with suppress(psutil.NoSuchProcess):
        leader = psutil.Process(worker_pid)
    if leader is not None and abs(leader.create_time() - leader_created_at) >= 0.01:
        raise RuntimeError("base candidate worker PID identity changed")
    deadline = time.monotonic() + deadline_s
    with suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGTERM)
    grace_end = min(deadline, time.monotonic() + grace_s)
    reap_exited_group_children(process, pgid)
    while group_members(pgid) and time.monotonic() < grace_end:
        time.sleep(0.1)
        reap_exited_group_children(process, pgid)
    while group_members(pgid) and time.monotonic() < deadline:
        with suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGKILL)
        time.sleep(0.1)
        reap_exited_group_children(process, pgid)
    if group_members(pgid):
        raise RuntimeError("base candidate process group could not be emptied")
    process.join(timeout=max(0, deadline - time.monotonic()))
    if process.is_alive():
        raise RuntimeError("base candidate worker leader could not be reaped")


def validate_ready_message(
    message: tuple[str, str, str, str], *, expected_pid: int
) -> tuple[int, float]:
    kind, raw_pid, raw_pgid, raw_created_at = message
    if kind != "ready" or int(raw_pid) != expected_pid or int(raw_pgid) != expected_pid:
        raise RuntimeError("base candidate worker reported invalid process ownership")
    return int(raw_pgid), float(raw_created_at)


def raise_live_descendants() -> NoReturn:
    raise RuntimeError("base candidate worker left live descendants")
