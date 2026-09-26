"""Run a restore proof in a sibling PostgreSQL instance, never the live PGDATA."""

from __future__ import annotations

import hashlib
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import psutil
import psycopg
from psycopg import sql

from services.pitr.base_manifest import CandidateManifest, _lsn
from services.pitr.operation_custody import NativeProcess
from services.pitr.restore_proof import (
    DrillResult,
    LivePostgresIdentity,
    RestoreProofError,
    _same_live,
    update_restore_owner,
)
from shared.pg_tools import pg_start_env


def _migration_hash(conn: psycopg.Connection[tuple[object, ...]]) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM schema_migrations ORDER BY name")
        names = [str(row[0]) for row in cur.fetchall()]
    return hashlib.sha256("\n".join(names).encode()).hexdigest()


def _smoke_samples(cur: psycopg.Cursor[tuple[object, ...]]) -> list[str]:
    """Anchor-table samples for the restore-proof evidence fingerprint.

    A table a migration dropped (events, per the 2026-08-29 ruling) is
    recorded as absent instead of failing the proof: the migration-set hash
    already pins the schema, so absence is evidence, not a mismatch."""
    evidence: list[str] = []
    for table, order in (
        ("agents_meta", "id"),
        ("checkpoints", "thread_id, checkpoint_id"),
        ("events", "id"),
    ):
        cur.execute("SELECT to_regclass(%s)", (table,))
        found = cur.fetchone()
        if found is None or found[0] is None:
            evidence.append(f"absent:{table}")
            continue
        # Sample probe for the evidence row: 16 rows prove the table readable
        # without shipping it (task #3696 exception inventory).
        query = sql.SQL(
            "SELECT to_jsonb(sample)::text FROM {} AS sample ORDER BY {} LIMIT 16"
        ).format(
            sql.Identifier(table),
            sql.SQL(", ").join(sql.Identifier(part) for part in order.split(", ")),
        )
        cur.execute(query)
        rows = [str(item[0]) for item in cur.fetchall()]
        if not rows:
            raise RestoreProofError(f"restored {table} smoke sample is empty")
        evidence.extend((table, *rows))
    return evidence


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
    native = NativeProcess.capture(psutil.Process(pid))
    fingerprint = hashlib.sha256(
        f"{data_directory}\n{system_identifier}\n{started_at}\n1".encode()
    ).hexdigest()
    return LivePostgresIdentity(native, data_directory, system_identifier, started_at, fingerprint)


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
    result = subprocess.run(  # noqa: S603 -- trusted inherited operation tool.
        command, capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode != 0:
        # Carry the child's own output in the error (a tail, bounded): the
        # 2026-09-03 activation #7 sandbox postmaster failure ("socket path too
        # long") was invisible because the previous DEVNULL discard ate pg_ctl's
        # stderr and the CLI refusal truncates to the first 300 chars of the
        # worker traceback — the cause must ride with the exception itself.
        detail = (result.stderr or result.stdout or "").strip()
        # Excerpt cap for the carried cause (task #3696 exception inventory).
        if len(detail) > 4000:
            detail = f"\u2026{detail[-4000:]}"
        raise RestoreProofError(
            f"restore command exited {result.returncode}: {command[0]}"
            + (f": {detail}" if detail else "")
        )


@dataclass(frozen=True)
class SandboxPostgresIdentity:
    native: NativeProcess
    pgid: int
    sid: int
    data_directory: str


def _log_tail(path: Path, limit: int = 4000) -> str:
    """Bounded tail of a postmaster log, to ride with an error before cleanup
    (4000 chars; task #3696 exception inventory)."""
    try:
        tail = path.read_text(errors="replace").strip()
    except OSError:
        return ""
    if len(tail) > limit:
        tail = f"\u2026{tail[-limit:]}"
    return tail


def _spawn_sandbox_postgres(
    postgres: Path, pgdata: Path, config_file: Path, log_path: Path
) -> subprocess.Popen[bytes]:
    """Launch a foreground postmaster in the operation worker's group."""
    with log_path.open("ab", buffering=0) as log:
        return subprocess.Popen(  # noqa: S603 -- trusted foreground postmaster.
            [str(postgres), "-D", str(pgdata), "-c", f"config_file={config_file}"],
            stdin=subprocess.DEVNULL,
            stdout=log.fileno(),
            stderr=log.fileno(),
            env=pg_start_env(),
        )


def _capture_sandbox(process: subprocess.Popen[bytes], pgdata: Path) -> SandboxPostgresIdentity:
    native = NativeProcess.capture(psutil.Process(process.pid))
    return SandboxPostgresIdentity(
        native, os.getpgid(process.pid), os.getsid(process.pid), str(pgdata.resolve())
    )


def _wait_for_sandbox_identity(
    captured: SandboxPostgresIdentity,
    pgdata: Path,
    log_path: Path,
    timeout: int,
) -> SandboxPostgresIdentity:
    """Wait until the sandbox postmaster owns its pid file (or crashed)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if captured.native.live() is None:
            detail = _log_tail(log_path)
            raise RestoreProofError(
                "sandbox postmaster exited before PID-file readiness"
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
        native = NativeProcess.capture(process)
        pgid = os.getpgid(pid)
        sid = os.getsid(pid)
    except (OSError, IndexError, ValueError, psutil.Error) as exc:
        raise RestoreProofError("cannot establish sandbox PostgreSQL identity") from exc
    if recorded_data_directory != pgdata.resolve():
        raise RestoreProofError("sandbox postmaster PID file names another data directory")
    command = " ".join(process.cmdline())
    if str(pgdata) not in command and str(pgdata.resolve()) not in command:
        raise RestoreProofError("sandbox postmaster command does not name its PGDATA")
    return SandboxPostgresIdentity(native, pgid, sid, str(recorded_data_directory))


def _matching_sandbox(identity: SandboxPostgresIdentity) -> psutil.Process | None:
    try:
        process = identity.native.live()
        if process is not None and (
            os.getpgid(process.pid) != identity.pgid or os.getsid(process.pid) != identity.sid
        ):
            raise RestoreProofError("sandbox PostgreSQL escaped its recorded process group")
        return process
    except (ProcessLookupError, psutil.NoSuchProcess):
        return None
    except (PermissionError, psutil.AccessDenied) as exc:
        raise RestoreProofError("cannot verify sandbox PostgreSQL identity") from exc


def _stop_sandbox(identity: SandboxPostgresIdentity, process: subprocess.Popen[bytes]) -> None:
    if process.pid != identity.native.process.pid:
        raise RestoreProofError("sandbox stop belongs to another child")
    if _matching_sandbox(identity) is not None:
        identity.native.process.send_signal(signal.SIGINT)
    # Local postmaster shutdown is needed for a valid proof. Failure leaves its
    # receipt and scratch intact; the outer operation owner closes descendants.
    process.wait(timeout=20)


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
        sandbox_process: subprocess.Popen[bytes] | None = None
        try:
            update_restore_owner(
                owner_path,
                state="postgres_starting",
                sandbox_pgdata=str(pgdata.resolve()),
                expected_sandbox_sid=os.getsid(0),
            )
            sandbox_process = _spawn_sandbox_postgres(
                self._postgres, pgdata, sandbox_config, sandbox_log
            )
            sandbox = _capture_sandbox(sandbox_process, pgdata)
            update_restore_owner(
                owner_path,
                sandbox_native=sandbox.native.value(),
                sandbox_pgid=sandbox.pgid,
                sandbox_sid=sandbox.sid,
            )
            ready_sandbox = _wait_for_sandbox_identity(sandbox, pgdata, sandbox_log, self._timeout)
            if not sandbox.native.same_birth(ready_sandbox.native):
                raise RestoreProofError("sandbox pid file differs from its captured launch")
            if sandbox.pgid != os.getpgrp() or sandbox.sid != os.getsid(0):
                raise RestoreProofError("sandbox PostgreSQL escaped the restore process group")
            update_restore_owner(
                owner_path,
                state="postgres_running",
                sandbox_native=sandbox.native.value(),
                sandbox_pgid=sandbox.pgid,
                sandbox_sid=sandbox.sid,
                sandbox_pgdata=sandbox.data_directory,
            )
            achieved = self._wait_for_promotion(socket_dir, port, candidate, sandbox, sandbox_log)
            replay_seconds = time.monotonic() - replay_started
            smoke_started = time.monotonic()
            restored_fingerprint = self._smoke(socket_dir, port, candidate)
            smoke_seconds = time.monotonic() - smoke_started
            _same_live(live, self.live_identity())
            return DrillResult(
                achieved,
                replay_seconds,
                smoke_seconds,
                restored_verify_seconds,
                restored_fingerprint,
            )
        finally:
            if sandbox is not None and sandbox_process is not None:
                self._finish_sandbox(pgdata, sandbox, sandbox_process, owner_path)

    def _finish_sandbox(
        self,
        pgdata: Path,
        sandbox: SandboxPostgresIdentity,
        process: subprocess.Popen[bytes],
        owner_path: Path,
    ) -> None:
        original = sys.exception()
        try:
            self._stop(pgdata, sandbox, process)
            update_restore_owner(owner_path, state="postgres_stopped")
        except BaseException as cleanup:
            if original is not None:
                original.add_note(f"sandbox custody remains unresolved: {cleanup}")
                raise original from cleanup
            raise

    def _wait_for_promotion(
        self,
        socket_dir: Path,
        port: int,
        candidate: CandidateManifest,
        captured: SandboxPostgresIdentity | None = None,
        log_path: Path | None = None,
    ) -> str:
        deadline = time.monotonic() + self._timeout
        db_url = f"postgresql://?host={socket_dir}&port={port}&dbname=postgres"
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if captured is not None and captured.native.live() is None:
                detail = _log_tail(log_path) if log_path is not None else ""
                raise RestoreProofError(
                    "sandbox postmaster exited before promotion" + (f": {detail}" if detail else "")
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
            # The identifier arrives as a bigint; the manifest carries the
            # frozen text form (_live_identity normalizes the same way).
            if row is None or str(row[0]) != candidate.system_identifier:
                raise RestoreProofError("restored system identifier differs")
            if _migration_hash(conn) != candidate.migration_set_sha256:
                raise RestoreProofError("restored migration set differs")
            evidence: list[str] = [candidate.system_identifier, candidate.migration_set_sha256]
            evidence.extend(_smoke_samples(cur))
            cur.execute(
                "SELECT n.nspname, c.relname, c.relkind, "
                "pg_get_userbyid(c.relowner) FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema' "
                "ORDER BY 1,2,3"
            )
            evidence.extend("|".join(str(value) for value in row) for row in cur.fetchall())
            return hashlib.sha256("\n".join(evidence).encode()).hexdigest()

    def _stop(
        self, pgdata: Path, identity: SandboxPostgresIdentity, process: subprocess.Popen[bytes]
    ) -> None:
        if Path(identity.data_directory) != pgdata.resolve():
            raise RestoreProofError("refusing to stop PostgreSQL outside the restore sandbox")
        # Signal the captured native postmaster directly. pg_ctl would reread a
        # mutable pid file and create a second authority for this owned child.
        _stop_sandbox(identity, process)
        if _matching_sandbox(identity) is not None:
            raise RestoreProofError("sandbox PostgreSQL could not be reaped")
