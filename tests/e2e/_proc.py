"""subprocess.Popen + wait-for-port helpers for e2e fixtures."""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
import time
from collections.abc import Generator
from contextlib import contextmanager


def wait_for_port(host: str, port: int, timeout: float = 30.0, *, label: str) -> None:
    """Poll TCP connect (host, port) until success or timeout; raises RuntimeError otherwise."""
    deadline = time.monotonic() + timeout
    last_err: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError as e:
            last_err = e
            time.sleep(0.2)
    raise RuntimeError(
        f"Waiting for {label} ({host}:{port}) timed out after {timeout}s; last error: {last_err!r}"
    )


_SIGKILL_GRACE_SEC = 2.0


@contextmanager
def managed_proc(
    cmd: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    label: str,
    stop_signal: int = signal.SIGTERM,
    stop_timeout: float = 10.0,
    log_path: str | None = None,
) -> Generator[subprocess.Popen]:
    """Start subprocess + clean teardown kill.

    Uses `start_new_session=True` to make the child a process group leader;
    on teardown, sends SIGTERM to the whole group -- next dev / uvicorn both fork
    children; a single SIGTERM to the leader is not enough, need group-level kill.

    If `log_path` is given, merges stdout+stderr into that file -- lets the fixture
    dump child process logs on failure. When None, stdout/stderr inherit (go to
    pytest terminal).

    Teardown sequence: SIGTERM -> wait(stop_timeout) -> if not reaped, SIGKILL ->
    wait again (_SIGKILL_GRACE_SEC, default 2s). After SIGKILL, normal reaping is
    sub-second; short grace prevents unbounded waiting on D state hangs. Still
    timed out -> raise RuntimeError.
    """
    log_file = open(log_path, "w") if log_path is not None else None  # noqa: SIM115, PTH123 -- held for process lifetime, finally close
    stdout: int | object = log_file if log_file is not None else None
    stderr: int | object = subprocess.STDOUT if log_file is not None else None
    try:
        proc = subprocess.Popen(  # noqa: S603 -- cmd is a fixture-hardcoded constant
            cmd,
            cwd=cwd,
            env=env,
            stdout=stdout,
            stderr=stderr,
            text=True,
            start_new_session=True,
        )
    except Exception:
        # Popen failure (ENOENT etc.) must immediately close log_file to prevent
        # leak -- the finally: close below will not run because yield was never entered.
        if log_file is not None:
            log_file.close()
        raise
    try:
        yield proc
    finally:
        try:
            if proc.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, stop_signal)
                try:
                    proc.wait(timeout=stop_timeout)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGKILL)
                    # Short grace after SIGKILL (default 2s) -- SIGKILL cannot be
                    # ignored; reaping is sub-second. Still timed out means D state /
                    # NFS / zombie wedged; raise diagnostic.
                    try:
                        proc.wait(timeout=_SIGKILL_GRACE_SEC)
                    except subprocess.TimeoutExpired as e:
                        raise RuntimeError(
                            f"[{label}] pid={proc.pid} {_SIGKILL_GRACE_SEC}s after SIGKILL "
                            f"still not reaped (uninterruptible sleep / NFS / zombie); "
                            f"see {log_path or '(stdout inherited)'}"
                        ) from e
        finally:
            if log_file is not None:
                log_file.close()
