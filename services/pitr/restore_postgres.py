"""Run a restore proof in a sibling PostgreSQL instance, never the live PGDATA."""

from __future__ import annotations

import hashlib
import os
import shlex
import socket
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import psutil
import psycopg
from psycopg import sql

from services.pitr.base_manifest import CandidateManifest, _lsn
from services.pitr.restore_proof import (
    DrillResult,
    LivePostgresIdentity,
    RestoreProofError,
    _is_zombie,
    update_restore_owner,
)


def _migration_hash(conn: psycopg.Connection[tuple[object, ...]]) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM schema_migrations ORDER BY name")
        names = [str(row[0]) for row in cur.fetchall()]
    return hashlib.sha256("\n".join(names).encode()).hexdigest()


def _live_identity(db_url: str, data_directory: str) -> LivePostgresIdentity:
    with psycopg.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT system_identifier, pg_postmaster_start_time()::text FROM pg_control_system()"
        )
        row = cur.fetchone()
        if row is None:
            raise RestoreProofError("live PostgreSQL omitted identity")
        system_identifier, started_at = (str(value) for value in row)
        cur.execute("SELECT 1")
        if cur.fetchone() != (1,):
            raise RestoreProofError("live PostgreSQL read probe failed")
    pid_path = Path(data_directory) / "postmaster.pid"
    pid = int(pid_path.read_text().splitlines()[0])
    created_at = psutil.Process(pid).create_time()
    fingerprint = hashlib.sha256(
        f"{data_directory}\n{system_identifier}\n{started_at}\n1".encode()
    ).hexdigest()
    return LivePostgresIdentity(
        pid, created_at, data_directory, system_identifier, started_at, fingerprint
    )


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _write_restore_allowlist(wal_dir: Path, run_root: Path) -> Path:
    records: dict[str, dict[str, object]] = {}
    for path in sorted(wal_dir.iterdir()):
        if path.is_symlink() or not path.is_file():
            raise RestoreProofError("restore archive contains a non-regular entry")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        path.chmod(0o400)
        records[path.name] = {"path": str(path), "sha256": digest, "size": path.stat().st_size}
    allowlist = run_root / "restore-allowlist.json"
    fd = os.open(allowlist, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as output:
        import json

        json.dump(records, output, sort_keys=True, separators=(",", ":"))
        output.flush()
        os.fsync(output.fileno())
    return allowlist


def _append_recovery_config(
    pgdata: Path, wal_dir: Path, socket_dir: Path, lsn: str, run_root: Path
) -> None:
    for path in (pgdata, wal_dir, socket_dir):
        if path.is_symlink() or not path.resolve().is_relative_to(run_root.resolve()):
            raise RestoreProofError("restore path escaped its owned run directory")
    allowlist = _write_restore_allowlist(wal_dir, run_root)
    command = " ".join(
        shlex.quote(value)
        for value in (
            sys.executable,
            "-m",
            "services.pitr.restore_wal_command",
            str(allowlist),
            "%f",
            "%p",
        )
    )
    config = pgdata / "postgresql.auto.conf"
    with config.open("w") as output:
        output.write(f"restore_command = {command!r}\n")
        output.write(f"recovery_target_lsn = {lsn!r}\n")
        output.write("recovery_target_action = 'promote'\n")
        output.write("archive_mode = 'off'\n")
        output.flush()
        os.fsync(output.fileno())
    (pgdata / "recovery.signal").touch(mode=0o600, exist_ok=False)


def _write_sandbox_config(pgdata: Path, socket_dir: Path, port: int, run_root: Path) -> Path:
    config = run_root / "sandbox-postgresql.conf"
    hba = run_root / "sandbox-pg_hba.conf"
    ident = run_root / "sandbox-pg_ident.conf"
    hba.write_text("local all all trust\n")
    ident.write_text("")
    hba.chmod(0o600)
    ident.chmod(0o600)
    values = {
        "data_directory": str(pgdata),
        "hba_file": str(hba),
        "ident_file": str(ident),
        "listen_addresses": "",
        "unix_socket_directories": str(socket_dir),
        "port": str(port),
        "external_pid_file": "",
        "ssl": "off",
        "logging_collector": "off",
        "shared_preload_libraries": "",
        "session_preload_libraries": "",
        "local_preload_libraries": "",
        "archive_mode": "off",
        "archive_command": "",
        "primary_conninfo": "",
        "primary_slot_name": "",
        "hot_standby": "off",
    }
    fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as output:
        for key, value in values.items():
            output.write(f"{key} = {value!r}\n")
        output.flush()
        os.fsync(output.fileno())
    return config


def _run(command: list[str], *, timeout: float) -> None:
    result = subprocess.run(  # noqa: S603
        command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        # Carry the child's own output in the error (a tail, bounded): the
        # 2026-09-03 activation #7 sandbox postmaster failure ("socket path too
        # long") was invisible because the previous DEVNULL discard ate pg_ctl's
        # stderr and the CLI refusal truncates to the first 300 chars of the
        # worker traceback — the cause must ride with the exception itself.
        detail = (result.stderr or result.stdout or "").strip()
        if len(detail) > 4000:
            detail = f"\u2026{detail[-4000:]}"
        raise RestoreProofError(
            f"restore command exited {result.returncode}: {command[0]}"
            + (f": {detail}" if detail else "")
        )


@dataclass(frozen=True)
class SandboxPostgresIdentity:
    pid: int
    created_at: float
    pgid: int
    data_directory: str


def _log_tail(path: Path, limit: int = 4000) -> str:
    """Bounded tail of a postmaster log, to ride with an error before cleanup."""
    try:
        tail = path.read_text(errors="replace").strip()
    except OSError:
        return ""
    if len(tail) > limit:
        tail = f"\u2026{tail[-limit:]}"
    return tail


def _spawn_sandbox_postgres(
    postgres: Path, pgdata: Path, config_file: Path, log_path: Path
) -> subprocess.Popen[str]:
    """Start the sandbox postmaster as our direct child, never via pg_ctl.

    pg_ctl detaches the postmaster with setsid() into its own session and
    process group, but the whole restore design reaps a run — restricted
    worker, sandbox postmaster and all — by signalling the worker's process
    group (worker_bootstrap setsid + killpg in worker_process.py /
    restore_proof.py). A pg_ctl-detached postmaster would survive the
    worker's crash unreachable by any group signal, so the sandbox must stay
    in OUR group: exec postgres directly with its output in the run-root log
    (activation #10 first surfaced the group-escape tripwire; #8/#9 hung
    earlier on pg_ctl's capture pipe for the same daemonization reason).
    """
    with log_path.open("ab", buffering=0) as log:
        return cast(
            "subprocess.Popen[str]",
            subprocess.Popen(  # noqa: S603 — resolved pg binary + static argv
                [
                    str(postgres),
                    "-D",
                    str(pgdata),
                    "-c",
                    f"config_file={config_file}",
                ],
                stdin=subprocess.DEVNULL,
                stdout=log.fileno(),
                stderr=log.fileno(),
            ),
        )


def _wait_for_sandbox_identity(
    process: subprocess.Popen[str],
    pgdata: Path,
    log_path: Path,
    timeout: int,
) -> SandboxPostgresIdentity:
    """Wait until the sandbox postmaster owns its pid file (or crashed)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            detail = _log_tail(log_path)
            raise RestoreProofError(
                f"sandbox postmaster exited {process.returncode}"
                + (f": {detail}" if detail else "")
            )
        if (pgdata / "postmaster.pid").exists():
            return _sandbox_identity(pgdata)
        time.sleep(0.1)
    detail = _log_tail(log_path)
    raise RestoreProofError(
        "sandbox postmaster never wrote its pid file" + (f": {detail}" if detail else "")
    )


def _sandbox_identity(pgdata: Path) -> SandboxPostgresIdentity:
    pid_path = pgdata / "postmaster.pid"
    try:
        lines = pid_path.read_text().splitlines()
        pid = int(lines[0])
        recorded_data_directory = Path(lines[1]).resolve()
        process = psutil.Process(pid)
        created_at = process.create_time()
        pgid = os.getpgid(pid)
    except (OSError, IndexError, ValueError, psutil.Error) as exc:
        raise RestoreProofError("cannot establish sandbox PostgreSQL identity") from exc
    if recorded_data_directory != pgdata.resolve():
        raise RestoreProofError("sandbox postmaster PID file names another data directory")
    command = " ".join(process.cmdline())
    if str(pgdata) not in command and str(pgdata.resolve()) not in command:
        raise RestoreProofError("sandbox postmaster command does not name its PGDATA")
    return SandboxPostgresIdentity(pid, created_at, pgid, str(recorded_data_directory))


def _matching_sandbox(identity: SandboxPostgresIdentity) -> psutil.Process | None:
    try:
        process = psutil.Process(identity.pid)
        if abs(process.create_time() - identity.created_at) >= 0.01:
            return None
        # A zombie postmaster is dead, not live — and it fails the pgid probe
        # below on macOS anyway (getpgid raises on zombies). Only the run()
        # cleanup reaps it, via the Popen handle (activation #12).
        if _is_zombie(process):
            return None
        if os.getpgid(identity.pid) != identity.pgid:
            return None
        return process
    except (ProcessLookupError, psutil.NoSuchProcess, psutil.ZombieProcess):
        return None
    except (PermissionError, psutil.AccessDenied) as exc:
        raise RestoreProofError("cannot verify sandbox PostgreSQL identity") from exc


def _stop_process_tree(identity: SandboxPostgresIdentity) -> None:
    leader = _matching_sandbox(identity)
    if leader is None:
        return
    owned: dict[tuple[int, float], psutil.Process] = {}
    try:
        members = [leader, *leader.children(recursive=True)]
        owned = {(item.pid, item.create_time()): item for item in members}
    except (psutil.AccessDenied, psutil.NoSuchProcess) as exc:
        raise RestoreProofError("cannot enumerate sandbox PostgreSQL descendants") from exc
    for member in reversed(list(owned.values())):
        with suppress(psutil.NoSuchProcess):
            member.terminate()
    deadline = time.monotonic() + 20
    alive = list(owned.values())
    while alive and time.monotonic() < deadline:
        with suppress(psutil.NoSuchProcess):
            for child in leader.children(recursive=True):
                owned[(child.pid, child.create_time())] = child
        _gone, alive = psutil.wait_procs(
            list(owned.values()), timeout=min(0.25, max(0, deadline - time.monotonic()))
        )
        if time.monotonic() + 5 >= deadline:
            for member in alive:
                with suppress(psutil.NoSuchProcess):
                    member.kill()
    if alive:
        raise RestoreProofError("sandbox PostgreSQL retained live descendants")


class IsolatedPostgresRestoreExecutor:
    def __init__(
        self,
        *,
        live_db_url: str,
        data_directory: str,
        pg_ctl: Path,
        pg_verifybackup: Path,
        timeout_seconds: int = 900,
    ) -> None:
        self._live_db_url = live_db_url
        self._data_directory = data_directory
        self._pg_ctl = pg_ctl
        self._postgres = pg_ctl.parent / "postgres"
        self._pg_verifybackup = pg_verifybackup
        self._timeout = timeout_seconds

    def live_identity(self) -> LivePostgresIdentity:
        return _live_identity(self._live_db_url, self._data_directory)

    def run(
        self,
        *,
        pgdata: Path,
        wal_dir: Path,
        candidate: CandidateManifest,
        run_root: Path,
        owner_path: Path,
    ) -> DrillResult:
        live = self.live_identity()
        if pgdata.resolve() == Path(live.data_directory).resolve():
            raise RestoreProofError("restore drill refused the live PostgreSQL data directory")
        if (pgdata / "postmaster.pid").exists():
            raise RestoreProofError("restore sandbox already has a postmaster")
        verify_started = time.monotonic()
        _run(
            [str(self._pg_verifybackup), "--no-parse-wal", str(pgdata)],
            timeout=6 * 3600,
        )
        restored_verify_seconds = time.monotonic() - verify_started
        socket_dir = run_root / "socket"
        socket_dir.mkdir(mode=0o700)
        port = _free_port()
        _append_recovery_config(pgdata, wal_dir, socket_dir, candidate.end_lsn, run_root)
        sandbox_config = _write_sandbox_config(pgdata, socket_dir, port, run_root)
        sandbox_log = run_root / "sandbox-postgres.log"
        replay_started = time.monotonic()
        sandbox: SandboxPostgresIdentity | None = None
        sandbox_process: subprocess.Popen[str] | None = None
        try:
            update_restore_owner(
                owner_path,
                state="postgres_starting",
                sandbox_pgdata=str(pgdata.resolve()),
                expected_sandbox_pgid=os.getpgrp(),
            )
            sandbox_process = _spawn_sandbox_postgres(
                self._postgres, pgdata, sandbox_config, sandbox_log
            )
            sandbox = _wait_for_sandbox_identity(
                sandbox_process, pgdata, sandbox_log, self._timeout
            )
            if sandbox.pgid != os.getpgrp():
                raise RestoreProofError("sandbox PostgreSQL escaped the restore process group")
            update_restore_owner(
                owner_path,
                state="postgres_running",
                sandbox_pid=sandbox.pid,
                sandbox_created_at=sandbox.created_at,
                sandbox_pgid=sandbox.pgid,
                sandbox_pgdata=sandbox.data_directory,
            )
            achieved = self._wait_for_promotion(
                socket_dir, port, candidate, sandbox_process, sandbox_log
            )
            replay_seconds = time.monotonic() - replay_started
            smoke_started = time.monotonic()
            restored_fingerprint = self._smoke(socket_dir, port, candidate)
            smoke_seconds = time.monotonic() - smoke_started
            if self.live_identity() != live:
                raise RestoreProofError("live PostgreSQL changed while sandbox was running")
            return DrillResult(
                achieved,
                replay_seconds,
                smoke_seconds,
                restored_verify_seconds,
                restored_fingerprint,
            )
        finally:
            if sandbox is None and (pgdata / "postmaster.pid").exists():
                sandbox = _sandbox_identity(pgdata)
                update_restore_owner(
                    owner_path,
                    state="postgres_running",
                    sandbox_pid=sandbox.pid,
                    sandbox_created_at=sandbox.created_at,
                    sandbox_pgid=sandbox.pgid,
                    sandbox_pgdata=sandbox.data_directory,
                )
            try:
                if sandbox is not None:
                    self._stop(pgdata, sandbox)
                    update_restore_owner(owner_path, state="postgres_stopped")
            finally:
                if sandbox_process is not None:
                    # The sandbox postmaster is our direct child; once stopped it
                    # lingers as a zombie until someone waitpid()s it. The stop
                    # path never reaps: a zombie fails the pgid identity probe,
                    # and an unreaped one kept looking "live" to the cleanup
                    # guards, masking the real failure (activation #12). Reap it
                    # here on every exit path.
                    with suppress(Exception):
                        sandbox_process.wait(timeout=10)

    def _wait_for_promotion(
        self,
        socket_dir: Path,
        port: int,
        candidate: CandidateManifest,
        process: subprocess.Popen[str] | None = None,
        log_path: Path | None = None,
    ) -> str:
        deadline = time.monotonic() + self._timeout
        db_url = f"postgresql://?host={socket_dir}&port={port}&dbname=postgres"
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if process is not None and process.poll() is not None:
                detail = _log_tail(log_path) if log_path is not None else ""
                raise RestoreProofError(
                    f"sandbox postmaster exited {process.returncode} before promotion"
                    + (f": {detail}" if detail else "")
                ) from last_error
            try:
                with psycopg.connect(db_url, connect_timeout=2) as conn, conn.cursor() as cur:
                    cur.execute("SELECT pg_is_in_recovery(), pg_last_wal_replay_lsn()::text")
                    row = cur.fetchone()
                    if row is not None and row[0] is False and row[1] is not None:
                        achieved = str(row[1])
                        if _lsn(achieved) >= _lsn(candidate.end_lsn):
                            return achieved
            except psycopg.Error as exc:
                last_error = exc
            time.sleep(0.25)
        raise RestoreProofError("sandbox did not promote at the target LSN") from last_error

    @staticmethod
    def _smoke(socket_dir: Path, port: int, candidate: CandidateManifest) -> str:
        db_url = f"postgresql://?host={socket_dir}&port={port}&dbname={candidate.database_name}"
        with psycopg.connect(db_url, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute("SELECT system_identifier FROM pg_control_system()")
            row = cur.fetchone()
            if row != (candidate.system_identifier,):
                raise RestoreProofError("restored system identifier differs")
            if _migration_hash(conn) != candidate.migration_set_sha256:
                raise RestoreProofError("restored migration set differs")
            evidence: list[str] = [candidate.system_identifier, candidate.migration_set_sha256]
            for table, order in (
                ("agents_meta", "id"),
                ("checkpoints", "thread_id, checkpoint_id"),
                ("events", "id"),
            ):
                query = sql.SQL(
                    "SELECT to_jsonb(sample)::text FROM {} AS sample ORDER BY {} LIMIT 16"
                ).format(
                    sql.Identifier(table),
                    sql.SQL(", ").join(sql.Identifier(part) for part in order.split(", ")),
                )
                cur.execute(query)
                rows = [str(row[0]) for row in cur.fetchall()]
                if not rows:
                    raise RestoreProofError(f"restored {table} smoke sample is empty")
                evidence.extend((table, *rows))
            cur.execute(
                "SELECT n.nspname, c.relname, c.relkind, "
                "pg_get_userbyid(c.relowner) FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema' "
                "ORDER BY 1,2,3"
            )
            evidence.extend("|".join(str(value) for value in row) for row in cur.fetchall())
            return hashlib.sha256("\n".join(evidence).encode()).hexdigest()

    def _stop(self, pgdata: Path, identity: SandboxPostgresIdentity) -> None:
        if Path(identity.data_directory) != pgdata.resolve():
            raise RestoreProofError("refusing to stop PostgreSQL outside the restore sandbox")
        current = _matching_sandbox(identity)
        if current is None:
            return
        with suppress(RestoreProofError):
            _run(
                [str(self._pg_ctl), "-D", str(pgdata), "-m", "fast", "-w", "stop"],
                timeout=30,
            )
        _stop_process_tree(identity)
        if _matching_sandbox(identity) is not None:
            raise RestoreProofError("sandbox PostgreSQL could not be reaped")
