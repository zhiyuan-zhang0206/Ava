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

    python -m shared._reparent [--receipt <path> --nonce <nonce>] <stdout_log> <stderr_log> <cmd> [args...]

``<cmd> [args...]`` is the program to exec (the venv python + ``-m agent`` …).
This process ``setsid()``s (new session, no controlling terminal), forks once,
writes the launched child's pid to **its own stdout** (a pipe the spawner reads),
and exits; the child redirects std streams to the two log files and execs the
target. It runs in a fresh single-threaded interpreter, so the ``os.fork()`` here
is free of the multithreaded-parent fork hazard — the spawner reaches it via
``subprocess``' async-signal-safe fork+exec, never a bare ``os.fork()`` inside the
gateway's event loop.

Receipt mode (the optional ``--receipt/--nonce`` prefix, used by the updater's
gated spawn): the child atomically replaces the spawner's ``intent`` receipt
with its own ``birth`` receipt — exact pid / create time / ``/proc`` starttime —
BEFORE it opens logs or execs. A child that cannot write a complete birth
receipt exits without exec (fail closed: no exec without an identifiable
birth). The child-side code stays within the standard modules imported at
startup (os, sys, json, datetime) and constant-level work — it runs post-fork,
and must not import packages or take locks there.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import cast

# stdout/stderr log files are opened append-only; a fresh machine may not have
# $AVA_HOME/logs yet, but the caller (posixproc.new_session) mkdirs it first.
_LOG_OPEN_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_APPEND
_LOG_MODE = 0o644

# The child exits with this code when it cannot publish its birth receipt —
# distinct from the usage error (2) so the spawner's captured stderr can say
# which failure it was.
_BIRTH_FAILURE_EXIT_CODE = 3

_USAGE = "usage: _reparent [--receipt <path> --nonce <nonce>] <stdout_log> <stderr_log> <cmd> [args...]\n"


def _split_receipt_args(argv: list[str]) -> tuple[tuple[str, str] | None, list[str]]:
    """Split the optional ``--receipt <path> --nonce <nonce>`` prefix off argv.

    Both tokens are all-or-nothing: a malformed prefix exits 2 like a missing
    positional, never falling back to a receipt-less launch.
    """
    if not argv or argv[0] != "--receipt":
        return None, argv
    if len(argv) < 5 or argv[2] != "--nonce":
        sys.stderr.write(_USAGE)
        os._exit(2)
    return (argv[1], argv[3]), argv[4:]


def _read_intent(path: str, nonce: str) -> dict[str, object] | None:
    """Read the spawner's intent receipt; None on any mismatch or read failure.

    Only the fields the birth merge needs are touched: the file's own binding
    checks (against the attempt's facts) belong to the spawner's adjudication,
    while the child verifies the one thing it can — that this is its intent.
    """
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        body = os.read(fd, 64 * 1024)
    finally:
        os.close(fd)
    try:
        parsed: object = json.loads(body)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    intent = cast("dict[str, object]", parsed)
    if intent.get("kind") != "intent" or intent.get("nonce") != nonce:
        return None
    return dict(intent)


def _proc_self_identity() -> tuple[int, int, float] | None:
    """``(pid, starttime_ticks, create_time)`` for THIS process, or None.

    Linux ``/proc`` only, by construction: field 22 of ``/proc/self/stat`` is
    the clock-stable identity the receipt must carry, and the boot epoch from
    ``/proc/stat`` turns it into an epoch create time for cross-checks against
    psutil readings. Without them there is no exact identity, and the caller
    refuses to exec (fail closed).
    """
    try:
        fd = os.open("/proc/self/stat", os.O_RDONLY)
        try:
            stat_body = os.read(fd, 4096)
        finally:
            os.close(fd)
        # The command name may contain spaces or parentheses; split after the
        # final closing parenthesis (same parse as shared.session_record).
        starttime = int(stat_body.decode("ascii", "replace").rsplit(")", 1)[1].split()[22 - 3])
        fd = os.open("/proc/stat", os.O_RDONLY)
        try:
            boot_body = os.read(fd, 65536)
        finally:
            os.close(fd)
        btime = None
        for line in boot_body.decode("ascii", "replace").splitlines():
            if line.startswith("btime "):
                btime = float(line.split()[1])
                break
        if btime is None:
            return None
        return os.getpid(), starttime, btime + starttime / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None


def _atomic_write_json(path: str, payload: dict[str, object]) -> bool:
    """Durably replace ``path`` with ``payload``; False on any failure.

    temp + fsync + rename + parent-dir fsync, the same discipline as the
    spawner's writer (this file cannot import that one: it lives in the
    post-fork child, which may only use already-imported stdlib modules).
    """
    directory = str(Path(path).parent)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
            offset = 0
            while offset < len(body):
                offset += os.write(fd, body[offset:])
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)  # noqa: PTH105 — os-level child; explicit atomic replace
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        with contextlib.suppress(OSError):
            Path(tmp).unlink(missing_ok=True)
        return False
    return True


def _birth_payload(path: str, nonce: str) -> dict[str, object] | None:
    """The birth receipt payload: the intent plus this child's exact identity."""
    intent = _read_intent(path, nonce)
    if intent is None:
        return None
    identity = _proc_self_identity()
    if identity is None:
        return None
    pid, starttime, create_time = identity
    payload = dict(intent)
    payload["kind"] = "birth"
    payload["pid"] = pid
    payload["create_time"] = create_time
    payload["starttime"] = starttime
    payload["captured_at"] = dt.datetime.now(dt.UTC).isoformat()
    return payload


def _exec_child(
    stdout_path: str,
    stderr_path: str,
    cmd: list[str],
    receipt: tuple[str, str] | None,
) -> None:
    """Redirect std streams to the log files and exec `cmd` (never returns).

    Runs in the grandchild (post-fork). Only async-signal-safe os.* calls — no
    Python-level locks — so it is safe regardless of what the forking process
    held. stdin is /dev/null (the agent never reads it); stdout+stderr go to the
    log files so a pre-exec failure or a C-level traceback survives.

    Receipt mode writes the birth receipt FIRST — before any redirection or
    exec — so "exec without an identifiable birth" is impossible by
    construction; a failed write exits the child without exec.
    """
    if receipt is not None:
        payload = _birth_payload(receipt[0], receipt[1])
        if payload is None or not _atomic_write_json(receipt[0], payload):
            os._exit(_BIRTH_FAILURE_EXIT_CODE)
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
    receipt, argv = _split_receipt_args(argv)
    if len(argv) < 3:
        sys.stderr.write(_USAGE)
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
    _exec_child(stdout_path, stderr_path, cmd, receipt)


if __name__ == "__main__":
    main(sys.argv[1:])
