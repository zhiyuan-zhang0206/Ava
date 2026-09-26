"""One bounded POSIX command owns its process group from launch through closure."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import BinaryIO

from shared.exec_process_domain import ExecProcessDomain
from shared.process_group_closure import wait_group_finished

_CLOSE_SECONDS = 5.0


def _wait_for_completion(process: subprocess.Popen[bytes], timeout: float) -> None:
    # Never poll/wait/reap the leader here: its retained native pid pins the group
    # even when its children have already been reparented to PID 1.
    if not wait_group_finished(process, time.monotonic() + timeout):
        raise subprocess.TimeoutExpired(process.args, timeout)


def _close_and_reap(domain: ExecProcessDomain) -> int:
    deadline = time.monotonic() + _CLOSE_SECONDS
    domain.close_confirmed(deadline)
    return domain.proc.wait(timeout=max(0.001, deadline - time.monotonic()))


def _output(stream: BinaryIO) -> bytes:
    stream.seek(0)
    return stream.read()


def _complete(domain: ExecProcessDomain, timeout: float) -> int:
    process = domain.proc
    if not isinstance(process, subprocess.Popen):
        raise TypeError("POSIX command lacks its direct child")
    try:
        _wait_for_completion(process, timeout)
    except BaseException as original:
        try:
            _close_and_reap(domain)
        except BaseException as cleanup:
            raise RuntimeError(
                f"command failed ({type(original).__name__}: {original}); "
                f"owned cleanup unresolved ({type(cleanup).__name__}: {cleanup})"
            ) from cleanup
        raise
    return _close_and_reap(domain)


def run_owned_command(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    temporary: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Wait for natural domain completion, or close that domain before failure.

    Ordinary descendants inherit this process group, within the caller's session.
    Each direct owner retains its leader until bounded signal-and-observation
    cleanup completes. An interrupted session needs all of those owners' closure
    proofs; a census alone is insufficient. This is trusted-tool cleanup, not a
    containment fence against code creating an independent session/group.
    File-backed output avoids an inherited pipe becoming another unbounded wait.
    """
    if sys.platform not in {"darwin", "linux"}:
        raise RuntimeError("owned preparation commands require macOS or Linux")
    with (
        tempfile.TemporaryFile(dir=temporary) as stdout,
        tempfile.TemporaryFile(dir=temporary) as stderr,
    ):
        _process, domain = ExecProcessDomain.launch_posix(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
        )
        try:
            code = _complete(domain, timeout)
        except subprocess.TimeoutExpired as exc:
            exc.stdout, exc.stderr = _output(stdout), _output(stderr)
            raise
        return subprocess.CompletedProcess(
            argv,
            code,
            _output(stdout).decode("utf-8", errors="replace"),
            _output(stderr).decode("utf-8", errors="replace"),
        )
