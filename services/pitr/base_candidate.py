"""Create and publish one unprotected weekly physical base candidate."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import psutil
import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from services.backup import backup_lock
from services.pitr.base_manifest import (
    SCHEMA_VERSION,
    CandidateManifest,
    base_object_from_ack,
    parse_native_manifest,
)
from services.pitr.base_object_store import RestartableStreamingObjectStore
from services.pitr.base_stream import BASE_MAGIC, load_or_create_source, snapshot_candidate
from services.pitr.checksums import CRC32C, KNOWN_CHECKSUM_ALGOS
from services.pitr.space_budget import CandidateSpaceBudget, require_candidate_space
from shared.db import direct_db_url
from shared.pg_tools import pg_tool


class BaseCandidateError(RuntimeError):
    pass


class StopSignal(Protocol):
    def is_set(self) -> bool: ...

    def wait(self, timeout: float | None = None) -> bool: ...


_PARTIAL_NAME = re.compile(r"^\.(?:\d{8}T\d{6}Z|activation-\d{8}T\d{6}Z-[0-9a-f-]{36})\.partial$")


@dataclass(frozen=True)
class CandidateFacts:
    postgres_major: int
    system_identifier: str
    wal_segment_size: int
    timeline: int
    migration_set_sha256: str
    database_name: str


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    staged = Path(raw)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, sort_keys=True, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        staged.replace(path)
        _fsync_dir(path.parent)
    finally:
        staged.unlink(missing_ok=True)


def _migration_set_sha256(db_url: str) -> str:
    with psycopg.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT name FROM schema_migrations ORDER BY name")
        names = [str(row[0]) for row in cur.fetchall()]
    return hashlib.sha256("\n".join(names).encode()).hexdigest()


def _server_facts(db_url: str) -> tuple[int, str, int, int, str]:
    with psycopg.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT current_setting('server_version_num'), system_identifier, "
            "timeline_id, bytes_per_wal_segment, current_database() FROM pg_control_system(), "
            "pg_control_checkpoint(), pg_control_init()"
        )
        row = cur.fetchone()
        if row is None:
            raise BaseCandidateError("PostgreSQL omitted physical backup identity")
        version, system_id, timeline, wal_segment_size, database_name = row
        cur.execute(
            "SELECT spcname FROM pg_tablespace "
            "WHERE spcname NOT IN ('pg_default', 'pg_global') ORDER BY spcname"
        )
        custom_tablespaces = [str(item[0]) for item in cur.fetchall()]
    if custom_tablespaces:
        raise BaseCandidateError(
            f"custom tablespaces are not supported: {', '.join(custom_tablespaces)}"
        )
    return (
        int(version) // 10000,
        str(system_id),
        int(wal_segment_size),
        int(timeline),
        str(database_name),
    )


def _passwordless_conninfo(db_url: str) -> tuple[str, str]:
    parsed = conninfo_to_dict(db_url)
    password = parsed.pop("password", "")
    return make_conninfo(**{key: str(value) for key, value in parsed.items()}), str(password)


def _validate_replication_contract(db_url: str, replication_db_url: str) -> None:
    primary = conninfo_to_dict(db_url)
    replication = conninfo_to_dict(replication_db_url)
    if str(primary.get("port", "")) != str(replication.get("port", "")):
        raise BaseCandidateError("replication URL does not target this cluster's Postgres port")
    with psycopg.connect(replication_db_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT rolreplication, rolsuper FROM pg_roles WHERE rolname = current_user")
        row = cur.fetchone()
    if row != (True, False):
        raise BaseCandidateError("PITR replication role must be REPLICATION and NOSUPERUSER")
    _validate_replication_hba(replication)


def _rule_name_set(value: str | list[str] | None) -> frozenset[str]:
    """One `pg_hba_file_rules` `database`/`user_name` cell as a frozenset of
    bare names. psycopg returns the text[] columns as Python lists; the psql
    `{...}` set-literal form is accepted too (and used by mocks)."""
    if value is None:
        return frozenset()
    if isinstance(value, list):
        return frozenset(str(name).strip().strip('"') for name in value)
    return frozenset(name.strip().strip('"') for name in value.strip("{}").split(","))


def _validate_replication_hba(replication: Mapping[str, object]) -> None:
    """pg_basebackup dials as a PHYSICAL replication connection (dbname=
    `replication`), which matches only pg_hba rules whose database field is the
    literal `replication` keyword — `all` does not cover it. The 2026-08-30
    activation died here: every normal-connection probe passed while
    pg_basebackup exited 1 with "no pg_hba.conf entry for replication
    connection". Fail closed BEFORE the backup when no loaded rule covers the
    PITR role, so the operator sees an actionable cause instead of a bare exit
    code."""
    from services.pitr.activation_runtime import pitr_admin_url

    role = str(replication.get("user") or "")
    host = str(replication.get("host") or "")
    try:
        with psycopg.connect(pitr_admin_url()) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT database, user_name, error FROM pg_hba_file_rules "
                "WHERE type <> 'local' AND address IS NOT NULL"
            )
            rules = cur.fetchall()
    except BaseException as exc:
        raise BaseCandidateError(
            f"cannot verify the replication pg_hba rule for {role}: {type(exc).__name__}"
        ) from exc
    for database_cell, user_cell, error_cell in rules:
        if error_cell is not None:
            # A row with a parse error is NOT in effect, though the view still
            # lists it — it must never count as coverage (QA #1096 P2).
            continue
        if "replication" not in _rule_name_set(database_cell):
            continue
        users = _rule_name_set(user_cell)
        if "all" in users or role in users:
            return
    raise BaseCandidateError(
        f"pg_hba.conf has no physical-replication rule for PITR role {role} "
        f"(pg_basebackup dials host={host or '(socket)'}); the data-plane "
        "renderer must emit a `host replication <role> ...` row — regenerate "
        "pg_hba.conf via `ava start` or `ava cluster update`"
    )


def _matching_process(pid: int, created_at: float, expected_token: str) -> psutil.Process | None:
    try:
        process = psutil.Process(pid)
        if abs(process.create_time() - created_at) >= 0.01:
            return None
        if expected_token not in " ".join(process.cmdline()):
            return None
        return process
    except psutil.AccessDenied as exc:
        raise BaseCandidateError("cannot verify base candidate owner identity") from exc
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return None


def _remove_tree(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise BaseCandidateError(f"refusing to remove unexpected candidate path: {path.name}")
    shutil.rmtree(path)
    _fsync_dir(path.parent)


def _recover_owned_partials(root: Path) -> None:
    candidates = root / "base-candidates"
    if not candidates.exists():
        return
    for partial in candidates.glob(".*.partial"):
        if not _PARTIAL_NAME.fullmatch(partial.name):
            raise BaseCandidateError(f"unknown base candidate partial: {partial.name}")
        chain_id = partial.name.removeprefix(".").removesuffix(".partial")
        owner = root / "base-facts" / f"{chain_id}.owner.json"
        if not owner.is_file():
            raise BaseCandidateError(f"base candidate partial lacks owner evidence: {partial.name}")
        try:
            evidence = json.loads(owner.read_text())
            state = str(evidence["state"])
            deadline = float(evidence["deadline"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise BaseCandidateError("invalid base candidate owner evidence") from exc
        if state == "spawning":
            if time.time() < deadline:
                raise BaseCandidateError(f"base candidate spawn is unresolved for {chain_id}")
        elif state == "running":
            try:
                pid = int(evidence["pid"])
                pgid = int(evidence["pgid"])
                created_at = float(evidence["created_at"])
                expected_token = str(evidence["expected_token"])
            except (KeyError, TypeError, ValueError) as exc:
                raise BaseCandidateError("invalid running capture evidence") from exc
            process = _matching_process(pid, created_at, expected_token)
            if process is not None:
                if time.time() < deadline:
                    raise BaseCandidateError(f"base candidate owner is still active for {chain_id}")
                _stop_owned_group(process, pgid)
        else:
            raise BaseCandidateError("unknown base candidate owner state")
        _remove_tree(partial)
        owner.unlink()
        _fsync_dir(owner.parent)


def _stop_owned_group(leader: psutil.Process, pgid: int) -> None:
    try:
        if os.getpgid(leader.pid) != pgid or pgid == os.getpgrp():
            raise BaseCandidateError("refusing to signal an unowned backup process group")
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 20
    with suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGTERM)
    grace_end = min(deadline, time.monotonic() + 5)
    while _group_members(pgid) and time.monotonic() < grace_end:
        time.sleep(0.1)
    while _group_members(pgid) and time.monotonic() < deadline:
        with suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGKILL)
        time.sleep(0.1)
    if _group_members(pgid):
        raise BaseCandidateError("backup process group retained live descendants")


def _group_members(pgid: int) -> list[psutil.Process]:
    members: list[psutil.Process] = []
    for process in psutil.process_iter(["pid"]):
        try:
            if os.getpgid(process.pid) == pgid:
                members.append(process)
        except (ProcessLookupError, PermissionError, psutil.NoSuchProcess):
            continue
    return members


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    try:
        leader = psutil.Process(process.pid)
    except psutil.NoSuchProcess:
        process.wait()
        return
    owned: dict[tuple[int, float], psutil.Process] = {
        (member.pid, member.create_time()): member
        for member in [leader, *leader.children(recursive=True)]
    }
    for member in reversed(list(owned.values())):
        with suppress(psutil.NoSuchProcess):
            member.terminate()
    deadline = time.monotonic() + 20
    alive = list(owned.values())
    while alive and time.monotonic() < deadline:
        with suppress(psutil.NoSuchProcess):
            for member in leader.children(recursive=True):
                owned[(member.pid, member.create_time())] = member
        _, alive = psutil.wait_procs(
            list(owned.values()), timeout=min(0.25, max(0, deadline - time.monotonic()))
        )
        if time.monotonic() + 5 >= deadline:
            for member in alive:
                with suppress(psutil.NoSuchProcess):
                    member.kill()
    if alive:
        raise BaseCandidateError("backup subprocess tree retained live descendants")
    try:
        process.wait(timeout=0)
    except subprocess.TimeoutExpired as exc:
        raise BaseCandidateError("backup process leader was not reaped") from exc


def _output_suffix(stdout: bytes | None, stderr: bytes | None) -> str:
    """The bounded, decoded tails of a failed child's stdout and stderr — the
    part of the failure a bare exit code hides (2026-08-30: "pg_basebackup
    exited 1" was the whole record; the actual FATAL "no pg_hba.conf entry for
    replication connection" sat in a DEVNULL'd pipe and stdout was discarded
    with it)."""
    tails = [
        stream.decode("utf-8", errors="replace").strip()[-1600:]
        for stream in (stdout, stderr)
        if stream
    ]
    tails = [tail for tail in tails if tail]
    return f": {' | '.join(tails)}" if tails else ""


def _run_capture(command: list[str], *, env: dict[str, str], owner: Path, stop: StopSignal) -> None:
    deadline = time.time() + 6 * 3600
    wrapped = [
        sys.executable,
        "-m",
        "services.pitr.capture_exec",
        "--owner",
        str(owner),
        "--deadline",
        str(deadline),
        "--",
        *command,
    ]
    # Both pipes are drained only at exit (communicate below), not during the
    # poll loop: without --progress, pg_basebackup emits far less than the
    # 64 KiB pipe buffer, so the child cannot block on a full pipe in
    # practice. Output beyond the buffer would only surface as the six-hour
    # bound firing with the drained tail in the message — accepted over a
    # reader thread, which this diagnostic path does not need.
    process = subprocess.Popen(  # noqa: S603
        wrapped,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=False,
    )
    while process.poll() is None:
        if stop.wait(0.25):
            _stop_process(process)
            stdout, stderr = process.communicate()
            raise BaseCandidateError(
                f"base candidate capture was stopped{_output_suffix(stdout, stderr)}"
            )
        if time.time() >= deadline:
            _stop_process(process)
            stdout, stderr = process.communicate()
            raise BaseCandidateError(
                f"pg_basebackup exceeded its six-hour bound{_output_suffix(stdout, stderr)}"
            )
    stdout, stderr = process.communicate()
    if process.returncode != 0:
        raise BaseCandidateError(
            f"pg_basebackup exited {process.returncode}{_output_suffix(stdout, stderr)}"
        )


def _birth_candidate(
    *,
    root: Path,
    chain_id: str,
    budget: CandidateSpaceBudget,
    db_url: str,
    replication_db_url: str,
    stop: StopSignal,
) -> tuple[Path, CandidateFacts]:
    partial = root / "base-candidates" / f".{chain_id}.partial"
    ready = root / "base-candidates" / f"{chain_id}.ready"
    facts_path = root / "base-facts" / f"{chain_id}.json"
    owner_path = root / "base-facts" / f"{chain_id}.owner.json"
    conninfo, password = _passwordless_conninfo(replication_db_url)
    with backup_lock(timeout_s=0):
        partial.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        (root / "base-facts").mkdir(parents=True, exist_ok=True, mode=0o700)
        _recover_owned_partials(root)
        require_candidate_space(partial.parent, budget)
        _validate_replication_contract(db_url, replication_db_url)
        postgres_major, system_id, wal_segment_size, timeline, database_name = _server_facts(db_url)
        facts = CandidateFacts(
            postgres_major,
            system_id,
            wal_segment_size,
            timeline,
            _migration_set_sha256(db_url),
            database_name,
        )
        current = psutil.Process()
        _atomic_json(
            owner_path,
            {
                "state": "spawning",
                "pid": current.pid,
                "created_at": current.create_time(),
                "deadline": time.time() + 30,
            },
        )
        partial.mkdir(mode=0o700)
        command = [
            str(pg_tool("pg_basebackup")),
            "-Fp",
            "-X",
            "none",
            "--manifest-checksums=SHA256",
            "--checkpoint=spread",
            "--no-password",
            "--label",
            f"ava-pitr-{chain_id}",
            "--pgdata",
            str(partial),
            "--dbname",
            conninfo,
        ]
        try:
            _run_capture(
                command,
                env={"PGPASSWORD": password} if password else {},
                owner=owner_path,
                stop=stop,
            )
            _verify_candidate(partial, stop)
            _atomic_json(facts_path, asdict(facts))
            partial.replace(ready)
            _fsync_dir(ready.parent)
        except BaseException as exc:
            if partial.exists():
                try:
                    _remove_tree(partial)
                except BaseException:
                    evidence = json.loads(owner_path.read_text())
                    evidence.update({"state": "cleanup_failed", "error": str(exc)})
                    _atomic_json(owner_path, evidence)
                    raise
            facts_path.unlink(missing_ok=True)
            raise
        finally:
            if ready.exists() or not partial.exists():
                owner_path.unlink(missing_ok=True)
    return ready, facts


def _verify_candidate(path: Path, stop: StopSignal) -> None:
    # Same pipe-bound acceptance as _run_capture: pg_verifybackup's report is
    # a few lines, far below the pipe buffer, drained at exit.
    verify = subprocess.Popen(  # noqa: S603
        [str(pg_tool("pg_verifybackup")), "--no-parse-wal", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=False,
    )
    deadline = time.monotonic() + 6 * 3600
    while verify.poll() is None:
        if stop.wait(0.25):
            _stop_process(verify)
            stdout, stderr = verify.communicate()
            raise BaseCandidateError(
                f"base candidate verification was stopped{_output_suffix(stdout, stderr)}"
            )
        if time.monotonic() >= deadline:
            _stop_process(verify)
            stdout, stderr = verify.communicate()
            raise BaseCandidateError(
                f"pg_verifybackup exceeded its six-hour bound{_output_suffix(stdout, stderr)}"
            )
    stdout, stderr = verify.communicate()
    if verify.returncode != 0:
        raise BaseCandidateError(
            f"pg_verifybackup exited {verify.returncode}{_output_suffix(stdout, stderr)}"
        )


def _load_facts(root: Path, chain_id: str) -> CandidateFacts:
    path = root / "base-facts" / f"{chain_id}.json"
    try:
        return CandidateFacts(**json.loads(path.read_text()))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BaseCandidateError("base candidate lacks valid capture-time facts") from exc


def reconcile_completed_candidates(root: Path, *, key: bytes, key_id: str) -> None:
    candidates = root / "base-candidates"
    if not candidates.exists():
        return
    for ready in candidates.glob("*.ready"):
        chain_id = ready.name.removesuffix(".ready")
        manifest = root / "base-manifests" / f"{chain_id}.candidate.json"
        if not manifest.is_file():
            continue
        candidate = CandidateManifest.from_json(manifest.read_text())
        plan_path = root / "base-plans" / f"{chain_id}.plan.json"
        if not plan_path.is_file():
            raise BaseCandidateError("completed candidate lacks its encryption plan")
        source, plan = load_or_create_source(
            ready,
            plan_path=plan_path,
            key=key,
            key_id=key_id,
            object_name=candidate.base_object.object_name,
        )
        facts = _load_facts(root, chain_id)
        system_id, start_lsn, wal_ranges = parse_native_manifest(ready / "backup_manifest")
        _, candidate_sha = snapshot_candidate(ready)
        if (
            candidate.chain_id != chain_id
            or candidate.base_object.source_sha256 != candidate_sha
            or plan.candidate_sha256 != candidate_sha
            or candidate.base_object.object_name != plan.object_name
            or candidate.base_object.ciphertext_size != plan.ciphertext_size
            # The local plan pins CRC32C; a backend whose ACK verifies a
            # different algorithm reconciled at upload time via read-back,
            # so only the crc32c vocabulary is compared here.
            or candidate.base_object.ciphertext_checksum_algo not in KNOWN_CHECKSUM_ALGOS
            or (
                candidate.base_object.ciphertext_checksum_algo == CRC32C
                and candidate.base_object.ciphertext_checksum_value != plan.ciphertext_crc32c
            )
            or candidate.base_object.ciphertext_crc32c != plan.ciphertext_crc32c
            or candidate.base_object.key_id != plan.key_id
            or candidate.native_manifest_sha256 != plan.native_manifest_sha256
            or candidate.native_manifest_container_object_name != plan.object_name
            or candidate.native_manifest_container_pin_token != candidate.base_object.pin_token
            or candidate.postgres_major != facts.postgres_major
            or candidate.system_identifier != facts.system_identifier
            or candidate.wal_segment_size != facts.wal_segment_size
            or candidate.system_identifier != system_id
            or candidate.timeline != facts.timeline
            or candidate.start_lsn != start_lsn
            or candidate.wal_ranges != wal_ranges
            or candidate.migration_set_sha256 != facts.migration_set_sha256
            or source.ciphertext_size != candidate.base_object.ciphertext_size
        ):
            raise BaseCandidateError("completed candidate cleanup evidence does not match")
        _remove_tree(ready)
        plan_path.unlink(missing_ok=True)
        _fsync_dir(plan_path.parent)
        (root / "base-facts" / f"{chain_id}.json").unlink(missing_ok=True)
        _fsync_dir(root / "base-facts")


def reconcile_runtime_state(root: Path, *, key: bytes, key_id: str) -> None:
    with backup_lock(timeout_s=0):
        _recover_owned_partials(root)
        reconcile_completed_candidates(root, key=key, key_id=key_id)


def create_base_candidate(
    *,
    root: Path,
    prefix: str,
    key: bytes,
    key_id: str,
    store: RestartableStreamingObjectStore,
    budget: CandidateSpaceBudget,
    db_url: str | None = None,
    replication_db_url: str,
    stop: StopSignal | None = None,
    now: datetime | None = None,
    forced_chain_id: str | None = None,
) -> CandidateManifest:
    """Create one candidate. This module cannot mark a chain protected."""

    db_url = direct_db_url() if db_url is None else db_url
    now = datetime.now(UTC) if now is None else now.astimezone(UTC)
    candidates = root / "base-candidates"
    stop = threading.Event() if stop is None else stop
    reconcile_runtime_state(root, key=key, key_id=key_id)
    resumable = sorted(candidates.glob("*.ready")) if candidates.exists() else []
    resumable = [
        path
        for path in resumable
        if not (
            root / "base-manifests" / f"{path.name.removesuffix('.ready')}.candidate.json"
        ).exists()
    ]
    if forced_chain_id is not None:
        unrelated = [path for path in resumable if path.name != f"{forced_chain_id}.ready"]
        if unrelated:
            raise BaseCandidateError("unrelated base candidate is already in flight")
    if len(resumable) > 1:
        raise BaseCandidateError("multiple unfinished base candidates require operator review")
    if resumable:
        ready = resumable[0]
        chain_id = ready.name.removesuffix(".ready")
        facts = _load_facts(root, chain_id)
        _verify_candidate(ready, stop)
    else:
        chain_id = forced_chain_id or now.strftime("%Y%m%dT%H%M%SZ")
        if forced_chain_id is not None and not re.fullmatch(
            r"activation-\d{8}T\d{6}Z-[0-9a-f-]{36}", forced_chain_id
        ):
            raise BaseCandidateError("activation chain id is invalid")
        ready, facts = _birth_candidate(
            root=root,
            chain_id=chain_id,
            budget=budget,
            db_url=db_url,
            replication_db_url=replication_db_url,
            stop=stop,
        )
    native_manifest = ready / "backup_manifest"
    if not native_manifest.is_file():
        raise BaseCandidateError("pg_basebackup omitted backup_manifest")
    system_id, start_lsn, wal_ranges = parse_native_manifest(native_manifest)
    if system_id != facts.system_identifier:
        raise BaseCandidateError("backup manifest system identifier changed during capture")
    if wal_ranges[0].timeline != facts.timeline:
        raise BaseCandidateError("backup manifest starts on a different live timeline")
    end_lsn = wal_ranges[-1].end_lsn
    _, candidate_sha = snapshot_candidate(ready)
    object_name = f"{prefix.rstrip('/')}/base/{chain_id}/{candidate_sha}/base.tar.zst.enc"
    plan_path = root / "base-plans" / f"{chain_id}.plan.json"
    source, plan = load_or_create_source(
        ready,
        plan_path=plan_path,
        key=key,
        key_id=key_id,
        object_name=object_name,
    )
    metadata = {
        "ava-candidate-sha256": plan.candidate_sha256,
        "ava-ciphertext-size": str(plan.ciphertext_size),
        "ava-ciphertext-crc32c": plan.ciphertext_crc32c,
        "ava-encryption-format": BASE_MAGIC.decode(),
        "ava-key-id": key_id,
        "ava-packer-version": str(plan.packer_version),
    }
    ack = store.put_base_if_absent(
        source=source,
        object_name=object_name,
        metadata=metadata,
        cancelled=stop.is_set,
    )
    candidate = CandidateManifest(
        schema_version=SCHEMA_VERSION,
        chain_id=chain_id,
        protected=False,
        postgres_major=facts.postgres_major,
        database_name=facts.database_name,
        system_identifier=system_id,
        wal_segment_size=facts.wal_segment_size,
        timeline=wal_ranges[0].timeline,
        start_lsn=start_lsn,
        end_lsn=end_lsn,
        wal_ranges=wal_ranges,
        base_object=base_object_from_ack(
            ack,
            ciphertext_crc32c=plan.ciphertext_crc32c,
            source_sha256=plan.candidate_sha256,
            source_size=plan.candidate_size,
            key_id=key_id,
            encryption_format=BASE_MAGIC.decode(),
        ),
        native_manifest_sha256=plan.native_manifest_sha256,
        native_manifest_member_path="backup_manifest",
        native_manifest_container_object_name=ack.object_name,
        native_manifest_container_pin_token=ack.pin_token,
        migration_set_sha256=facts.migration_set_sha256,
    )
    manifest_path = root / "base-manifests" / f"{chain_id}.candidate.json"
    if stop.is_set():
        raise BaseCandidateError("base candidate lost ownership before manifest publication")
    _atomic_json(manifest_path, json.loads(candidate.to_json()))
    _remove_tree(ready)
    plan_path.unlink()
    _fsync_dir(plan_path.parent)
    (root / "base-facts" / f"{chain_id}.json").unlink()
    _fsync_dir(root / "base-facts")
    return candidate
