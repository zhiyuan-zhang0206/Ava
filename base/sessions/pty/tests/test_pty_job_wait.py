"""`wait_for_job` pins "the typed job is up", not "the shell has some child".

A login shell forks and reaps startup helpers before it reads the typed
command (CI flake: `IndexError` on `shell.children()[0]` after a wait on any
child). The stand-in shell below does the same on a fixed schedule: a helper
child first, the job only after the helper is gone.
"""

from __future__ import annotations

import contextlib
import subprocess

import psutil
import pytest

from base.native_process.os_platform import is_windows
from base.sessions.pty.tests.job_wait import wait_for_job

pytestmark = pytest.mark.skipif(is_windows(), reason="pty sessions are POSIX-only")

# The trailing `:` keeps bash from exec-ing the job in place of itself, so the
# job is a child of the stand-in shell, as `sleep 300` is of a session shell.
_SHELL_WITH_STARTUP_HELPER = "sleep 1.5; sleep 300; :"


def test_wait_for_job_skips_a_startup_helper_child() -> None:
    shell = subprocess.Popen(["/bin/bash", "-c", _SHELL_WITH_STARTUP_HELPER])  # noqa: S603 — fixed argv
    try:
        job = wait_for_job(psutil.Process(shell.pid), ["sleep", "300"])
        assert job.cmdline() == ["sleep", "300"], "must return the job, not the helper"
        assert job.ppid() == shell.pid
    finally:
        # Kill by PID from this Popen: the job outlives its shell if the shell dies first.
        with contextlib.suppress(psutil.NoSuchProcess):
            for child in psutil.Process(shell.pid).children(recursive=True):
                with contextlib.suppress(psutil.NoSuchProcess):
                    child.kill()
        shell.kill()
        shell.wait()
