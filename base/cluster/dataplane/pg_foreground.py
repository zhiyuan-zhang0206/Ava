"""Direct-child disposable Postgres startup and native shutdown."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import psycopg

POSTMASTER_SHUTDOWN_S = 10.0


def start_foreground_postgres(
    argv: list[str], *, log: Path, env: Mapping[str, str] | None = None
) -> subprocess.Popen[bytes]:
    """Return the direct child before readiness checks; no process-family receipts."""
    with log.open("ab", buffering=0) as output:
        return subprocess.Popen(  # noqa: S603 -- resolved postgres and private data directory
            argv, stdin=subprocess.DEVNULL, stdout=output.fileno(), stderr=output.fileno(), env=env
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
    """Ask pg_ctl to shut down this disposable PGDATA, then reap its direct child.

    PostgreSQL owns its native child shutdown. No descendant/cwd census or
    persisted family proof is made. A failed native stop raises and callers
    retain the scratch data directory rather than removing a running cluster.
    """
    if process.poll() is not None:
        return
    argv = process.args
    if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
        raise TypeError("foreground Postgres requires an argv list")
    command = cast("list[str]", argv)
    try:
        data = command[command.index("-D") + 1]
    except (ValueError, IndexError) as exc:
        raise ValueError(
            "foreground Postgres must name its private data directory with -D"
        ) from exc
    pg_ctl = Path(command[0]).with_name("pg_ctl")
    subprocess.run(  # noqa: S603 -- native tool next to the launched postgres
        [
            str(pg_ctl),
            "-D",
            data,
            "stop",
            "-m",
            "immediate",
            "-w",
            "-t",
            str(int(POSTMASTER_SHUTDOWN_S)),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=True,
        timeout=POSTMASTER_SHUTDOWN_S + 2,
    )
    process.wait(timeout=2)
