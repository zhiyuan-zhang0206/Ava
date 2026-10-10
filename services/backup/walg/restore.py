"""Restore a WAL-G backup into an empty directory and recover it with a scratch Postgres.

One code path for two uses: the operator's `ava backup walg restore` (disaster
recovery) and the weekly recovery drill (`drill.py`).

1. `wal-g backup-fetch <dir> <backup>` into an empty directory. An increment is
   fetched by its name, which resolves its whole chain; `LATEST` may name a backup
   newer than the recovery target.
2. `recovery.signal`, then a scratch postmaster on that directory whose settings are
   all launch arguments, none written to the directory:
   - `archive_mode=off`: a recovering instance must never archive into the live
     chain. The backup carries no archive setting (Ava's are launch arguments of the
     source, see `archive.py`), but a source that once wrote one into
     `postgresql.auto.conf` would bring it back, so it is overridden explicitly;
   - `restore_command` is `wal-g wal-fetch %f %p`, `recovery_target_*` is the
     requested target, `recovery_target_action=promote`;
   - no TCP listener, its own short unix-socket directory, a free port: the live
     cluster's data directory and port are never touched;
   - the capacity settings `pg_controldata` records for the directory (`max_connections`
     and friends): Postgres refuses to replay WAL from a primary that had larger
     values than the recovering instance.
3. Wait until `pg_is_in_recovery()` is false. A target that archived WAL cannot reach
   (a missing segment, a wrong key) ends in Postgres' own FATAL, which surfaces here as
   `RestoreError` with the end of its log: that is the proof the chain is continuous.

The scratch postmaster is a direct child of this process, so its whole process family is
reaped on exit (`pg_foreground`). The directory is kept for the operator and its
postmaster shut down cleanly; a drill passes `keep_data=False` and removes the directory itself.
"""

from __future__ import annotations

import getpass
import re
import shlex
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

import psycopg

from base.cluster.dataplane.pg_foreground import start_foreground_postgres, stop_foreground_postgres
from base.cluster.dataplane.pg_tools import pg_shm_args, pg_start_env, pg_tool, pg_tz_args
from base.db import connect_url
from base.native_process.child_env import daemon_process_env
from base.paths import ava_home
from services.backup.walg.archive import postgres_command
from services.backup.walg.runner import WalgCommandError, run_walg

RESTORE_TIMEOUT_S = 2 * 3600
"""Bound for each phase (`backup-fetch`, then recovery): the recovery-time objective
(RTO <= 2 hours). A restore slower than that has failed its purpose; measured and
extrapolated durations are under twenty minutes."""

_POLL_S = 0.2
# What the server says when it refuses a connection for good, matched on its (English, see
# `lc_messages` in `recovery_argv`) text: libpq reports no SQLSTATE for a failed connect.
_REFUSED = re.compile(
    r'(?:role|database) ".*" does not exist|authentication failed|no pg_hba\.conf entry'
)
_CLEAN_STOP_TIMEOUT_S = 60
_TOOL_TIMEOUT_S = 60

Report = Callable[[str], None]

_LSN = re.compile(r"[0-9A-F]{1,8}/[0-9A-F]{1,8}")

# pg_controldata label -> the setting it records. Replaying WAL requires the recovering
# instance to be at least as large as the primary was in each of these.
_CONTROL_SETTINGS = {
    "max_connections setting": "max_connections",
    "max_worker_processes setting": "max_worker_processes",
    "max_wal_senders setting": "max_wal_senders",
    "max_prepared_xacts setting": "max_prepared_transactions",
    "max_locks_per_xact setting": "max_locks_per_transaction",
}


class RestoreError(RuntimeError):
    """A restore step failed; the message says which and carries the end of Postgres' log."""


@dataclass(frozen=True)
class RecoveryTarget:
    """Where recovery stops: a time, an LSN, or (neither) the end of the archived WAL."""

    time: str | None = None
    lsn: str | None = None

    def __post_init__(self) -> None:
        if self.time is not None and self.lsn is not None:
            raise ValueError("a recovery target is a time or an LSN, not both")
        if self.lsn is not None and _LSN.fullmatch(self.lsn) is None:
            raise ValueError(f"{self.lsn!r} is not an LSN such as 0/3000060")
        if self.time is not None and not self.time.strip():
            raise ValueError("the recovery target time is empty")

    def pg_args(self) -> list[str]:
        if self.time is not None:
            return ["-c", f"recovery_target_time={self.time}"]
        if self.lsn is not None:
            return ["-c", f"recovery_target_lsn={self.lsn}"]
        return []

    def describe(self) -> str:
        if self.time is not None:
            return f"time {self.time}"
        if self.lsn is not None:
            return f"LSN {self.lsn}"
        return "the end of the archived WAL"


@dataclass(frozen=True)
class RestoredInstance:
    """A running, promoted scratch Postgres on the restored directory."""

    data_dir: Path
    socket_dir: Path
    port: int
    user: str

    def url(self, database: str) -> str:
        return f"postgresql://{self.user}@/{database}?host={self.socket_dir}&port={self.port}"


def _free_port() -> int:
    # Only names the socket file in a directory nobody else uses; there is no TCP listener.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _log_tail(log: Path) -> str:
    try:
        lines = log.read_text(errors="replace").strip().splitlines()
    except OSError:
        return "(no postgres log)"
    return " | ".join(lines[-6:]) or "(empty postgres log)"


def _prepare_directory(directory: Path) -> None:
    live = ava_home() / "pg"
    if directory.is_relative_to(live) or live.is_relative_to(directory):
        raise RestoreError(f"{directory} is or contains this home's live data directory {live}")
    if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
        raise RestoreError(f"{directory} exists and is not an empty directory")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)


def control_settings(directory: Path) -> dict[str, str]:
    """The capacity settings recorded in `directory`'s control file, as `-c` names and values."""
    result = subprocess.run(  # noqa: S603 — the resolved pg_controldata with one path
        [str(pg_tool("pg_controldata")), str(directory)],
        capture_output=True,
        text=True,
        check=False,
        timeout=_TOOL_TIMEOUT_S,
        env={**daemon_process_env(), "LC_ALL": "C"},  # labels are translated otherwise
    )
    if result.returncode != 0:
        raise RestoreError(f"pg_controldata failed: {result.stderr.strip()[-300:]}")
    found: dict[str, str] = {}
    for line in result.stdout.splitlines():
        label, _, value = line.partition(":")
        if label.strip() in _CONTROL_SETTINGS:
            found[_CONTROL_SETTINGS[label.strip()]] = value.strip()
    if len(found) != len(_CONTROL_SETTINGS):
        raise RestoreError("pg_controldata did not report every capacity setting")
    return found


def recovery_argv(
    directory: Path,
    *,
    socket_dir: Path,
    port: int,
    target: RecoveryTarget,
    path_reader: Callable[[], Path | None],
) -> list[str]:
    """The scratch postmaster's command line; every setting is an argument (see the module doc)."""
    capacity = [
        arg
        for name, value in sorted(control_settings(directory).items())
        for arg in ("-c", f"{name}={value}")
    ]
    return [
        str(pg_tool("postgres")),
        "-D",
        str(directory),
        "-p",
        str(port),
        "-c",
        "listen_addresses=",
        "-c",
        f"unix_socket_directories={socket_dir}",
        "-c",
        "unix_socket_permissions=0700",
        "-c",
        "archive_mode=off",
        "-c",
        "hot_standby=on",
        "-c",
        "lc_messages=C",
        "-c",
        f"restore_command={postgres_command('wal-fetch', '%f', '%p', path_reader=path_reader)}",
        "-c",
        "recovery_target_action=promote",
        *target.pg_args(),
        *capacity,
        *shlex.split(f"{pg_tz_args()} {pg_shm_args()}"),
    ]


def _wait_promoted(process: subprocess.Popen[bytes], instance: RestoredInstance, log: Path) -> int:
    """Block until recovery has ended and the instance is a primary; returns its timeline."""
    deadline = time.monotonic() + RESTORE_TIMEOUT_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RestoreError(
                f"recovery failed, postgres exited {process.returncode}: {_log_tail(log)}"
            )
        try:
            with connect_url(instance.url("postgres"), autocommit=True, connect_timeout=2) as conn:
                row = conn.execute(
                    "SELECT NOT pg_is_in_recovery(), "
                    "(SELECT timeline_id FROM pg_control_checkpoint())"
                ).fetchone()
                if row is not None and row[0]:
                    return int(row[1])
        except psycopg.OperationalError as exc:
            # Starting up, replaying WAL and a socket that is not there yet all mean "wait".
            # A refusal by the server (no such role, authentication) never changes: ending
            # here beats running out the two-hour bound.
            if _REFUSED.search(str(exc)):
                raise RestoreError(
                    f"the recovered instance refuses {instance.user!r}: {str(exc).strip()} "
                    "(the superuser of a restored cluster is the OS user that ran initdb on "
                    f"the source; name it with --user): {_log_tail(log)}"
                ) from None
        time.sleep(_POLL_S)
    raise RestoreError(f"recovery did not finish within {RESTORE_TIMEOUT_S}s: {_log_tail(log)}")


def _stop(process: subprocess.Popen[bytes], *, clean: bool) -> None:
    """Shut the scratch postmaster down; `clean` first asks for a fast shutdown (checkpoint)."""
    if clean and process.poll() is None:
        process.send_signal(signal.SIGINT)
        with suppress(subprocess.TimeoutExpired):  # the immediate shutdown below takes over
            process.wait(timeout=_CLEAN_STOP_TIMEOUT_S)
    stop_foreground_postgres(process)


@contextmanager
def restored_instance(
    directory: Path,
    *,
    backup: str,
    target: RecoveryTarget,
    report: Report,
    user: str | None = None,
    keep_data: bool,
    path_reader: Callable[[], Path | None],
) -> Generator[RestoredInstance]:
    """Fetch `backup` into `directory`, recover it to `target`, yield the promoted instance.

    On exit the scratch postmaster is stopped and its socket directory removed;
    `keep_data` makes that a clean shutdown (the directory is the product) instead of
    an immediate one (the caller deletes the directory).

    Raises:
        RestoreError: the directory is not empty or is the live data directory; fetch,
            or recovery failed (Postgres' own FATAL is in the message).
    """
    _prepare_directory(directory)
    started = time.monotonic()
    report(f"fetching backup {backup} into {directory}")
    try:
        run_walg(
            ["backup-fetch", str(directory), backup],
            timeout_s=RESTORE_TIMEOUT_S,
            path_reader=path_reader,
        )
    except WalgCommandError as exc:
        raise RestoreError(f"backup-fetch failed: {exc}") from None
    directory.chmod(0o700)
    (directory / "recovery.signal").touch()
    report(f"fetched in {time.monotonic() - started:.0f}s; recovering to {target.describe()}")

    # Short and private: a socket path is capped at 103 bytes, which a deep scratch path exceeds.
    socket_dir = Path(tempfile.mkdtemp(prefix="ava-walg-restore-", dir="/tmp"))
    log = socket_dir / "postgres.log"
    instance = RestoredInstance(directory, socket_dir, _free_port(), user or getpass.getuser())
    process: subprocess.Popen[bytes] | None = None
    try:
        argv = recovery_argv(
            directory,
            socket_dir=socket_dir,
            port=instance.port,
            target=target,
            path_reader=path_reader,
        )
        process = start_foreground_postgres(argv, log=log, env=pg_start_env())
        timeline = _wait_promoted(process, instance, log)
        report(f"promoted on timeline {timeline} after {time.monotonic() - started:.0f}s")
        yield instance
    finally:
        try:
            if process is not None:
                _stop(process, clean=keep_data)
        finally:
            shutil.rmtree(socket_dir, ignore_errors=True)
