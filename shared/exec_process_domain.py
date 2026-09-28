"""Native managed-domain ownership without importing agent graph or SDK state."""

from __future__ import annotations

import contextlib
import errno
import os
import signal
import subprocess
import sys
import threading
from dataclasses import InitVar, dataclass, field
from typing import Any, cast

import psutil

from shared.native_process import native_boot_id
from shared.native_process.ownership import OwnedProcess
from shared.platform import IS_WINDOWS
from shared.process_group_closure import confirm_closure
from shared.winjob import WindowsJob
from shared.winjob_pipes import PipedJobChild

KILL_GRACE_S = 2.0
_POSIX_LAUNCH = object()


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
        """Prove the domain closed; the POSIX leader stays unreaped for its caller."""
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
        if not isinstance(self.proc, subprocess.Popen):
            raise TypeError("exec domain has no POSIX direct child")
        # The shared core runs the rounds, the non-reaping leader-exit wait and
        # the kernel group listing; this domain keeps each round's signal authority.
        confirm_closure(self.proc, deadline, self._signal_round)

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
