"""Native managed-domain ownership without importing agent graph or SDK state."""

from __future__ import annotations

import contextlib
import ctypes
import errno
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import InitVar, dataclass, field
from functools import cache
from typing import Any, cast

import psutil

from shared.native_process import native_boot_id
from shared.native_process.ownership import OwnedProcess
from shared.platform import IS_WINDOWS
from shared.winjob import WindowsJob
from shared.winjob_pipes import PipedJobChild

KILL_GRACE_S = 2.0
_ROOT_EXIT_POLL_S = 0.05
_POSIX_LAUNCH = object()
_PROC_PGRP_ONLY = 2  # <sys/proc_info.h>: list PIDs by process-group id.


class ExecDomainBirthError(RuntimeError):
    """A launched child remains owned, but native admission is unresolved."""

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        super().__init__("exec domain native birth is unresolved after launch")
        self.proc = proc


def _process_group_has_live_member(pgid: int) -> bool:
    """Read a still-pinned group; unreadable members are never absence."""
    for process in psutil.process_iter(["pid", "status"]):
        try:
            if os.getpgid(process.info["pid"]) != pgid:
                continue
            if process.info["status"] in {psutil.STATUS_DEAD, psutil.STATUS_ZOMBIE}:
                continue
            if process.info["status"] is None:
                raise psutil.AccessDenied(process.info["pid"])
            return True
        except (ProcessLookupError, psutil.NoSuchProcess):
            continue
    return False


@cache
def _proc_listpids() -> Any:
    if sys.platform != "darwin":
        raise RuntimeError("kernel process-group listing requires macOS")
    listpids = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True).proc_listpids
    listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
    listpids.restype = ctypes.c_int
    return listpids


def _darwin_group_listing(pgid: int) -> list[int]:
    """Every PID XNU files under `pgid`, zombies included, as one snapshot.

    `proc_listpids(PROC_PGRP_ONLY)` walks allproc then zombproc under the
    proc-list lock that fork, exit and reap take to change those lists, unlike
    an enumerate-then-read scan. A result filling the buffer may be truncated;
    it already names more than the leader, which never confirms closure.
    """
    listpids = _proc_listpids()
    ctypes.set_errno(0)
    size = listpids(_PROC_PGRP_ONLY, pgid, None, 0)
    if size <= 0:
        code = ctypes.get_errno()
        raise OSError(code, f"proc_listpids size for group {pgid}: {os.strerror(code)}")
    width = ctypes.sizeof(ctypes.c_int)
    buffer = (ctypes.c_int * (size // width))()
    ctypes.set_errno(0)
    filled = listpids(_PROC_PGRP_ONLY, pgid, buffer, ctypes.sizeof(buffer))
    code = ctypes.get_errno()
    if filled <= 0 and code:
        raise OSError(code, f"proc_listpids for group {pgid}: {os.strerror(code)}")
    return list(buffer[: filled // width])


def process_group_closed(pgid: int) -> bool:
    """Whether no process, live or zombie, remains in group `pgid`.

    Only meaningful once the group's leader was reaped: the number stays
    reserved while any member exists, so no other group can take it. macOS
    reads the kernel group listing (`_darwin_group_listing`). Linux sends the
    group a null signal, which walks the group under the tasklist lock that
    fork holds to add a child to it, so a member mid-fork keeps the answer
    false. Neither is an enumerate-then-read scan.
    """
    if sys.platform == "darwin":
        return not _darwin_group_listing(pgid)
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    return False


def process_group_pids(pgid: int) -> list[int]:
    """Name the members of a group that `process_group_closed` found occupied.

    For capturing and reporting survivors only; never a proof of closure. On
    Linux this is a process-table scan, so a member can exit or appear during it.
    """
    if sys.platform == "darwin":
        return sorted(_darwin_group_listing(pgid))
    members: list[int] = []
    for process in psutil.process_iter(["pid"]):
        pid = cast("int", process.info["pid"])
        with contextlib.suppress(ProcessLookupError, psutil.NoSuchProcess):
            if os.getpgid(pid) == pgid:
                members.append(pid)
    return sorted(members)


@dataclass
class ExecProcessDomain:
    """Direct launch authority; a retained receipt alone cannot create it."""

    proc: subprocess.Popen[bytes] | PipedJobChild
    windows_job: WindowsJob | None
    _launch: InitVar[object | None] = None
    _birth: OwnedProcess | None = field(init=False, default=None)
    _boot: str | None = field(init=False, default=None)
    _closed: bool = field(init=False, default=False)
    _lock: threading.RLock = field(init=False, default_factory=threading.RLock)

    def __post_init__(self, _launch: object | None) -> None:
        if IS_WINDOWS:
            return
        if _launch is not _POSIX_LAUNCH:
            raise RuntimeError("POSIX exec custody requires its own launch boundary")
        self._boot = native_boot_id()
        self._birth = OwnedProcess.capture(psutil.Process(self.proc.pid))
        self._require_unreaped()

    @classmethod
    def launch_posix(
        cls, argv: list[str], *, new_session: bool = False, **options: Any
    ) -> tuple[subprocess.Popen[bytes], ExecProcessDomain]:
        """Create the group and retain its direct child before any reap.

        The launch syscall establishes group ownership even when macOS already
        reports a zombie and no longer exposes its PGID. Callers must not poll,
        wait, communicate or signal through Popen before domain closure.
        """
        if IS_WINDOWS:
            raise RuntimeError("POSIX exec launch is unavailable on Windows")
        if {
            "process_group",
            "start_new_session",
            "text",
            "encoding",
            "universal_newlines",
        } & options.keys():
            raise ValueError("exec launch owns process grouping and byte streams")
        proc = cast(
            "subprocess.Popen[bytes]",
            subprocess.Popen(  # noqa: S603 -- admitted argv.
                argv,
                start_new_session=new_session,
                process_group=-1 if new_session else 0,
                **options,
            ),
        )
        try:
            return proc, cls(proc, None, _launch=_POSIX_LAUNCH)
        except BaseException as exc:
            raise ExecDomainBirthError(proc) from exc

    def _require_unreaped(self) -> None:
        if not isinstance(self.proc, subprocess.Popen) or self.proc.returncode is not None:
            raise RuntimeError("exec domain direct leader was already reaped")
        if self._birth is None or self._boot is None or self._boot != native_boot_id():
            raise RuntimeError("exec domain native birth is unavailable")
        current = psutil.Process(self.proc.pid)
        if (
            not self._birth.same_birth(OwnedProcess.capture(current))
            or current.ppid() != os.getpid()
        ):
            raise RuntimeError("exec domain no longer owns its direct native leader")

    def leader_alive(self) -> bool:
        self._require_unreaped()
        assert self._birth is not None  # noqa: S101 -- checked by _require_unreaped.
        return self._birth.live()

    def members_live(self) -> bool:
        """Observe the pinned trusted-tool group; this is not a closure proof."""
        self._require_unreaped()
        return _process_group_has_live_member(self.proc.pid)

    def signal(self, signum: int) -> None:
        """Signal the launch-owned group while its direct child remains pinned."""
        if IS_WINDOWS:
            raise RuntimeError("Windows exec domains stop through their Job Object")
        with self._lock:
            if self._closed:
                return
            if not isinstance(self.proc, subprocess.Popen):
                raise TypeError("exec domain has no POSIX direct child")
            # Popen poll/wait share this lock. No numeric signal may race the
            # release of the direct child's PID; unknown custody never falls back.
            with cast("Any", self.proc)._waitpid_lock:
                self._require_unreaped()
                os.killpg(self.proc.pid, signum)

    def close_confirmed(self, deadline: float) -> None:
        with self._lock:
            if self._closed:
                return
            self._confirm_closure(deadline)
            self._closed = True

    def _confirm_closure(self, deadline: float) -> None:
        if IS_WINDOWS:
            if self.windows_job is None:
                raise RuntimeError("Windows exec process has no Job Object")
            self.windows_job.terminate_and_confirm(deadline)
            return
        while True:
            self._signal_round()
            if _process_group_has_live_member(self.proc.pid):
                unresolved = "exec group still has live managed members"
            else:
                late = self._late_members(deadline)
                if not late:
                    return
                unresolved = f"exec group still lists members {late} besides its leader"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(unresolved)
            time.sleep(min(_ROOT_EXIT_POLL_S, remaining))

    def _signal_round(self) -> None:
        try:
            self.close()
        except PermissionError as exc:
            # XNU returns EPERM for a group containing only zombies. Confirm the
            # retained leader exited and no member is live after the attempted
            # kernel signal; this is trusted-tool cleanup, not an arbitrary-code fence.
            self._require_unreaped()
            if (
                sys.platform != "darwin"
                or exc.errno != errno.EPERM
                or self._birth is None
                or self._birth.live()
                or _process_group_has_live_member(self.proc.pid)
            ):
                raise

    def _late_members(self, deadline: float) -> list[int]:
        """Group PIDs besides the exited leader that an empty live sample can miss.

        XNU lets a member that is inside fork() when killpg arrives complete
        the fork, and the child never receives the signal. If the child appears
        after the sample enumerated PIDs and its parent exits before its own
        status read, the sample reports no live member. Linux instead fails
        that fork or signals the child too, so its post-signal sample is
        already complete.

        On macOS, first observe the signalled leader exit, so it has no fork in
        flight, and only then list the group. The unreaped leader pins the group
        id and only members fork into it, so a listing of just that leader
        leaves no process able to add one. Any other listed process, live or
        zombie, needs another round: a state read after the listing could see
        it exited after forking.
        """
        if sys.platform != "darwin":
            return []
        assert self._birth is not None  # noqa: S101 -- set at POSIX launch.
        while self._birth.live():
            # Already signalled this round: wait for its exit, never re-signal.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("exec group leader is still live after its group signal")
            time.sleep(min(_ROOT_EXIT_POLL_S, remaining))
        listed = _darwin_group_listing(self.proc.pid)
        if self.proc.pid not in listed:
            raise RuntimeError(f"exec group listing {listed} lost its retained leader")
        return [pid for pid in listed if pid != self.proc.pid]

    def close(self) -> None:
        if IS_WINDOWS:
            if self.windows_job is None:
                raise RuntimeError("Windows exec process has no Job Object")
            try:
                self.windows_job.close()
            except BaseException:
                with contextlib.suppress(OSError):
                    self.proc.kill()
                raise
            return
        # Never skip this signal based on a prior process-table census. Delivery
        # uncertainty retains the unreaped leader; Popen.kill would poll/reap it.
        self.signal(signal.SIGKILL)
