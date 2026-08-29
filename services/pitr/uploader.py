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
from typing import BinaryIO

from services.pitr.archive_shim import archive_name_is_valid
from services.pitr.crypto import MAGIC, EncryptionPlan, create_plan, open_encrypted
from services.pitr.object_store import ObjectStore, RemoteObjectAck


class RemoteCollisionError(RuntimeError):
    """An immutable object name already contains different content."""


class AckCorruptionError(RuntimeError):
    """A local durable ACK cannot be trusted."""


@dataclass(frozen=True)
class AckManifest:
    archive_name: str
    source_sha256: str
    source_size: int
    object_name: str
    generation: int
    ciphertext_size: int
    ciphertext_crc32c: str
    encryption_format: str
    key_id: str
    acknowledged_at: str


def _digest(path: Path) -> tuple[str, int]:
    value = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
            size += len(chunk)
    return value.hexdigest(), size


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
    ) -> None:
        self._spool = spool
        self._ack_dir = ack_dir
        self._staging = staging
        self._prefix = prefix.rstrip("/")
        self._key = key
        self._key_id = key_id
        self._store = store

    def pending(self) -> list[Path]:
        return sorted(
            (entry for entry in self._spool.iterdir() if archive_name_is_valid(entry.name)),
            key=lambda entry: entry.stat().st_mtime,
        )

    def _object_name(self, archive_name: str) -> str:
        return f"{self._prefix}/wal/{archive_name[:8]}/{archive_name}.enc"

    def _read_ack(self, path: Path) -> AckManifest:
        try:
            raw = json.loads(path.read_text())
            return AckManifest(**raw)
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
            try:
                plan = EncryptionPlan(**json.loads(plan_path.read_text()))
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise AckCorruptionError("invalid encryption plan") from exc
            # open_encrypted performs the source identity check before any remote I/O.
            with open_encrypted(source, key=self._key, plan=plan):
                pass
            if plan.object_name != object_name or plan.key_id != self._key_id:
                raise AckCorruptionError("encryption plan targets a different immutable object")
            return plan, plan_path
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

    def upload_one(self, source: Path) -> AckManifest:
        if not archive_name_is_valid(source.name):
            raise ValueError("unsupported archive filename")
        source_hash, source_size = _digest(source)
        ack_path = self._ack_dir / f"{source.name}.ack.json"
        if ack_path.exists():
            ack = self._read_ack(ack_path)
            if ack.source_sha256 != source_hash or ack.source_size != source_size:
                raise AckCorruptionError("ACK does not describe the local spool object")
            source.unlink()
            _fsync_dir(self._spool)
            return ack

        object_name = self._object_name(source.name)
        plan, plan_path = self._load_or_create_plan(source, object_name)

        def open_source() -> BinaryIO:
            return open_encrypted(source, key=self._key, plan=plan)

        checksum = __import__("google_crc32c").Checksum()
        with open_source() as encrypted:
            for chunk in iter(lambda: encrypted.read(1024 * 1024), b""):
                checksum.update(chunk)
        expected_crc = base64.b64encode(checksum.digest()).decode()
        metadata = {
            "ava-archive-name": source.name,
            "ava-source-sha256": plan.source_sha256,
            "ava-source-size": str(plan.source_size),
            "ava-ciphertext-crc32c": expected_crc,
            "ava-encryption-format": MAGIC.decode(),
            "ava-key-id": self._key_id,
        }
        remote = self._store.put_stream_if_absent(
            open_source, plan.ciphertext_size, object_name, metadata
        )
        self._verify_remote(remote, object_name, plan.ciphertext_size, expected_crc, metadata)
        ack = AckManifest(
            source.name,
            plan.source_sha256,
            plan.source_size,
            object_name,
            remote.generation,
            remote.size,
            remote.crc32c,
            MAGIC.decode(),
            self._key_id,
            datetime.now(UTC).isoformat(),
        )
        self._write_ack(ack, ack_path)
        plan_path.unlink()
        _fsync_dir(self._staging)
        source.unlink()
        _fsync_dir(self._spool)
        return ack

    @staticmethod
    def _verify_remote(
        remote: RemoteObjectAck,
        object_name: str,
        ciphertext_size: int,
        expected_crc: str,
        metadata: dict[str, str],
    ) -> None:
        if (
            remote.object_name != object_name
            or remote.generation <= 0
            or remote.size != ciphertext_size
            or remote.crc32c != expected_crc
            or dict(remote.metadata) != metadata
        ):
            if not remote.created:
                raise RemoteCollisionError("immutable GCS object differs from local archive")
            raise RuntimeError("new GCS object failed post-upload verification")
