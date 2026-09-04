"""Native managed-domain ownership without importing agent graph or SDK state."""

import contextlib
import os
import signal
import subprocess
import time
from dataclasses import dataclass

import psutil

from shared.platform import IS_WINDOWS
from shared.winjob import WindowsJob
from shared.winjob_pipes import PipedJobChild

KILL_GRACE_S = 2.0
_ROOT_EXIT_POLL_S = 0.05


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
    """Only the direct owner closes this domain while its root is unreaped."""

    proc: subprocess.Popen[bytes] | PipedJobChild
    windows_job: WindowsJob | None

    def close_confirmed(self, deadline: float) -> None:
        if IS_WINDOWS:
            if self.windows_job is None:
                raise RuntimeError("Windows exec process has no Job Object")
            self.windows_job.terminate_and_confirm(deadline)
            return
        self.close()
        while _process_group_has_live_member(self.proc.pid):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("exec group still has live managed members")
            time.sleep(min(_ROOT_EXIT_POLL_S, remaining))

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
        try:
            if _process_group_has_live_member(self.proc.pid):
                os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except PermissionError:
            # macOS may reject killpg for a zombie-only pinned group.
            if not _process_group_has_live_member(self.proc.pid):
                return
            with contextlib.suppress(OSError):
                self.proc.kill()
            raise
        except BaseException:
            with contextlib.suppress(OSError):
                self.proc.kill()
            raise
