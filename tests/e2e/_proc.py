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
from pathlib import Path

# Servers currently running under `managed_proc`, label -> (proc, log_path).
# `pytest_runtest_makereport` in conftest.py reads this on failure so a server
# that should have been up but wasn't (issue #213) leaves its exit code and log
# tail IN the failing report instead of only in an artifact nobody opens.
_LIVE_SERVERS: dict[str, tuple[subprocess.Popen[str], str | None]] = {}


def proc_log_tail(log_path: str | None, n: int = 40) -> str:
    """Last `n` lines of a managed process's merged log (or a short reason why
    there is no tail) — the evidence a dead server leaves behind."""
    if log_path is None:
        return "(no log_path; stdout was inherited by pytest)"
    try:
        lines = Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        return f"(log unreadable: {e!r})"
    if not lines:
        return "(log is empty)"
    return "\n".join(lines[-n:])


def dead_server_evidence() -> str:
    """For every registered server that exited while its fixture was still
    active, the exit code + log tail — appended to failing test reports."""
    parts: list[str] = []
    for label, (proc, log_path) in sorted(_LIVE_SERVERS.items()):
        code = proc.poll()
        if code is None:
            continue
        parts.append(
            f"[e2e] server '{label}' (pid {proc.pid}) was dead at failure time — "
            f"exit code {code}; log tail:\n{proc_log_tail(log_path)}"
        )
    return "\n\n".join(parts)


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
) -> Generator[subprocess.Popen[str]]:
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
    _LIVE_SERVERS[label] = (proc, log_path)
    try:
        yield proc
    finally:
        _LIVE_SERVERS.pop(label, None)
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
