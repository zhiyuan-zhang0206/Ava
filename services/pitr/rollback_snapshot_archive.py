"""Archive, prove, and retire finite migration rollback snapshots."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import psycopg
from psycopg import sql

from services import backup
from services.pitr.checksums import ObjectChecksum
from services.pitr.crypto import decrypt_archive, encrypt_archive, source_identity
from services.pitr.object_store import ObjectStore, RemoteObjectAck
from services.pitr.restore_manifest import RestoreObject
from services.pitr.restore_object_store import GenerationPinnedObjectReader
from shared import db
from shared.pg_tools import pg_tool, throwaway_postgres
from shared.private_storage import write_private_bytes
from shared.proc import run_bounded
from shared.rollback_snapshot import is_rollback_snapshot_table

_ARCHIVE_SCHEMA_VERSION = 1
_ARCHIVE_DIRECTORY = "rollback-snapshot-archives"
_ARCHIVE_TIMEOUT_SECONDS = 20 * 60
_ARCHIVE_OBJECT_PREFIX = "rollback-snapshots"

SnapshotExporter = Callable[[str, Path], None]
SnapshotRestoreDrill = Callable[[str, Path], None]
SnapshotRetirer = Callable[[str], None]


class SnapshotArchiveError(RuntimeError):
    """A rollback snapshot could not complete its archive workflow."""


class SnapshotArchiveNotVerifiedError(SnapshotArchiveError):
    """Retirement was requested before an archived snapshot passed its drill."""


@dataclass(frozen=True)
class RollbackSnapshotArchive:
    """Durable evidence for one immutable archived rollback snapshot."""

    schema_version: int
    table: str
    object_name: str
    pin_token: str
    size: int
    checksum_algo: str
    checksum_value: str
    metadata: tuple[tuple[str, str], ...]
    source_sha256: str
    source_size: int
    archived_at: str
    verified_at: str | None = None

    def __post_init__(self) -> None:
        _require_snapshot_table(self.table)
        if self.schema_version != _ARCHIVE_SCHEMA_VERSION:
            raise ValueError("rollback snapshot archive schema is unsupported")
        if not self.object_name or not self.pin_token or self.size <= 0:
            raise ValueError("rollback snapshot archive lacks a pinned remote object")
        if self.source_size < 0 or len(self.source_sha256) != 64:
            raise ValueError("rollback snapshot archive lacks a source identity")
        if tuple(sorted(self.metadata)) != self.metadata:
            raise ValueError("rollback snapshot archive metadata must be canonical")
        ObjectChecksum(self.checksum_algo, self.checksum_value)
        _parse_timestamp(self.archived_at)
        if self.verified_at is not None:
            _parse_timestamp(self.verified_at)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, value: str) -> RollbackSnapshotArchive:
        parsed: object = json.loads(value)
        if not isinstance(parsed, dict):
            raise TypeError("rollback snapshot archive must be an object")
        raw = cast(dict[str, object], parsed)
        if set(raw) != set(cls.__dataclass_fields__):
            raise ValueError("rollback snapshot archive fields do not match schema")
        return cls(
            schema_version=_record_int(raw, "schema_version"),
            table=_record_string(raw, "table"),
            object_name=_record_string(raw, "object_name"),
            pin_token=_record_string(raw, "pin_token"),
            size=_record_int(raw, "size"),
            checksum_algo=_record_string(raw, "checksum_algo"),
            checksum_value=_record_string(raw, "checksum_value"),
            metadata=_record_metadata(raw),
            source_sha256=_record_string(raw, "source_sha256"),
            source_size=_record_int(raw, "source_size"),
            archived_at=_record_string(raw, "archived_at"),
            verified_at=_record_optional_string(raw, "verified_at"),
        )

    def restore_object(self) -> RestoreObject:
        return RestoreObject(
            archive_name=f"{self.table}.dump.enc",
            object_name=self.object_name,
            pin_token=self.pin_token,
            size=self.size,
            checksum_algo=self.checksum_algo,
            checksum_value=self.checksum_value,
            metadata=self.metadata,
        )


def archive_rollback_snapshot(
    table: str,
    *,
    ava_home: Path,
    key: bytes,
    key_id: str,
    export_table: SnapshotExporter,
    store: ObjectStore,
) -> RollbackSnapshotArchive:
    """Export, AES-GCM encrypt, and immutably publish one rollback snapshot.

    A completed local evidence record makes archive idempotent. If a process
    dies after publication but before the record write, the deterministic
    object name re-observes the already-published artifact and records its
    pinned identity on the retry.
    """
    _require_snapshot_table(table)
    record_path = _record_path(ava_home, table)
    if record_path.is_symlink():
        raise SnapshotArchiveError("rollback snapshot archive evidence is a symlink")
    if record_path.exists():
        return _read_record(record_path)

    with tempfile.TemporaryDirectory(prefix="ava-rollback-snapshot-") as temporary:
        scratch = Path(temporary)
        dump = scratch / f"{table}.dump"
        export_table(table, dump)
        if not dump.is_file() or dump.stat().st_size == 0:
            raise SnapshotArchiveError("rollback snapshot export produced no dump")
        source_sha256, _source_size = source_identity(dump)
        object_name = f"{_ARCHIVE_OBJECT_PREFIX}/{table}/{source_sha256}.dump.enc"
        ciphertext = scratch / f"{table}.dump.enc"
        encrypted = encrypt_archive(
            dump,
            ciphertext,
            key=key,
            key_id=key_id,
            object_name=object_name,
        )
        metadata = _archive_metadata(table, key_id)
        ack = store.put_wal_ciphertext_if_absent(ciphertext, object_name, metadata)

    _require_matching_ack(ack, object_name, metadata)
    record = RollbackSnapshotArchive(
        schema_version=_ARCHIVE_SCHEMA_VERSION,
        table=table,
        object_name=ack.object_name,
        pin_token=ack.pin_token,
        size=ack.size,
        checksum_algo=ack.checksum.algo,
        checksum_value=ack.checksum.value,
        metadata=tuple(sorted((str(key), str(value)) for key, value in ack.metadata.items())),
        source_sha256=encrypted.source_sha256,
        source_size=encrypted.source_size,
        archived_at=_timestamp(),
    )
    _write_record(record_path, record)
    return record


def verify_rollback_snapshot(
    table: str,
    *,
    ava_home: Path,
    key: bytes,
    reader: GenerationPinnedObjectReader,
    restore_drill: SnapshotRestoreDrill,
) -> RollbackSnapshotArchive:
    """Restore the recorded immutable generation into a disposable database."""
    _require_snapshot_table(table)
    record_path = _record_path(ava_home, table)
    record = _read_record(record_path)
    with tempfile.TemporaryDirectory(prefix="ava-rollback-snapshot-verify-") as temporary:
        scratch = Path(temporary)
        ciphertext = scratch / f"{table}.dump.enc"
        plaintext = scratch / f"{table}.dump"
        reader.download_exact(record.restore_object(), ciphertext)
        decrypt_archive(ciphertext, plaintext, key=key)
        restore_drill(table, plaintext)
    verified = replace(record, verified_at=_timestamp())
    _write_record(record_path, verified)
    return verified


def retire_rollback_snapshot(
    table: str,
    *,
    ava_home: Path,
    drop_table: SnapshotRetirer,
) -> RollbackSnapshotArchive:
    """Drop one rollback snapshot only after local archive proof is complete."""
    _require_snapshot_table(table)
    record = _read_record(_record_path(ava_home, table))
    if record.verified_at is None:
        raise SnapshotArchiveNotVerifiedError(
            f"rollback snapshot {table!r} cannot retire before archive verification succeeds"
        )
    drop_table(table)
    return record


def export_rollback_snapshot_table(table: str, destination: Path) -> None:
    """Create an owner-only custom-format dump of one finite snapshot table."""
    _require_snapshot_table(table)
    conninfo, password = backup._passwordless_conninfo(db.direct_db_url())
    destination.touch(mode=0o600, exist_ok=False)
    destination.chmod(0o600)
    result = run_bounded(
        [
            str(pg_tool("pg_dump")),
            "--format=custom",
            "--compress=zstd:3",
            "--table",
            f"public.{table}",
            "--file",
            str(destination),
            "--dbname",
            conninfo,
        ],
        timeout=_ARCHIVE_TIMEOUT_SECONDS,
        capture_output=True,
        env={"PGPASSWORD": password} if password else {},
    )
    if result.returncode != 0:
        raise SnapshotArchiveError(f"pg_dump exited {result.returncode}")


def restore_rollback_snapshot_table(table: str, dump: Path) -> None:
    """Prove a table archive restores and can be read in throwaway PostgreSQL."""
    _require_snapshot_table(table)
    with throwaway_postgres() as scratch_db_url:
        result = run_bounded(
            [
                str(pg_tool("pg_restore")),
                "--no-owner",
                "--no-privileges",
                "--dbname",
                scratch_db_url,
                str(dump),
            ],
            timeout=_ARCHIVE_TIMEOUT_SECONDS,
            capture_output=True,
        )
        if result.returncode != 0:
            raise SnapshotArchiveError(f"pg_restore exited {result.returncode}")
        with psycopg.connect(scratch_db_url, autocommit=True) as connection:
            exists = connection.execute("SELECT to_regclass(%s)", (f"public.{table}",)).fetchone()
            if exists is None or exists[0] is None:
                raise SnapshotArchiveError("restore drill did not recreate the rollback snapshot")
            row = connection.execute(
                sql.SQL("SELECT count(*) FROM {}.{}").format(
                    sql.Identifier("public"), sql.Identifier(table)
                )
            ).fetchone()
            if row is None:
                raise SnapshotArchiveError("restore drill could not read the rollback snapshot")


def drop_rollback_snapshot_table(table: str) -> None:
    """Retire one finite snapshot with an idempotent direct Postgres DDL call."""
    _require_snapshot_table(table)
    with db.connect(direct=True, autocommit=True) as connection:
        connection.execute(
            sql.SQL("DROP TABLE IF EXISTS {}.{}").format(
                sql.Identifier("public"), sql.Identifier(table)
            )
        )


def _record_path(ava_home: Path, table: str) -> Path:
    return ava_home / _ARCHIVE_DIRECTORY / f"{table}.json"


def _read_record(path: Path) -> RollbackSnapshotArchive:
    if path.is_symlink():
        raise SnapshotArchiveError("rollback snapshot archive evidence is a symlink")
    try:
        return RollbackSnapshotArchive.from_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SnapshotArchiveNotVerifiedError(
            f"rollback snapshot {path.stem!r} cannot retire before archive verification succeeds"
        ) from exc
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SnapshotArchiveError("rollback snapshot archive evidence is invalid") from exc


def _write_record(path: Path, record: RollbackSnapshotArchive) -> None:
    write_private_bytes(path, (record.to_json() + "\n").encode())


def _archive_metadata(table: str, key_id: str) -> dict[str, str]:
    return {
        "ava-artifact-kind": "rollback-snapshot",
        "ava-key-id": key_id,
        "ava-rollback-snapshot-table": table,
    }


def _require_matching_ack(
    ack: RemoteObjectAck, object_name: str, metadata: Mapping[str, str]
) -> None:
    if (
        ack.object_name != object_name
        or not ack.pin_token
        or ack.size <= 0
        or dict(ack.metadata) != dict(metadata)
    ):
        raise SnapshotArchiveError("offsite archive acknowledgement differs from the snapshot")


def _require_snapshot_table(table: str) -> None:
    if not is_rollback_snapshot_table(table):
        raise ValueError("rollback snapshot table must use the *_backfill_* naming convention")


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _parse_timestamp(value: str) -> None:
    if datetime.fromisoformat(value).tzinfo is None:
        raise ValueError("rollback snapshot archive timestamp must be timezone-aware")


def _record_string(raw: Mapping[str, object], name: str) -> str:
    value = raw[name]
    if not isinstance(value, str):
        raise TypeError(f"rollback snapshot archive field {name!r} must be a string")
    return value


def _record_optional_string(raw: Mapping[str, object], name: str) -> str | None:
    value = raw[name]
    if value is not None and not isinstance(value, str):
        raise TypeError(f"rollback snapshot archive field {name!r} must be a string or null")
    return value


def _record_int(raw: Mapping[str, object], name: str) -> int:
    value = raw[name]
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"rollback snapshot archive field {name!r} must be an integer")
    return value


def _record_metadata(raw: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    value = raw["metadata"]
    if not isinstance(value, list):
        raise TypeError("rollback snapshot archive metadata is invalid")
    items = cast(list[object], value)
    metadata: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, list):
            raise TypeError("rollback snapshot archive metadata entry is invalid")
        entry = cast(list[object], item)
        if len(entry) != 2 or not isinstance(entry[0], str) or not isinstance(entry[1], str):
            raise TypeError("rollback snapshot archive metadata entry is invalid")
        metadata.append((entry[0], entry[1]))
    return tuple(metadata)
