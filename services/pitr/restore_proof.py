"""Generation-pinned restore drill domain workflow."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import struct
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

import psutil

from services.pitr.base_manifest import CandidateManifest
from services.pitr.base_restore_crypto import (
    authenticate_base_ciphertext,
    extract_authenticated_base,
)
from services.pitr.crypto import MAGIC, decrypt_archive
from services.pitr.object_store import RemoteObjectAck
from services.pitr.restore_manifest import (
    PROTECTED_SCHEMA_VERSION,
    ProtectedManifest,
    RestoreObject,
    RestoreProof,
    candidate_sha256,
    required_archive_names,
    wal_objects_from_acks,
)
from services.pitr.restore_object_store import GenerationPinnedObjectReader
from services.pitr.wal_validate import validate_wal_file


class RestoreProofError(RuntimeError):
    pass


@dataclass(frozen=True)
class RestoreSpaceBudget:
    spool_and_pg_wal_reserve: int
    logical_backup_peak: int
    emergency_floor: int


# Longest allowed restore run-directory name (".{token}.partial"): the restore
# sandbox's PostgreSQL listens on a unix socket inside the run directory, and
# macOS caps socket paths at 103 bytes. For the real restore root
# (~/.ava/physical-backup/restore, 47 chars) the fixed suffix
# "/socket/.s.PGSQL.<port>" leaves 33 chars for the directory name — a name
# carrying the full run_id (chain ids are ~17-86 chars) deterministically
# exceeded it and every restore proof on the host died at postmaster start
# ("Unix-domain socket path ... is too long", 2026-09-03 activation #7; CI
# never caught it because tests use short tmp_path roots). Keep the on-disk
# name within this budget and carry the full run_id in the owner evidence.
_MAX_RUN_DIR_NAME_LEN = 33


def _restore_run_token(chain_id: str, now: datetime) -> str:
    """Short, deterministic on-disk identity for one restore run.

    The token is the run timestamp plus the first six characters of the
    chain id's last dash-segment (the activation operation uuid, or the whole
    id for the dash-less scheduled chain id), so a run directory still tells
    which chain and when while fitting the socket-path budget
    (`_MAX_RUN_DIR_NAME_LEN`). Distinct chains starting in the same second
    collide only when their six-character prefixes do (1/16M for two random
    uuids) — a run that finds an existing partial refuses via the owner
    evidence instead of overwriting.
    """
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{chain_id.rsplit('-', 1)[-1][:6]}"


@dataclass(frozen=True)
class LivePostgresIdentity:
    pid: int
    created_at: float
    data_directory: str
    system_identifier: str
    postmaster_started_at: str
    probe_sha256: str


@dataclass(frozen=True)
class DrillResult:
    achieved_lsn: str
    replay_seconds: float
    smoke_seconds: float
    restored_verify_seconds: float
    restored_fingerprint_sha256: str


class RestoreDrillExecutor(Protocol):
    def live_identity(self) -> LivePostgresIdentity: ...

    def run(
        self,
        *,
        pgdata: Path,
        wal_dir: Path,
        candidate: CandidateManifest,
        run_root: Path,
        owner_path: Path,
    ) -> DrillResult: ...


class ProtectedManifestPublisher(Protocol):
    def put_manifest_if_absent(
        self, *, payload: bytes, object_name: str, metadata: dict[str, str]
    ) -> RemoteObjectAck: ...


def _base_restore_object(candidate: CandidateManifest) -> RestoreObject:
    metadata = {
        "ava-candidate-sha256": candidate.base_object.source_sha256,
        "ava-ciphertext-size": str(candidate.base_object.ciphertext_size),
        "ava-ciphertext-crc32c": candidate.base_object.ciphertext_crc32c,
        "ava-encryption-format": candidate.base_object.encryption_format,
        "ava-key-id": candidate.base_object.key_id,
        "ava-packer-version": "1",
    }
    return RestoreObject(
        "base.tar.zst.enc",
        candidate.base_object.object_name,
        candidate.base_object.pin_token,
        candidate.base_object.ciphertext_size,
        candidate.base_object.ciphertext_checksum_algo,
        candidate.base_object.ciphertext_checksum_value,
        tuple(sorted(metadata.items())),
    )


def _required_bytes(
    candidate: CandidateManifest,
    base: RestoreObject,
    wal: tuple[RestoreObject, ...],
    budget: RestoreSpaceBudget,
) -> int:
    wal_plain = sum(int(dict(item.metadata)["ava-source-size"]) for item in wal)
    return (
        base.size
        + candidate.base_object.source_size
        + sum(item.size for item in wal)
        + wal_plain
        + budget.spool_and_pg_wal_reserve
        + budget.logical_backup_peak
        + budget.emergency_floor
    )


def _require_space(root: Path, required: int) -> None:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    free = shutil.disk_usage(root).free
    if free < required:
        raise RestoreProofError(
            f"restore proof deferred: requires {required} bytes but only {free} are free"
        )


def _same_live(before: LivePostgresIdentity, after: LivePostgresIdentity) -> None:
    if before != after:
        raise RestoreProofError("live PostgreSQL identity changed during restore drill")


def _download_wal(
    *,
    reader: GenerationPinnedObjectReader,
    objects: tuple[RestoreObject, ...],
    encrypted_dir: Path,
    wal_dir: Path,
    key: bytes,
    candidate: CandidateManifest,
) -> None:
    encrypted_dir.mkdir(mode=0o700)
    wal_dir.mkdir(mode=0o700)
    for item in objects:
        ciphertext = encrypted_dir / f"{item.archive_name}.enc"
        reader.download_exact(item, ciphertext)
        _verify_wal_header(ciphertext, item)
        destination = wal_dir / item.archive_name
        decrypt_archive(ciphertext, destination, key=key)
        source_sha256 = hashlib.sha256(destination.read_bytes()).hexdigest()
        metadata = dict(item.metadata)
        if (
            destination.stat().st_size != int(metadata["ava-source-size"])
            or source_sha256 != metadata["ava-source-sha256"]
        ):
            raise RestoreProofError("restored WAL plaintext differs from protected evidence")
        validate_wal_file(destination, candidate)
        ciphertext.unlink()


def _verify_wal_header(path: Path, expected: RestoreObject) -> None:
    with path.open("rb") as source:
        if source.read(len(MAGIC)) != MAGIC:
            raise RestoreProofError("WAL object has an unsupported encryption format")
        raw_length = source.read(4)
        if len(raw_length) != 4:
            raise RestoreProofError("WAL object has a truncated header")
        header_length = struct.unpack(">I", raw_length)[0]
        if header_length <= 0 or header_length > 64 * 1024:
            raise RestoreProofError("WAL object has an invalid header length")
        header = json.loads(source.read(header_length))
    metadata = dict(expected.metadata)
    exact = {
        "archive_name": expected.archive_name,
        "key_id": metadata["ava-key-id"],
        "object_name": expected.object_name,
        "source_sha256": metadata["ava-source-sha256"],
        "source_size": int(metadata["ava-source-size"]),
    }
    if set(header) != {*exact, "nonce"} or any(
        header[key] != value for key, value in exact.items()
    ):
        raise RestoreProofError("WAL encryption header differs from protected evidence")


def _write_local_manifest(path: Path, payload: bytes) -> None:
    if path.is_file():
        if path.read_bytes() != payload:
            raise RestoreProofError("durable restore manifest differs from retry payload")
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    staged = Path(raw)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.link(staged, path, follow_symlinks=False)
        _fsync_dir(path.parent)
    finally:
        staged.unlink(missing_ok=True)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_owner(path: Path, value: dict[str, object]) -> None:
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


def update_restore_owner(path: Path, **changes: object) -> None:
    """Durably extend restore ownership without weakening prior evidence."""

    try:
        evidence = _require_owner_object(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError) as exc:
        raise RestoreProofError("restore owner evidence is unreadable") from exc
    evidence.update(changes)
    _atomic_owner(path, evidence)


def _is_zombie(process: psutil.Process) -> bool:
    """A zombie is an exited process whose status was never reaped: it runs
    nothing, so every live-process probe must count it as dead. An unreaped
    sandbox postmaster otherwise keeps looking "live" to the cleanup guards
    and masks the real failure (activation #12)."""
    try:
        return process.status() == psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied):
        return False


def _matching_process(pid: int, created_at: float) -> psutil.Process | None:
    try:
        process = psutil.Process(pid)
        if abs(process.create_time() - created_at) >= 0.01:
            return None
        if _is_zombie(process):
            return None
        return process
    except psutil.AccessDenied as exc:
        raise RestoreProofError("cannot verify restore owner identity") from exc
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return None


def _group_members(pgid: int) -> list[psutil.Process]:
    members: list[psutil.Process] = []
    for process in psutil.process_iter(["pid"]):
        try:
            if os.getpgid(process.pid) == pgid and not _is_zombie(process):
                members.append(process)
        except (ProcessLookupError, PermissionError, psutil.NoSuchProcess):
            continue
    return members


def _stop_owned_group(leader: psutil.Process, pgid: int) -> None:
    try:
        if os.getpgid(leader.pid) != pgid or pgid == os.getpgrp():
            raise RestoreProofError("refusing to signal an unowned restore process group")
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 20
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    grace = min(deadline, time.monotonic() + 5)
    while _group_members(pgid) and time.monotonic() < grace:
        time.sleep(0.1)
    while _group_members(pgid) and time.monotonic() < deadline:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    if _group_members(pgid):
        raise RestoreProofError("restore process group retained live descendants")


def _sandbox_is_live(evidence: dict[str, object]) -> bool:
    raw_pid = evidence.get("sandbox_pid")
    raw_created_at = evidence.get("sandbox_created_at")
    if raw_pid is None and raw_created_at is None:
        return False
    if raw_pid is None or raw_created_at is None:
        raise RestoreProofError("restore owner has incomplete sandbox identity")
    return _matching_process(_owner_int(raw_pid), _owner_float(raw_created_at)) is not None


def _stop_owned_sandbox(evidence: dict[str, object], pgid: int) -> None:
    raw_pid = evidence.get("sandbox_pid")
    raw_created_at = evidence.get("sandbox_created_at")
    raw_pgid = evidence.get("sandbox_pgid")
    if raw_pid is None or raw_created_at is None or raw_pgid is None:
        if evidence.get("state") == "postgres_starting" and pgid == _owner_int(evidence["pid"]):
            _stop_ownerless_job_group(pgid)
            return
        raise RestoreProofError("orphaned restore group lacks sandbox ownership evidence")
    if _owner_int(raw_pgid) != pgid:
        raise RestoreProofError("sandbox PostgreSQL escaped its restore process group")
    leader = _matching_process(_owner_int(raw_pid), _owner_float(raw_created_at))
    if leader is None:
        raise RestoreProofError("restore group survives without its recorded sandbox postmaster")
    deadline = time.monotonic() + 20
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    grace = min(deadline, time.monotonic() + 5)
    while _group_members(pgid) and time.monotonic() < grace:
        time.sleep(0.1)
    while _group_members(pgid) and time.monotonic() < deadline:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    if _group_members(pgid):
        raise RestoreProofError("orphaned restore process group could not be emptied")


def _stop_ownerless_job_group(pgid: int) -> None:
    if pgid == os.getpgrp():
        raise RestoreProofError("refusing to signal the current restore process group")
    deadline = time.monotonic() + 20
    with suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGTERM)
    grace = min(deadline, time.monotonic() + 5)
    while _group_members(pgid) and time.monotonic() < grace:
        time.sleep(0.1)
    while _group_members(pgid) and time.monotonic() < deadline:
        with suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGKILL)
        time.sleep(0.1)
    if _group_members(pgid):
        raise RestoreProofError("restore job group could not be emptied")


def _remove_owned_restore(partial: Path, owner: Path, evidence: dict[str, object]) -> None:
    if partial.is_symlink() or not partial.is_dir():
        raise RestoreProofError("refusing to remove an unexpected restore path")
    if _sandbox_is_live(evidence):
        raise RestoreProofError("refusing to remove a live restore PostgreSQL data directory")
    if evidence.get("state") in {"postgres_starting", "postgres_running"}:
        raise RestoreProofError("refusing to remove restore data with unresolved PostgreSQL state")
    shutil.rmtree(partial)
    _fsync_dir(partial.parent)
    owner.unlink()
    _fsync_dir(owner.parent)


def _require_owner_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RestoreProofError("restore owner evidence is not an object")
    return cast(dict[str, object], value)


def _owner_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise RestoreProofError("restore owner integer field is invalid")
    return value


def _owner_float(value: object) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise RestoreProofError("restore owner numeric field is invalid")
    return float(value)


def reconcile_restore_runtime(root: Path) -> None:  # noqa: PLR0915
    """Recover only restore runs whose durable owner identity is conclusive."""

    restore_root = root / "restore"
    owners = root / "restore-owners"
    partials: set[Path] = set(restore_root.glob(".*.partial")) if restore_root.exists() else set()
    owner_paths: set[Path] = set(owners.glob("*.owner.json")) if owners.exists() else set()
    for owner in sorted(owner_paths):
        try:
            evidence = _require_owner_object(json.loads(owner.read_text()))
            partial = Path(str(evidence["partial"]))
            pid = _owner_int(evidence["pid"])
            created_at = _owner_float(evidence["created_at"])
            pgid = _owner_int(evidence["pgid"])
            deadline = _owner_float(evidence["deadline"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RestoreProofError("invalid restore owner evidence") from exc
        if partial.parent != restore_root:
            raise RestoreProofError("restore owner escaped the restore root")
        leader = _matching_process(pid, created_at)
        if partial not in partials:
            if leader is not None and time.time() < deadline:
                raise RestoreProofError("restore spawn owner is still active")
            if leader is not None:
                _stop_owned_group(leader, pgid)
            elif _group_members(pgid):
                raise RestoreProofError("restore spawn owner left unattributed descendants")
            owner.unlink()
            _fsync_dir(owner.parent)
            continue
        if leader is not None:
            if time.time() < deadline:
                raise RestoreProofError("restore proof owner is still active")
            _stop_owned_group(leader, pgid)
        elif _group_members(pgid):
            _stop_owned_sandbox(evidence, pgid)
        if evidence.get("state") in {"postgres_starting", "postgres_running"}:
            postmaster = partial / "sandbox" / "data" / "postmaster.pid"
            if postmaster.exists() and _sandbox_is_live(evidence):
                raise RestoreProofError("dead restore owner left unresolved postmaster evidence")
            evidence["state"] = "postgres_stopped"
        _remove_owned_restore(partial, owner, evidence)
        partials.remove(partial)
    if partials:
        raise RestoreProofError("restore partial lacks durable owner evidence")
    pending_root = root / "protected-pending"
    local_root = root / "protected-manifests"
    if pending_root.exists():
        for pending in pending_root.glob("*.json"):
            local = local_root / pending.name
            if not local.is_file():
                continue
            if local.read_bytes() != pending.read_bytes():
                raise RestoreProofError("protected local and pending manifests differ")
            pending.unlink()
            _fsync_dir(pending_root)


def _publish_protected(
    *,
    root: Path,
    candidate: CandidateManifest,
    prefix: str,
    payload: bytes,
    publisher: ProtectedManifestPublisher,
    require_ownership: Callable[[], None] = lambda: None,
) -> None:
    object_name = f"{prefix.rstrip('/')}/protected/{candidate.chain_id}.json"
    metadata = {
        "ava-chain-id": candidate.chain_id,
        "ava-candidate-sha256": candidate_sha256(candidate),
        "ava-manifest-sha256": hashlib.sha256(payload).hexdigest(),
        "ava-protected": "true",
    }
    require_ownership()
    ack = publisher.put_manifest_if_absent(
        payload=payload, object_name=object_name, metadata=metadata
    )
    require_ownership()
    if (
        ack.object_name != object_name
        or not ack.pin_token
        or ack.size != len(payload)
        or dict(ack.metadata) != metadata
    ):
        raise RestoreProofError("protected manifest remote ACK differs")
    local = root / "protected-manifests" / f"{candidate.chain_id}.json"
    require_ownership()
    _write_local_manifest(local, payload)


def _resume_protected_publish(
    *,
    root: Path,
    candidate: CandidateManifest,
    prefix: str,
    publisher: ProtectedManifestPublisher,
) -> ProtectedManifest | None:
    local = root / "protected-manifests" / f"{candidate.chain_id}.json"
    pending = root / "protected-pending" / f"{candidate.chain_id}.json"
    source = local if local.is_file() else pending if pending.is_file() else None
    if source is None:
        return None
    payload = source.read_bytes()
    protected = ProtectedManifest.from_json(payload.decode())
    if protected.chain_id != candidate.chain_id or protected.candidate_sha256 != candidate_sha256(
        candidate
    ):
        raise RestoreProofError("durable protected retry does not match its candidate")
    if source == pending:
        _publish_protected(
            root=root,
            candidate=candidate,
            prefix=prefix,
            payload=payload,
            publisher=publisher,
        )
        pending.unlink()
        _fsync_dir(pending.parent)
    elif pending.is_file():
        if pending.read_bytes() != payload:
            raise RestoreProofError("protected local and pending manifests differ")
        pending.unlink()
        _fsync_dir(pending.parent)
    return protected


def prove_candidate(  # noqa: PLR0915
    *,
    candidate: CandidateManifest,
    root: Path,
    ack_dir: Path,
    key: bytes,
    reader: GenerationPinnedObjectReader,
    executor: RestoreDrillExecutor,
    budget: RestoreSpaceBudget,
    now: datetime | None = None,
) -> ProtectedManifest:
    """Create durable proof evidence without possessing publication authority."""

    reconcile_restore_runtime(root)
    pending = root / "protected-pending" / f"{candidate.chain_id}.json"
    if pending.is_file():
        protected = ProtectedManifest.from_json(pending.read_text())
        if protected.candidate_sha256 != candidate_sha256(candidate):
            raise RestoreProofError("durable pending proof differs from its candidate")
        return protected
    archive_names = required_archive_names(candidate.wal_ranges, candidate.wal_segment_size)
    wal = wal_objects_from_acks(ack_dir=ack_dir, archive_names=archive_names)
    base = _base_restore_object(candidate)
    _require_space(root, _required_bytes(candidate, base, wal, budget))
    now = datetime.now(UTC) if now is None else now.astimezone(UTC)
    run_id = f"{candidate.chain_id}-{now.strftime('%Y%m%dT%H%M%SZ')}"
    # The on-disk run identity is a SHORT token, not the full run_id: the
    # sandboxed PostgreSQL's unix socket lives under the run directory, and
    # macOS caps socket paths at 103 bytes (see _restore_run_token / the
    # _MAX_RUN_DIR_NAME_LEN budget). The full run_id rides in the owner
    # evidence below and in the published proof.
    token = _restore_run_token(candidate.chain_id, now)
    partial = root / "restore" / f".{token}.partial"
    owner = root / "restore-owners" / f"{token}.owner.json"
    if partial.exists() or partial.is_symlink():
        raise RestoreProofError("restore proof has unresolved owned work")
    process = psutil.Process()
    pgid = os.getpgrp()
    if pgid != process.pid:
        raise RestoreProofError("restore proof must run as its process-group leader")
    if owner.exists() or owner.is_symlink():
        raise RestoreProofError("restore proof owner evidence already exists")
    _atomic_owner(
        owner,
        {
            "schema_version": 1,
            "state": "spawning",
            "run_id": run_id,
            "partial": str(partial),
            "pid": process.pid,
            "created_at": process.create_time(),
            "pgid": pgid,
            "deadline": time.time() + 6 * 3600,
        },
    )
    try:
        partial.mkdir(parents=True, mode=0o700)
        update_restore_owner(owner, state="running")
        started_at = now.isoformat()
        before = executor.live_identity()
        base_ciphertext = partial / "quarantine" / "base.enc"
        reader.download_exact(base, base_ciphertext)
        authenticate_base_ciphertext(base_ciphertext, key=key, expected=base)
        pgdata = extract_authenticated_base(
            base_ciphertext,
            partial / "sandbox",
            key=key,
            expected=base,
            candidate_sha256=candidate.base_object.source_sha256,
            native_manifest_sha256=candidate.native_manifest_sha256,
            max_extracted_bytes=candidate.base_object.source_size,
        )
        encrypted_wal = partial / "quarantine" / "wal"
        wal_dir = partial / "archive"
        _download_wal(
            reader=reader,
            objects=wal,
            encrypted_dir=encrypted_wal,
            wal_dir=wal_dir,
            key=key,
            candidate=candidate,
        )
        result = executor.run(
            pgdata=pgdata,
            wal_dir=wal_dir,
            candidate=candidate,
            run_root=partial,
            owner_path=owner,
        )
        after = executor.live_identity()
        _same_live(before, after)
        completed_at = datetime.now(UTC).isoformat()
        proof = RestoreProof(
            run_id,
            started_at,
            completed_at,
            candidate.end_lsn,
            result.achieved_lsn,
            before.pid,
            before.probe_sha256,
            candidate.native_manifest_sha256,
            result.replay_seconds,
            result.smoke_seconds,
            result.restored_verify_seconds,
            base.size + sum(item.size for item in wal),
            result.restored_fingerprint_sha256,
        )
        protected = ProtectedManifest(
            schema_version=PROTECTED_SCHEMA_VERSION,
            protected=True,
            chain_id=candidate.chain_id,
            candidate_sha256=candidate_sha256(candidate),
            candidate=candidate,
            base=base,
            wal=wal,
            target_lsn=candidate.end_lsn,
            wal_segment_size=candidate.wal_segment_size,
            proof=proof,
        )
        payload = protected.to_json().encode()
        _write_local_manifest(pending, payload)
        update_restore_owner(owner, state="proof_durable", pending=str(pending))
        return protected
    finally:
        if owner.is_file():
            if partial.exists():
                evidence = json.loads(owner.read_text())
                _remove_owned_restore(partial, owner, evidence)
            else:
                owner.unlink()
                _fsync_dir(owner.parent)


def verify_candidate_proof(
    *,
    candidate: CandidateManifest,
    root: Path,
    ack_dir: Path,
) -> ProtectedManifest:
    """Verify durable proof against authoritative local candidate and ACK evidence."""

    local = root / "protected-manifests" / f"{candidate.chain_id}.json"
    pending = root / "protected-pending" / f"{candidate.chain_id}.json"
    source = local if local.is_file() else pending if pending.is_file() else None
    if source is None:
        raise RestoreProofError("protected publication has no durable pending proof")
    protected = ProtectedManifest.from_json(source.read_text())
    if protected.candidate_sha256 != candidate_sha256(candidate):
        raise RestoreProofError("pending proof differs from authoritative candidate")
    archive_names = required_archive_names(candidate.wal_ranges, candidate.wal_segment_size)
    authoritative_wal = wal_objects_from_acks(ack_dir=ack_dir, archive_names=archive_names)
    if protected.base != _base_restore_object(candidate) or protected.wal != authoritative_wal:
        raise RestoreProofError("pending proof differs from authoritative object identities")
    return protected


def publish_candidate_proof(
    *,
    candidate: CandidateManifest,
    root: Path,
    prefix: str,
    verified: ProtectedManifest,
    publisher: ProtectedManifestPublisher,
    require_ownership: Callable[[], None] = lambda: None,
) -> ProtectedManifest:
    """Publish bytes already verified before publisher authority was constructed."""

    payload = verified.to_json().encode()
    pending = root / "protected-pending" / f"{candidate.chain_id}.json"
    local = root / "protected-manifests" / f"{candidate.chain_id}.json"
    source = local if local.is_file() else pending if pending.is_file() else None
    if source is None or source.read_bytes() != payload:
        raise RestoreProofError("verified proof changed before publication")
    if source == pending:
        _publish_protected(
            root=root,
            candidate=candidate,
            prefix=prefix,
            payload=payload,
            publisher=publisher,
            require_ownership=require_ownership,
        )
        require_ownership()
        pending.unlink()
        _fsync_dir(pending.parent)
    return verified
