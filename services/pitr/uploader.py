"""Crash-consistent single-object PITR upload transaction."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import google_crc32c

from services.pitr.archive_shim import archive_name_is_valid
from services.pitr.checksums import CRC32C, ObjectChecksum, digest_file
from services.pitr.crypto import MAGIC, EncryptionPlan, create_plan, open_encrypted
from services.pitr.object_store import ObjectStore, RemoteObjectAck


class RemoteCollisionError(RuntimeError):
    """An immutable object name already contains different content."""


class AckCorruptionError(RuntimeError):
    """A local durable ACK cannot be trusted."""


class WalSourceTooLargeError(RuntimeError):
    """A source exceeds the bounded WAL-only staging contract."""


DEFAULT_MAX_WAL_SOURCE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class AckManifest:
    archive_name: str
    source_sha256: str
    source_size: int
    object_name: str
    pin_token: str
    ciphertext_size: int
    ciphertext_crc32c: str
    ciphertext_checksum_algo: str
    ciphertext_checksum_value: str
    encryption_format: str
    key_id: str
    acknowledged_at: str


def ack_manifest_from_raw(raw: dict[str, Any]) -> AckManifest:
    """Parse one durable ACK; older shapes normalize in place.

    Pre-abstraction ACKs carry ``generation`` + ``ciphertext_crc32c`` (the
    GCS vocabulary) and map one-to-one onto ``pin_token`` /
    ``ciphertext_checksum_*``; the first abstraction shape (GCS-only)
    dropped the crc32c field, which is reconstructable because every such
    ACK verifies with CRC32C. ``ciphertext_crc32c`` is the local plan
    digest — a non-crc32c backend still computes it locally, so fresh ACKs
    carry it alongside the backend-verified ``checksum_*`` pair.
    """
    normalized = dict(raw)
    if "pin_token" not in normalized:
        legacy_generation = normalized.pop("generation", None)
        if legacy_generation is None:
            raise TypeError("ACK manifest lacks a pin token")
        normalized["pin_token"] = str(legacy_generation)
    else:
        normalized.pop("generation", None)
    if "ciphertext_checksum_algo" not in normalized:
        normalized["ciphertext_checksum_algo"] = CRC32C
    if "ciphertext_checksum_value" not in normalized:
        legacy_crc32c = normalized.pop("ciphertext_crc32c", None)
        if legacy_crc32c is None:
            raise TypeError("ACK manifest lacks a ciphertext checksum")
        normalized["ciphertext_checksum_value"] = legacy_crc32c
    if "ciphertext_crc32c" not in normalized:
        if normalized.get("ciphertext_checksum_algo") != CRC32C:
            raise TypeError("ACK manifest lacks the local ciphertext digest")
        normalized["ciphertext_crc32c"] = normalized["ciphertext_checksum_value"]
    return AckManifest(**normalized)


@dataclass(frozen=True)
class DiskFootprint:
    spool_bytes: int
    staging_bytes: int

    @property
    def total_bytes(self) -> int:
        return self.spool_bytes + self.staging_bytes


def _digest(path: Path) -> tuple[str, int]:
    value = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
            size += len(chunk)
    return value.hexdigest(), size


def _verify_staged_ciphertext(path: Path, source: Path, *, key: bytes, plan: EncryptionPlan) -> str:
    """Verify a crash-preserved stage byte-for-byte against its durable plan."""

    checksum = google_crc32c.Checksum()  # pyright: ignore[reportUnknownMemberType]
    with path.open("rb") as staged, open_encrypted(source, key=key, plan=plan) as expected:
        while True:
            staged_chunk = staged.read(1024 * 1024)
            expected_chunk = expected.read(1024 * 1024)
            if staged_chunk != expected_chunk:
                raise AckCorruptionError("staged WAL ciphertext differs from its plan")
            if not staged_chunk:
                break
            checksum.update(staged_chunk)  # pyright: ignore[reportUnknownMemberType]
    return base64.b64encode(checksum.digest()).decode("ascii")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class PitrUploader:
    def __init__(
        self,
        *,
        spool: Path,
        ack_dir: Path,
        staging: Path,
        prefix: str,
        key: bytes,
        key_id: str,
        store: ObjectStore,
        max_source_bytes: int = DEFAULT_MAX_WAL_SOURCE_BYTES,
    ) -> None:
        self._spool = spool
        self._ack_dir = ack_dir
        self._staging = staging
        self._prefix = prefix.rstrip("/")
        self._key = key
        self._key_id = key_id
        self._store = store
        self._max_source_bytes = max_source_bytes

    def disk_footprint(self) -> DiskFootprint:
        """Return both plaintext spool and temporary ciphertext disk use.

        Entries may vanish between listing and stat: the upload daemon runs
        ``upload_one`` in a worker thread, which unlinks acknowledged WAL
        while the health handler is listing these same directories. A
        vanished entry contributes 0 bytes.
        """

        def _dir_bytes(directory: Path) -> int:
            total = 0
            for entry in directory.iterdir():
                try:
                    if entry.is_file():
                        total += entry.stat().st_size
                except OSError:
                    continue
            return total

        return DiskFootprint(
            spool_bytes=_dir_bytes(self._spool),
            staging_bytes=_dir_bytes(self._staging),
        )

    def pending(self) -> list[Path]:
        """Spool entries ordered oldest-first; vanished entries are skipped.

        The worker thread unlinks a WAL the moment its ACK lands, which can
        interleave with this listing once the upload loop runs off the event
        loop; an entry that disappears mid-listing is simply no longer
        pending.
        """
        with_mtime: list[tuple[float, Path]] = []
        for entry in self._spool.iterdir():
            if not archive_name_is_valid(entry.name):
                continue
            try:
                with_mtime.append((entry.stat().st_mtime, entry))
            except OSError:
                continue
        with_mtime.sort(key=lambda pair: pair[0])
        return [entry for _mtime, entry in with_mtime]

    def _object_name(self, archive_name: str) -> str:
        return f"{self._prefix}/wal/{archive_name[:8]}/{archive_name}.enc"

    def _read_ack(self, path: Path) -> AckManifest:
        try:
            raw = json.loads(path.read_text())
            return ack_manifest_from_raw(raw)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AckCorruptionError(f"invalid ACK manifest: {path.name}") from exc

    def _write_ack(self, ack: AckManifest, destination: Path) -> None:
        fd, raw = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        staged = Path(raw)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as output:
                json.dump(asdict(ack), output, sort_keys=True, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            staged.replace(destination)
            _fsync_dir(destination.parent)
        finally:
            staged.unlink(missing_ok=True)

    def _load_or_create_plan(self, source: Path, object_name: str) -> tuple[EncryptionPlan, Path]:
        plan_path = self._staging / f"{source.name}.plan.json"
        if plan_path.exists():
            return self._load_plan(source, object_name, plan_path), plan_path
        plan = create_plan(source, key_id=self._key_id, object_name=object_name)
        fd, raw = tempfile.mkstemp(prefix=f".{plan_path.name}.", dir=self._staging)
        staged = Path(raw)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as output:
                json.dump(asdict(plan), output, sort_keys=True, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            staged.replace(plan_path)
            _fsync_dir(self._staging)
        finally:
            staged.unlink(missing_ok=True)
        return plan, plan_path

    def _load_plan(self, source: Path, object_name: str, plan_path: Path) -> EncryptionPlan:
        try:
            plan = EncryptionPlan(**json.loads(plan_path.read_text()))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AckCorruptionError("invalid encryption plan") from exc
        with open_encrypted(source, key=self._key, plan=plan):
            pass
        if plan.object_name != object_name or plan.key_id != self._key_id:
            raise AckCorruptionError("encryption plan targets a different immutable object")
        return plan

    def _write_staging(self, source: Path, plan: EncryptionPlan) -> Path:
        """Materialize the encrypted archive as a real seekable file.

        QA #920 block 1: uploading the encrypted stream directly breaks past
        ~8 MiB — the SDK's resumable upload calls ``tell()``/``seek()`` on
        its source, and the streaming reader raises UnsupportedOperation, so
        every real WAL segment failed while tests (which read the whole
        stream in one ``read()``) stayed green. The ciphertext is therefore
        staged to disk first (atomic, 0600, fsync) and uploaded by filename.
        This path is WAL-only and its caller enforces the source-size bound;
        base backups use a separate restartable streaming contract.
        """
        staging_path = self._staging / f"{source.name}.enc"
        active = [entry for entry in self._staging.glob("*.enc") if entry != staging_path]
        if active:
            raise AckCorruptionError("multiple active WAL ciphertext staging files")
        if staging_path.exists():
            if staging_path.stat().st_size != plan.ciphertext_size:
                raise AckCorruptionError("staged WAL ciphertext size differs from its plan")
            return staging_path
        fd, raw = tempfile.mkstemp(prefix=f".{staging_path.name}.", dir=self._staging)
        staged = Path(raw)
        try:
            os.fchmod(fd, 0o600)
            with (
                os.fdopen(fd, "wb") as output,
                open_encrypted(source, key=self._key, plan=plan) as encrypted,
            ):
                for chunk in iter(lambda: encrypted.read(1024 * 1024), b""):
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            staged.replace(staging_path)
            _fsync_dir(self._staging)
        finally:
            staged.unlink(missing_ok=True)
        return staging_path

    def _cleanup_acknowledged_local_files(
        self, source: Path, ack: AckManifest, object_name: str
    ) -> None:
        """Reconcile any crash-left stage/plan before deleting acknowledged WAL."""

        plan_path = self._staging / f"{source.name}.plan.json"
        staging_path = self._staging / f"{source.name}.enc"
        if staging_path.exists() and not plan_path.exists():
            raise AckCorruptionError("acknowledged WAL stage has no encryption plan")
        if plan_path.exists():
            plan = self._load_plan(source, object_name, plan_path)
            if plan.ciphertext_size != ack.ciphertext_size:
                raise AckCorruptionError("acknowledged WAL plan differs from ACK")
            if staging_path.exists():
                if ack.ciphertext_checksum_algo == CRC32C:
                    crc32c = _verify_staged_ciphertext(
                        staging_path, source, key=self._key, plan=plan
                    )
                    stage_matches = (
                        crc32c == ack.ciphertext_checksum_value and crc32c == ack.ciphertext_crc32c
                    )
                else:
                    stage_matches = (
                        digest_file(ack.ciphertext_checksum_algo, str(staging_path))
                        == ack.ciphertext_checksum_value
                    )
                if staging_path.stat().st_size != ack.ciphertext_size or not stage_matches:
                    raise AckCorruptionError("acknowledged WAL stage differs from ACK")
                staging_path.unlink()
                _fsync_dir(self._staging)
            plan_path.unlink()
            _fsync_dir(self._staging)
        source.unlink()
        _fsync_dir(self._spool)

    def upload_one(self, source: Path) -> AckManifest:
        if not archive_name_is_valid(source.name):
            raise ValueError("unsupported archive filename")
        source_size_before_digest = source.stat().st_size
        if source_size_before_digest > self._max_source_bytes:
            raise WalSourceTooLargeError(
                f"WAL source exceeds {self._max_source_bytes}-byte staging limit"
            )
        source_hash, source_size = _digest(source)
        if source_size != source_size_before_digest:
            raise AckCorruptionError("WAL source changed while it was read")
        ack_path = self._ack_dir / f"{source.name}.ack.json"
        if ack_path.exists():
            ack = self._read_ack(ack_path)
            object_name = self._object_name(source.name)
            if (
                ack.archive_name != source.name
                or ack.source_sha256 != source_hash
                or ack.source_size != source_size
                or ack.object_name != object_name
                or ack.encryption_format != MAGIC.decode()
                or ack.key_id != self._key_id
                or not ack.pin_token
            ):
                raise AckCorruptionError("ACK does not describe the local spool object")
            self._cleanup_acknowledged_local_files(source, ack, object_name)
            return ack

        object_name = self._object_name(source.name)
        plan, _ = self._load_or_create_plan(source, object_name)
        staging_path = self._write_staging(source, plan)
        expected_crc = _verify_staged_ciphertext(staging_path, source, key=self._key, plan=plan)
        metadata = {
            "ava-archive-name": source.name,
            "ava-source-sha256": plan.source_sha256,
            "ava-source-size": str(plan.source_size),
            "ava-ciphertext-crc32c": expected_crc,
            "ava-encryption-format": MAGIC.decode(),
            "ava-key-id": self._key_id,
        }
        remote = self._store.put_wal_ciphertext_if_absent(staging_path, object_name, metadata)
        self._verify_remote(
            remote, object_name, plan.ciphertext_size, expected_crc, metadata, staging_path
        )
        ack = AckManifest(
            source.name,
            plan.source_sha256,
            plan.source_size,
            object_name,
            remote.pin_token,
            remote.size,
            expected_crc,
            remote.checksum.algo,
            remote.checksum.value,
            MAGIC.decode(),
            self._key_id,
            datetime.now(UTC).isoformat(),
        )
        self._write_ack(ack, ack_path)
        self._cleanup_acknowledged_local_files(source, ack, object_name)
        return ack

    @staticmethod
    def _verify_remote(
        remote: RemoteObjectAck,
        object_name: str,
        ciphertext_size: int,
        expected_crc: str,
        metadata: dict[str, str],
        staging_path: Path,
    ) -> None:
        if remote.object_name != object_name:
            raise RemoteCollisionError("immutable GCS object differs from local archive")
        if remote.checksum.algo == CRC32C:
            expected = ObjectChecksum(CRC32C, expected_crc)
        else:
            expected = ObjectChecksum(
                remote.checksum.algo, digest_file(remote.checksum.algo, str(staging_path))
            )
        exact_match = (
            bool(remote.pin_token)
            and remote.size == ciphertext_size
            and remote.checksum == expected
            and dict(remote.metadata) == metadata
        )
        if remote.created:
            # Fresh upload: the reloaded object must match what we sent —
            # size, crc32c, metadata (design baseline 4) — before any ACK.
            if not exact_match:
                raise RuntimeError("new GCS object failed post-upload verification")
            return
        # The durable plan pins the nonce, so crash recovery reproduces the
        # same ciphertext. Source-only equality would silently accept a
        # different immutable object under the canonical name.
        if not exact_match:
            raise RemoteCollisionError("immutable GCS object differs from local archive")
