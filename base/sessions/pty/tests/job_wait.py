"""Wait for a job typed into a real pty session to be running.

Shared by the pty session tests that type a long-lived job (`sleep 300`) into a
fresh login shell and then act on that job's process.
"""

from __future__ import annotations

import contextlib
import time

import psutil

# Ceiling for a typed job to show up as a child of its shell. A ceiling, not a
# delay: the poll returns the moment the job exists. The job cannot exist before
# the login shell has sourced its startup files (each forking short-lived
# helpers) and reached its prompt, which production allows 30s on a busy CI box
# (host._INITIAL_CMD_READY_TIMEOUT_S); the same budget applies here.
JOB_APPEARS_TIMEOUT_S = 30.0
_POLL_INTERVAL_S = 0.05


def wait_for_job(shell: psutil.Process, argv: list[str]) -> psutil.Process:
    """The child of `shell` whose argv is `argv`, once it exists.

    "The shell has some child" is not "the job is up": a login shell forks and
    reaps startup helpers (/etc/profile, rc files, prompt substitutions) before
    it reads the typed command, and one of those satisfies a bare
    `_wait(shell.children)`. The next `shell.children()` read then comes back
    empty (IndexError) or names the helper instead of the job. Matching the job
    by argv pins "the job is up", and the Process returned is the one from the
    read that matched, so no later read can disagree with the wait.
    """
    deadline = time.monotonic() + JOB_APPEARS_TIMEOUT_S
    while True:
        for child in shell.children():
            with contextlib.suppress(psutil.NoSuchProcess):  # reaped between the two reads
                if child.cmdline() == argv:
                    return child
        if time.monotonic() >= deadline:
            raise AssertionError(f"{argv} never appeared as a child of shell {shell.pid}")
        time.sleep(_POLL_INTERVAL_S)
