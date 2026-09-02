"""Reparenting spawn helper — the POSIX "detach a child to init" primitive.

`shared.posixproc.new_session` runs this as ``python -m shared._reparent`` to
launch a long-running agent process fully detached from whoever spawned it. The
gateway / ops daemon that starts an agent is long-lived, so a naive
``Popen(start_new_session=True)`` agent would linger as a zombie in that parent
after it exits — nothing ever ``waitpid()``s it (the historical
``start_new_session`` daemon-zombie bug, see ``shared.service_respawn``). This
helper double-forks so the agent reparents to **init (pid 1)** immediately; init
reaps it on death, and the spawner's only direct child (this helper) exits at
once and is reaped by the spawner's own ``subprocess`` wait.

Invocation (argv, no shell — JSON argument elements pass through intact)::

    python -m shared._reparent <stdout_log> <stderr_log> <cmd> [args...]

``<cmd> [args...]`` is the program to exec (the venv python + ``-m agent`` …).
This process ``setsid()``s (new session, no controlling terminal), forks once,
writes the launched child's pid to **its own stdout** (a pipe the spawner reads),
and exits; the child redirects std streams to the two log files and execs the
target. It runs in a fresh single-threaded interpreter, so the ``os.fork()`` here
is free of the multithreaded-parent fork hazard — the spawner reaches it via
``subprocess``' async-signal-safe fork+exec, never a bare ``os.fork()`` inside the
gateway's event loop.
"""

from __future__ import annotations

import os
import sys

# stdout/stderr log files are opened append-only; a fresh machine may not have
# $AVA_HOME/logs yet, but the caller (posixproc.new_session) mkdirs it first.
_LOG_OPEN_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_APPEND
_LOG_MODE = 0o644


def _exec_child(stdout_path: str, stderr_path: str, cmd: list[str]) -> None:
    """Redirect std streams to the log files and exec `cmd` (never returns).

    Runs in the grandchild (post-fork). Only async-signal-safe os.* calls — no
    Python-level locks — so it is safe regardless of what the forking process
    held. stdin is /dev/null (the agent never reads it); stdout+stderr go to the
    log files so a pre-exec failure or a C-level traceback survives.
    """
    out_fd = os.open(stdout_path, _LOG_OPEN_FLAGS, _LOG_MODE)
    err_fd = (
        out_fd if stderr_path == stdout_path else os.open(stderr_path, _LOG_OPEN_FLAGS, _LOG_MODE)
    )
    null_fd = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null_fd, 0)
    os.dup2(out_fd, 1)  # closes the pid pipe inherited on fd 1 → spawner sees EOF
    os.dup2(err_fd, 2)
    os.execv(cmd[0], cmd)  # noqa: S606 — absolute interpreter path from repo internals; inherits this process's env


def main(argv: list[str]) -> None:
    if len(argv) < 3:
        sys.stderr.write("usage: _reparent <stdout_log> <stderr_log> <cmd> [args...]\n")
        os._exit(2)
    stdout_path, stderr_path, cmd = argv[0], argv[1], argv[2:]

    # New session: detach from any controlling terminal so a terminal SIGHUP can
    # never reach the agent. subprocess spawned this helper as a plain child (not
    # a group leader), so setsid always succeeds.
    os.setsid()
    pid = os.fork()
    if pid > 0:
        # Intermediate: hand the agent's pid back to the spawner (over the fd-1
        # pipe), then exit so the agent reparents to init. os.write is
        # async-signal-safe; no buffering to flush.
        os.write(1, str(pid).encode())
        os._exit(0)
    _exec_child(stdout_path, stderr_path, cmd)


if __name__ == "__main__":
    main(sys.argv[1:])
