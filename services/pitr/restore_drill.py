"""Operator-driven isolated restore drill for one protected backup chain.

The scheduled restore proof (``restore_proof.prove_candidate``) restores a chain
to its recorded end LSN and publishes proof evidence. An operator drill is a
different action: it restores a chain to an arbitrary target LSN the operator
picked, runs the acceptance criteria of ``pitr-wsl-drill-criteria.md`` against
the sandbox and keeps the scratch tree as raw evidence. It publishes nothing
and never touches the write path.

The flow composes the same-source modules as the scheduled proof: the
``restore_proof`` download helpers, ``base_restore_crypto`` for base
authentication and extraction, ``restore_manifest`` for the WAL allowlist and
``restore_postgres`` for the sandbox postmaster. Two bugs from the 2026-09-13
WSL drill (the ad-hoc driver this module solidifies) are hardened into
invariants here:

- the sandbox pgdata is the *return value* of ``extract_authenticated_base``
  and is never re-derived from the directory layout;
- the evidence tables come from ``DRILL_TABLES`` -- ``agent_tasks``, not
  ``tasks``.

Like the restore proof, the drill must run as its process-group leader: the
sandbox postmaster rides in the drill's process group so a crashed drill stays
reapable by a single group signal. ``ava pitr drill`` refuses to start
otherwise and prints the ``setsid`` re-run.
"""

from __future__ import annotations

import dataclasses
import json
import os
import socket
import subprocess
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import psutil
import psycopg
from psycopg import sql

from services.pitr.base_manifest import CandidateManifest, WalRange, _lsn
from services.pitr.base_restore_crypto import (
    authenticate_base_ciphertext,
    extract_authenticated_base,
)
from services.pitr.restore_manifest import required_archive_names, wal_objects_from_acks
from services.pitr.restore_object_store import GenerationPinnedObjectReader
from services.pitr.restore_postgres import (
    SandboxPostgresIdentity,
    _append_recovery_config,
    _free_port,
    _live_identity,
    _matching_sandbox,
    _run,
    _spawn_sandbox_postgres,
    _stop_process_tree,
    _wait_for_sandbox_identity,
    _write_sandbox_config,
)
from services.pitr.restore_proof import (
    LivePostgresIdentity,
    RestoreProofError,
    _base_restore_object,
    _download_wal,
)

_TASKS_TABLE = "agent_tasks"
"""The task table's real name; ``tasks`` does not exist (driver bug B2)."""

DRILL_TABLES: tuple[str, ...] = (
    "inbound_messages",
    _TASKS_TABLE,
    "agents",
    "checkpoints",
)
"""The tables every drill counts and probes for availability."""

_TARGET_WALL_TOLERANCE = timedelta(minutes=5)
"""The timestamp-form tolerance from the drill criteria, section 3."""

_STOP_LINE_MARKERS = (
    "recovery stopping",
    "last completed transaction",
    "redo done",
    "consistent recovery state",
)


class DrillError(RuntimeError):
    """The drill cannot continue or failed an acceptance criterion."""


def parse_target_wall(value: str) -> datetime:
    """Parse the target wall clock; an explicit UTC offset is required.

    ``2026-09-13 13:13:03+08`` is valid; a bare abbreviation such as ``CST`` is
    not -- PostgreSQL reads it as -06:00 (driver bug B3), and a naive value
    cannot gate the double-face criterion.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DrillError(f"target wall {value!r} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DrillError(
            f"target wall {value!r} carries no UTC offset; write it with an explicit "
            "offset such as '2026-09-13 13:13:03+08' (a bare zone abbreviation is "
            "ambiguous to PostgreSQL)"
        )
    return parsed


@dataclass(frozen=True)
class DrillRequest:
    """Resolved inputs for one operator drill."""

    candidate: CandidateManifest
    reader: GenerationPinnedObjectReader
    key: bytes
    ack_dir: Path
    scratch: Path
    target_lsn: str
    target_wall: datetime
    pg_ctl: Path
    pg_verifybackup: Path
    live_db_url: str
    data_directory: str
    timeout_seconds: int = 1800


@dataclass
class DrillEvidence:
    """The evidence bundle written to ``<scratch>/drill-evidence.json``."""

    chain_id: str
    target_lsn: str
    target_wall: str
    scratch: str
    started_at: str
    finished_at: str | None = None
    outcome: str = "running"
    error: str | None = None
    timings: dict[str, float] = field(default_factory=dict[str, float])
    criteria: dict[str, Any] = field(default_factory=dict[str, Any])
    steps: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def note(self, kind: str, payload: Any) -> None:
        self.steps.append({"at": datetime.now(UTC).isoformat(), "kind": kind, "payload": payload})

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), indent=1, sort_keys=True, default=str)


def run_restore_drill(
    request: DrillRequest, *, progress: Callable[[str], None] | None = None
) -> DrillEvidence:
    """Run one full drill and return its evidence; raises DrillError on failure.

    The scratch tree is kept on every outcome -- it carries the downloaded
    quarantine, the sandbox log and ``drill-evidence.json``. The sandbox
    postmaster is always stopped before this function returns or raises.
    """
    say = progress if progress is not None else _discard_progress
    _require_group_leader()
    _require_fresh_scratch(request.scratch)
    _require_target_lsn(request.candidate, request.target_lsn)
    evidence = DrillEvidence(
        chain_id=request.candidate.chain_id,
        target_lsn=request.target_lsn,
        target_wall=request.target_wall.isoformat(),
        scratch=str(request.scratch),
        started_at=datetime.now(UTC).isoformat(),
    )
    evidence.note("scratch", str(request.scratch))
    evidence.note("chain", request.candidate.chain_id)
    evidence.note("target_lsn", request.target_lsn)
    evidence.note("target_wall", request.target_wall.isoformat())
    error: BaseException | None = None
    try:
        live_before = _live_identity(request.live_db_url, request.data_directory)
        evidence.criteria["live_before"] = dataclasses.asdict(live_before)
        say(f"live identity recorded: {live_before.system_identifier}")
        pgdata = _prepare_pgdata(request, evidence, say)
        _run_sandbox(request, evidence, pgdata, live_before, say)
    except BaseException as exc:
        error = exc
    finally:
        evidence.finished_at = datetime.now(UTC).isoformat()
        failures = _acceptance_failures(evidence)
        if failures:
            evidence.criteria["failures"] = failures
            if error is None:
                error = DrillError("acceptance criteria failed: " + "; ".join(failures))
        if error is None:
            evidence.outcome = "pass"
        else:
            evidence.outcome = "fail"
            evidence.error = f"{type(error).__name__}: {error}"
        _write_evidence(request.scratch, evidence)
    if error is not None:
        raise error
    say(f"drill passed; evidence at {request.scratch / 'drill-evidence.json'}")
    return evidence


def _prepare_pgdata(
    request: DrillRequest, evidence: DrillEvidence, say: Callable[[str], None]
) -> Path:
    """Download, authenticate, extract the base and stage the WAL allowlist."""
    scratch = request.scratch
    quarantine = scratch / "quarantine"
    quarantine.mkdir(mode=0o700)
    base = _base_restore_object(request.candidate)
    evidence.note(
        "base_object", {"name": base.object_name, "size": base.size, "pin": base.pin_token}
    )
    say(f"downloading base object ({base.size} bytes)")
    ciphertext = quarantine / "base.enc"
    started = time.monotonic()
    request.reader.download_exact(base, ciphertext)
    evidence.timings["base_download_seconds"] = round(time.monotonic() - started, 1)
    authenticate_base_ciphertext(ciphertext, key=request.key, expected=base)
    say("base authenticated")
    started = time.monotonic()
    pgdata = extract_authenticated_base(
        ciphertext,
        scratch / "sandbox",
        key=request.key,
        expected=base,
        candidate_sha256=request.candidate.base_object.source_sha256,
        native_manifest_sha256=request.candidate.native_manifest_sha256,
        max_extracted_bytes=request.candidate.base_object.source_size,
    )
    evidence.timings["base_extract_seconds"] = round(time.monotonic() - started, 1)
    evidence.note("pgdata", str(pgdata))
    say(f"base extracted to {pgdata}")
    wal_range = WalRange(
        timeline=request.candidate.timeline,
        start_lsn=request.candidate.start_lsn,
        end_lsn=request.target_lsn,
    )
    names = required_archive_names((wal_range,), request.candidate.wal_segment_size)
    objects = wal_objects_from_acks(ack_dir=request.ack_dir, archive_names=names)
    evidence.note("wal_objects", {"count": len(names), "first": names[0], "last": names[-1]})
    say(f"downloading {len(names)} WAL segments")
    started = time.monotonic()
    _download_wal(
        reader=request.reader,
        objects=objects,
        encrypted_dir=quarantine / "wal",
        wal_dir=scratch / "archive",
        key=request.key,
        candidate=request.candidate,
    )
    evidence.timings["wal_download_seconds"] = round(time.monotonic() - started, 1)
    _run([str(request.pg_verifybackup), "--no-parse-wal", str(pgdata)], timeout=6 * 3600)
    evidence.note("pg_verifybackup", "ok")
    say("backup manifest verified")
    return pgdata


def _run_sandbox(
    request: DrillRequest,
    evidence: DrillEvidence,
    pgdata: Path,
    live_before: LivePostgresIdentity,
    say: Callable[[str], None],
) -> None:
    """Start the sandbox at the target LSN, run the criteria, then tear down."""
    scratch = request.scratch
    socket_dir = scratch / "socket"
    socket_dir.mkdir(mode=0o700)
    port = _free_port()
    _append_recovery_config(pgdata, scratch / "archive", socket_dir, request.target_lsn, scratch)
    config = _write_sandbox_config(pgdata, socket_dir, port, scratch)
    sandbox_log = scratch / "sandbox-postgres.log"
    postgres = request.pg_ctl.parent / "postgres"
    process = _spawn_sandbox_postgres(postgres, pgdata, config, sandbox_log)
    sandbox: SandboxPostgresIdentity | None = None
    try:
        started = time.monotonic()
        sandbox = _wait_for_sandbox_identity(process, pgdata, sandbox_log, request.timeout_seconds)
        if sandbox.pgid != os.getpgrp():
            raise DrillError("sandbox PostgreSQL escaped the drill process group")
        evidence.criteria["sandbox"] = {
            "pid": sandbox.pid,
            "pgid": sandbox.pgid,
            "port": port,
            "seconds": round(time.monotonic() - started, 1),
        }
        say(f"sandbox postmaster up (pid {sandbox.pid}, port {port}); replaying to target")
        _wait_for_promotion(request, process, sandbox_log, socket_dir, port, evidence)
        evidence.criteria["stop_lines"] = _stop_lines(sandbox_log)
        _collect_criteria(request, evidence, socket_dir, port)
        live_after = _live_identity(request.live_db_url, request.data_directory)
        evidence.criteria["live_after"] = dataclasses.asdict(live_after)
        evidence.criteria["live_unchanged"] = live_before == live_after
    finally:
        _teardown_sandbox(request, evidence, pgdata, sandbox, process, port, say)


def _wait_for_promotion(
    request: DrillRequest,
    process: subprocess.Popen[str],
    sandbox_log: Path,
    socket_dir: Path,
    port: int,
    evidence: DrillEvidence,
) -> None:
    dsn = f"postgresql://?host={socket_dir}&port={port}&dbname=postgres"
    deadline = time.monotonic() + request.timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise DrillError(
                f"sandbox postmaster exited {process.returncode} before promotion: "
                f"{_log_tail(sandbox_log)}"
            )
        try:
            with psycopg.connect(dsn, connect_timeout=2) as conn, conn.cursor() as cur:
                cur.execute("SELECT pg_is_in_recovery(), pg_last_wal_replay_lsn()::text")
                row = cur.fetchone()
                if row is not None and row[0] is False and row[1] is not None:
                    achieved = str(row[1])
                    if _lsn(achieved) >= _lsn(request.target_lsn):
                        evidence.note(
                            "promoted", {"replay_lsn": achieved, "target": request.target_lsn}
                        )
                        return
        except psycopg.Error as exc:
            last_error = exc
        time.sleep(0.25)
    raise DrillError("sandbox did not promote at the target LSN") from last_error


def _collect_criteria(
    request: DrillRequest, evidence: DrillEvidence, socket_dir: Path, port: int
) -> None:
    candidate = request.candidate
    dsn = f"postgresql://?host={socket_dir}&port={port}&dbname={candidate.database_name}"
    with psycopg.connect(dsn, connect_timeout=5) as conn, conn.cursor() as cur:
        cur.execute("SELECT system_identifier::text FROM pg_control_system()")
        row = cur.fetchone()
        identifier = str(row[0]) if row is not None else ""
        cur.execute("SELECT current_setting('server_version_num')")
        row = cur.fetchone()
        version = int(str(row[0])) if row is not None else 0
        cur.execute("SELECT timeline_id::text FROM pg_control_checkpoint()")
        row = cur.fetchone()
        timeline = str(row[0]) if row is not None else ""
        cur.execute("SELECT current_database()")
        row = cur.fetchone()
        evidence.criteria["identity"] = {
            "system_identifier": identifier,
            "server_version_num": version,
            "server_version_major": version // 10000,
            "timeline": timeline,
            "database": str(row[0]) if row is not None else "",
            "expected_system_identifier": candidate.system_identifier,
            "expected_major": candidate.postgres_major,
            "expected_timeline": candidate.timeline,
        }
        wall = request.target_wall
        cur.execute(
            "SELECT count(*) FROM inbound_messages "
            "WHERE created_at > %s::timestamptz - interval '1 hour' "
            "AND created_at <= %s::timestamptz",
            (wall, wall),
        )
        row = cur.fetchone()
        recent = int(row[0]) if row is not None else 0
        cur.execute("SELECT max(created_at)::text FROM inbound_messages")
        row = cur.fetchone()
        latest = None if row is None or row[0] is None else str(row[0])
        cur.execute(
            "SELECT coalesce(max(created_at) <= %s::timestamptz + interval '5 minutes', false) "
            "FROM inbound_messages",
            (wall,),
        )
        row = cur.fetchone()
        within_tolerance = bool(row[0]) if row is not None else False
        evidence.criteria["double_face"] = {
            "recent_count": recent,
            "max_created_at": latest,
            "within_tolerance": within_tolerance,
            "tolerance_seconds": int(_TARGET_WALL_TOLERANCE.total_seconds()),
        }
        evidence.criteria["counts_restored"] = {table: _count(cur, table) for table in DRILL_TABLES}
        cur.execute(
            sql.SQL(
                "SELECT id::text, status, left(title, 40) FROM {} ORDER BY id DESC LIMIT 3"
            ).format(sql.Identifier(_TASKS_TABLE))
        )
        evidence.criteria["business_rows"] = [
            [str(value) for value in row] for row in cur.fetchall()
        ]
    with psycopg.connect(request.live_db_url, connect_timeout=5) as conn, conn.cursor() as cur:
        evidence.criteria["counts_live"] = {table: _count(cur, table) for table in DRILL_TABLES}


def _count(cur: psycopg.Cursor[tuple[Any, ...]], table: str) -> int:
    cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
    row = cur.fetchone()
    return int(row[0]) if row is not None else 0


def _teardown_sandbox(
    request: DrillRequest,
    evidence: DrillEvidence,
    pgdata: Path,
    sandbox: SandboxPostgresIdentity | None,
    process: subprocess.Popen[str],
    port: int,
    say: Callable[[str], None],
) -> None:
    teardown: dict[str, object] = {"stopped": False, "error": None}
    try:
        if sandbox is not None:
            _stop_sandbox(request.pg_ctl, pgdata, sandbox)
        teardown["stopped"] = True
        say("sandbox stopped")
    except Exception as exc:  # the residue scan below is the backstop
        teardown["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        with suppress(Exception):
            process.wait(timeout=10)
        evidence.criteria["teardown"] = teardown
        evidence.criteria["residue"] = _residue_scan(request.scratch, port, pgdata)


def _stop_sandbox(pg_ctl: Path, pgdata: Path, sandbox: SandboxPostgresIdentity) -> None:
    """Stop the sandbox; mirrors IsolatedPostgresRestoreExecutor._stop."""
    if Path(sandbox.data_directory) != pgdata.resolve():
        raise DrillError("refusing to stop PostgreSQL outside the drill sandbox")
    with suppress(RestoreProofError):
        _run(
            [str(pg_ctl), "-D", str(pgdata), "-m", "fast", "-w", "stop"],
            timeout=30,
        )
    _stop_process_tree(sandbox)
    if _matching_sandbox(sandbox) is not None:
        raise DrillError("sandbox PostgreSQL could not be reaped")


def _residue_scan(scratch: Path, port: int, pgdata: Path) -> dict[str, Any]:
    processes: list[str] = []
    for process in psutil.process_iter(["cmdline"]):
        with suppress(psutil.Error):
            raw_cmdline = process.info["cmdline"]
            if isinstance(raw_cmdline, list):
                cmdline = [str(part) for part in cast("list[object]", raw_cmdline)]
                if any(str(scratch) in part for part in cmdline):
                    processes.append(" ".join(cmdline))
    listening = False
    with socket.socket() as probe:
        probe.settimeout(1.0)
        listening = probe.connect_ex(("127.0.0.1", port)) == 0
    return {
        "processes": processes,
        "port_listening": listening,
        "pid_file_present": (pgdata / "postmaster.pid").exists(),
    }


def _acceptance_failures(evidence: DrillEvidence) -> list[str]:
    criteria = evidence.criteria
    failures: list[str] = []
    identity = criteria.get("identity")
    if identity is None:
        failures.append("identity check did not run")
    else:
        if identity["system_identifier"] != identity["expected_system_identifier"]:
            failures.append(
                f"restored system identifier {identity['system_identifier']} differs from "
                f"{identity['expected_system_identifier']}"
            )
        if identity["server_version_major"] != identity["expected_major"]:
            failures.append(
                f"restored major {identity['server_version_major']} differs from "
                f"{identity['expected_major']}"
            )
    double_face = criteria.get("double_face")
    if double_face is None:
        failures.append("double-face check did not run")
    else:
        if double_face["recent_count"] <= 0:
            failures.append("no inbound_message in the hour before the target wall clock")
        if not double_face["within_tolerance"]:
            failures.append("an inbound_message lands after the target wall clock plus tolerance")
    if not criteria.get("live_unchanged"):
        failures.append("live PostgreSQL identity changed while the sandbox was running")
    if "counts_restored" not in criteria:
        failures.append("restored table counts were not collected")
    teardown: dict[str, object] = criteria.get("teardown") or {}
    if not teardown.get("stopped"):
        failures.append("sandbox teardown did not complete")
    residue: dict[str, object] = criteria.get("residue") or {}
    if residue.get("processes"):
        failures.append(f"processes remain under the scratch tree: {residue['processes']}")
    if residue.get("port_listening"):
        failures.append("the sandbox port still accepts connections")
    if residue.get("pid_file_present"):
        failures.append("the sandbox pid file remains")
    return failures


def _require_group_leader() -> None:
    if os.getpgrp() != os.getpid():
        raise DrillError(
            "the restore drill must run as its process-group leader so the sandbox postmaster "
            "shares the group and a crashed drill stays reapable; re-run it under setsid"
        )


def _require_fresh_scratch(scratch: Path) -> None:
    if scratch.exists():
        if not scratch.is_dir() or any(scratch.iterdir()):
            raise DrillError(
                f"drill scratch {scratch} is not fresh; it must be absent or an empty directory "
                "(the previous evidence tree is never overwritten)"
            )
    else:
        scratch.mkdir(parents=True, mode=0o700)


def _require_target_lsn(candidate: CandidateManifest, target_lsn: str) -> None:
    try:
        position = _lsn(target_lsn)
    except ValueError as exc:
        raise DrillError(str(exc)) from exc
    if position < _lsn(candidate.start_lsn):
        raise DrillError(f"target LSN {target_lsn} precedes the chain start {candidate.start_lsn}")


def _write_evidence(scratch: Path, evidence: DrillEvidence) -> None:
    path = scratch / "drill-evidence.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(evidence.to_json())


def _log_tail(path: Path, limit: int = 4000) -> str:
    try:
        return path.read_text(errors="replace")[-limit:]
    except OSError:
        return ""


def _stop_lines(sandbox_log: Path) -> list[str]:
    text = _log_tail(sandbox_log, limit=200_000)
    return [
        line for line in text.splitlines() if any(marker in line for marker in _STOP_LINE_MARKERS)
    ][-8:]


def _discard_progress(_message: str) -> None:
    """Do nothing; the default progress sink for library callers."""
