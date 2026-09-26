"""Create and publish one unprotected weekly physical base candidate."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

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
from services.pitr.operation_custody import NativeProcess, OperationWorker, owned_receipts
from services.pitr.space_budget import CandidateSpaceBudget, require_candidate_space
from services.pitr.worker_process import StopSignal
from shared.db import direct_db_url
from shared.pg_tools import pg_tool


class BaseCandidateError(RuntimeError):
    pass


# The owner state of a completed capture that failed its own verification or
# loading: quarantine discards it instead of keeping it for resumption.
REJECTED = "rejected"


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


def _remove_tree(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise BaseCandidateError(f"refusing to remove unexpected candidate path: {path.name}")
    shutil.rmtree(path)
    _fsync_dir(path.parent)


def _recover_owned_partials(root: Path) -> None:
    partials = list((root / "base-candidates").glob(".*.partial"))
    owners = list((root / "base-facts").glob("*.owner.json"))
    if partials or owners:
        raise BaseCandidateError(
            "base candidate has unresolved operation evidence; `ava pitr operations "
            "retire` must prove its worker closed, never receipt-based adoption: "
            f"{sorted(path.name for path in partials + owners)}"
        )


def quarantine_candidate_staging(root: Path, work: Path, worker: OperationWorker | None) -> None:
    """After proven closure, drop a worker's plaintext copy and keep its receipts.

    An incomplete `.partial` copy of PGDATA never survives its writer. A
    completed `.ready` capture keeps its facts and plan so the next worker can
    resume it without another multi-hour capture, unless its worker rejected
    it: a tree that failed its own verification or loading would fail every
    resumption. The owner receipt moves into the quarantined controls, which
    frees the chain for the next worker.
    """
    facts = root / "base-facts"
    owners = sorted(facts.glob("*.owner.json")) if facts.is_dir() else []
    for owner, evidence in owned_receipts(owners, work, worker):
        chain_id = str(evidence["chain_id"])
        if not re.fullmatch(r"[0-9A-Za-z-]+", chain_id):
            raise BaseCandidateError("base candidate owner names an invalid chain")
        partial = root / "base-candidates" / f".{chain_id}.partial"
        if partial.exists() or partial.is_symlink():
            _remove_tree(partial)
        ready = root / "base-candidates" / f"{chain_id}.ready"
        if evidence["state"] == REJECTED and (ready.exists() or ready.is_symlink()):
            _remove_tree(ready)
        if not ready.exists():
            (facts / f"{chain_id}.json").unlink(missing_ok=True)
            (root / "base-plans" / f"{chain_id}.plan.json").unlink(missing_ok=True)
        receipts = work / "business"
        receipts.mkdir(mode=0o700, exist_ok=True)
        shutil.move(owner, receipts / owner.name)
        _fsync_dir(facts)


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


def _run_tool(
    command: list[str],
    *,
    stop: StopSignal,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    # These are trusted foreground children of the operation worker. Reaping a
    # tool does not release the worker's group pin; its controller closes all
    # inherited descendants before accepting output or retiring artifacts.
    process = subprocess.Popen(  # noqa: S603 -- exact tool argv.
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    deadline = time.monotonic() + 6 * 3600
    while True:
        if stop.is_set() or time.monotonic() >= deadline:
            raise BaseCandidateError("backup tool stopped or exceeded its six-hour bound")
        try:
            stdout, stderr = process.communicate(timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            continue
    if process.returncode != 0:
        raise BaseCandidateError(
            f"{command[0]} exited {process.returncode}{_output_suffix(stdout, stderr)}"
        )
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


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
        _record_owner(root, chain_id)
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
        _run_tool(command, env={"PGPASSWORD": password} if password else {}, stop=stop)
        _verify_candidate(partial, stop)
        _atomic_json(facts_path, asdict(facts))
        partial.replace(ready)
        _fsync_dir(ready.parent)
    return ready, facts


def _record_owner(root: Path, chain_id: str, state: str = "running") -> None:
    """Bind candidate staging to this worker; the controller commits only its own."""
    owner = root / "base-facts" / f"{chain_id}.owner.json"
    if owner.exists() or owner.is_symlink():
        raise BaseCandidateError("base candidate owner evidence already exists")
    _atomic_json(
        owner,
        {
            "state": state,
            "native": NativeProcess.capture(psutil.Process()).value(),
            "pgid": os.getpgrp(),
            "chain_id": chain_id,
        },
    )


def _set_owner_state(root: Path, chain_id: str, state: str) -> None:
    owner = root / "base-facts" / f"{chain_id}.owner.json"
    evidence = json.loads(owner.read_text())
    if not NativeProcess.from_value(evidence["native"]).same_birth(
        NativeProcess.capture(psutil.Process())
    ):
        raise BaseCandidateError("base candidate owner belongs to another worker")
    _atomic_json(owner, {**evidence, "state": state})


def _loaded_capture(
    root: Path, chain_id: str, ready: Path, facts: CandidateFacts | None, stop: StopSignal
) -> CandidateFacts:
    """Verify and load a completed capture; a failure rejects the tree for good.

    A stop is not a verdict on the tree, so it stays resumable. Any other
    failure here (corruption, missing or contradicting facts) would fail
    every resumption, so the worker marks the capture rejected before raising.
    """
    try:
        if facts is None:
            facts = _load_facts(root, chain_id)
            _verify_candidate(ready, stop)
        _check_capture(ready, facts)
    except Exception:
        _set_owner_state(root, chain_id, REJECTED)
        raise
    return facts


def _check_capture(ready: Path, facts: CandidateFacts) -> None:
    native_manifest = ready / "backup_manifest"
    if not native_manifest.is_file():
        raise BaseCandidateError("pg_basebackup omitted backup_manifest")
    system_id, _start_lsn, wal_ranges = parse_native_manifest(native_manifest)
    if system_id != facts.system_identifier:
        raise BaseCandidateError("backup manifest system identifier changed during capture")
    if wal_ranges[0].timeline != facts.timeline:
        raise BaseCandidateError("backup manifest starts on a different live timeline")


def _verify_candidate(path: Path, stop: StopSignal) -> None:
    _run_tool([str(pg_tool("pg_verifybackup")), "--no-parse-wal", str(path)], stop=stop)


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
    _finish_retiring_trees(root)
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
        _remove_commit_staging(root, chain_id)


def _finish_retiring_trees(root: Path) -> None:
    """A torn removal after the manifest committed is garbage, never evidence."""
    for retiring in (root / "base-candidates").glob(".*.retiring"):
        chain_id = retiring.name.removeprefix(".").removesuffix(".retiring")
        if not (root / "base-manifests" / f"{chain_id}.candidate.json").is_file():
            raise BaseCandidateError("retiring candidate tree lacks its committed manifest")
        _remove_tree(retiring)
        _remove_commit_staging(root, chain_id)


def _remove_commit_staging(root: Path, chain_id: str) -> None:
    for staged in (
        root / "base-plans" / f"{chain_id}.plan.json",
        root / "base-facts" / f"{chain_id}.json",
    ):
        if staged.parent.is_dir():
            staged.unlink(missing_ok=True)
            _fsync_dir(staged.parent)


def reconcile_runtime_state(root: Path, *, key: bytes, key_id: str) -> None:
    """Worker-side guard: refuse unsettled evidence, then finish committed cleanup."""
    with backup_lock(timeout_s=0):
        _recover_owned_partials(root)
        reconcile_completed_candidates(root, key=key, key_id=key_id)


def reconcile_committed_cleanup(root: Path, *, key: bytes, key_id: str) -> None:
    """Scheduler-side cleanup of committed trees only.

    Another operation's unsettled staging is its own kind's custody question
    (quarantine or a blocked control root), never a reason to stall this.
    """
    with backup_lock(timeout_s=0):
        reconcile_completed_candidates(root, key=key, key_id=key_id)


def _resumable_candidate(root: Path, forced_chain_id: str | None) -> Path | None:
    """The single captured tree still lacking a manifest, if one exists."""
    candidates = root / "base-candidates"
    resumable = [
        path
        for path in (sorted(candidates.glob("*.ready")) if candidates.exists() else [])
        if not (
            root / "base-manifests" / f"{path.name.removesuffix('.ready')}.candidate.json"
        ).exists()
    ]
    if forced_chain_id is not None and any(
        path.name != f"{forced_chain_id}.ready" for path in resumable
    ):
        raise BaseCandidateError("unrelated base candidate is already in flight")
    if len(resumable) > 1:
        raise BaseCandidateError("multiple unfinished base candidates require operator review")
    return resumable[0] if resumable else None


def discard_resumable_candidate(root: Path, chain_id: str, *, confirm: bool) -> Path:
    """Remove one unfinished capture an operator judged stale; without `confirm`, check.

    The caller holds the base-candidate kind lock with no blocked operation,
    so no worker can own the tree. A committed chain (reconciliation removes
    its tree) and one that an unsettled owner receipt still claims refuse.
    """
    if not re.fullmatch(r"[0-9A-Za-z-]+", chain_id):
        raise BaseCandidateError(f"invalid chain id {chain_id!r}")
    ready = root / "base-candidates" / f"{chain_id}.ready"
    if ready.is_symlink() or not ready.is_dir():
        raise BaseCandidateError(f"no unfinished capture at {ready}")
    if (root / "base-manifests" / f"{chain_id}.candidate.json").exists():
        raise BaseCandidateError(f"{chain_id} is committed; reconciliation removes its tree")
    if (root / "base-facts" / f"{chain_id}.owner.json").exists():
        raise BaseCandidateError(f"an unsettled operation still owns {chain_id}; retire it first")
    if confirm:
        _remove_tree(ready)
        _remove_commit_staging(root, chain_id)
    return ready


def prepare_base_candidate(
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
    """Prepare uploaded candidate evidence; the controller commits after closure."""

    db_url = direct_db_url() if db_url is None else db_url
    now = datetime.now(UTC) if now is None else now.astimezone(UTC)
    stop = threading.Event() if stop is None else stop
    reconcile_runtime_state(root, key=key, key_id=key_id)
    resumable = _resumable_candidate(root, forced_chain_id)
    if resumable is not None:
        ready = resumable
        chain_id = ready.name.removesuffix(".ready")
        # Operator retirement removed the prior owner; this worker now owns it.
        _record_owner(root, chain_id, "verifying")
        facts = _loaded_capture(root, chain_id, ready, None, stop)
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
        _loaded_capture(root, chain_id, ready, facts, stop)
    system_id, start_lsn, wal_ranges = parse_native_manifest(ready / "backup_manifest")
    end_lsn = wal_ranges[-1].end_lsn
    plan_path = root / "base-plans" / f"{chain_id}.plan.json"
    try:  # A tree that cannot be hashed or planned is rejected like one that fails verification.
        _, candidate_sha = snapshot_candidate(ready)
        object_name = f"{prefix.rstrip('/')}/base/{chain_id}/{candidate_sha}/base.tar.zst.enc"
        source, plan = load_or_create_source(
            ready,
            plan_path=plan_path,
            key=key,
            key_id=key_id,
            object_name=object_name,
        )
    except Exception:
        _set_owner_state(root, chain_id, REJECTED)
        raise
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
    if stop.is_set():
        raise BaseCandidateError("base candidate lost ownership before returning its result")
    return candidate


def commit_base_candidate(root: Path, candidate: CandidateManifest, worker: NativeProcess) -> None:
    """Commit prepared output from this closed worker, then retire exact staging."""
    chain_id = candidate.chain_id
    owner = root / "base-facts" / f"{chain_id}.owner.json"
    original = owner.read_bytes()
    evidence = json.loads(original)
    if evidence["chain_id"] != chain_id or not NativeProcess.from_value(
        evidence["native"]
    ).same_birth(worker):
        raise BaseCandidateError("base candidate belongs to another operation worker")
    ready = root / "base-candidates" / f"{chain_id}.ready"
    _, digest = snapshot_candidate(ready)
    if digest != candidate.base_object.source_sha256:
        raise BaseCandidateError("prepared candidate changed before controller commit")
    native_digest = hashlib.sha256((ready / "backup_manifest").read_bytes()).hexdigest()
    if native_digest != candidate.native_manifest_sha256 or owner.read_bytes() != original:
        raise BaseCandidateError("prepared candidate evidence changed before controller commit")
    manifest_path = root / "base-manifests" / f"{chain_id}.candidate.json"
    _atomic_json(manifest_path, json.loads(candidate.to_json()))
    # Rename before removal: a crash mid-removal leaves a `.retiring` tree that
    # reconciliation deletes, never a torn `.ready` it cannot verify.
    retiring = ready.with_name(f".{chain_id}.retiring")
    ready.rename(retiring)
    _fsync_dir(retiring.parent)
    _remove_tree(retiring)
    _remove_commit_staging(root, chain_id)
    owner.unlink()
    _fsync_dir(owner.parent)
