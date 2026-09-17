"""A throwaway postmaster that stays in its owner's process group.

pg_ctl daemonizes its server. Cancellable restore workers instead retain a
direct child so their controller can reap the complete job, including Postgres,
even when Python cleanup cannot run.
"""

from __future__ import annotations

import signal
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path

import psycopg


def start_foreground_postgres(
    argv: list[str], *, log: Path, env: Mapping[str, str] | None = None
) -> subprocess.Popen[bytes]:
    """Transfer the child handle before readiness checks can fail or be cancelled.

    `env` is the child's environment (None inherits the caller's); the throwaway
    fixture passes `pg_start_env()` so the postmaster is never started without a
    locale (Task #3754)."""
    with log.open("ab", buffering=0) as output:
        return subprocess.Popen(  # noqa: S603 -- resolved postgres and caller-owned data directory
            argv, stdin=subprocess.DEVNULL, stdout=output, stderr=output, env=env
        )


def wait_foreground_postgres(
    process: subprocess.Popen[bytes], *, log: Path, port: int, data: Path, timeout_s: float = 60
) -> None:
    """Accept only a live owned postmaster answering for the expected PGDATA."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"foreground Postgres exited {process.returncode}: {log}")
        try:
            with psycopg.connect(
                host="127.0.0.1",
                port=port,
                user="ava",
                dbname="postgres",
                connect_timeout=1,
                options="-c statement_timeout=1000",
            ) as connection:
                row = connection.execute("SHOW data_directory").fetchone()
        except psycopg.OperationalError:
            time.sleep(0.1)
            continue
        if row is None or Path(row[0]).resolve() != data.resolve():
            raise RuntimeError("foreground Postgres port is owned by another data directory")
        if process.poll() is not None:
            raise RuntimeError(f"foreground Postgres exited {process.returncode}: {log}")
        return
    raise TimeoutError(f"foreground Postgres did not become ready: {log}")


def stop_foreground_postgres(process: subprocess.Popen[bytes]) -> None:
    """Bound immediate shutdown; retain PGDATA if the direct child cannot die.

    This is for disposable clusters only. SIGQUIT asks the postmaster to stop
    its backends immediately; the enclosing job's process-group reaper also
    owns every backend when the worker itself has to be killed.
    """
    if process.poll() is None:
        process.send_signal(signal.SIGQUIT)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
    process.wait(timeout=2)
