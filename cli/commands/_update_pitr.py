"""Incremental pre-update recovery points on an already drilled PITR chain.

This does not create a new protected proof: the base's existing restore drill
and fresh, exact remote WAL identities are separate evidence. Recovery is
physical, for the whole PostgreSQL instance, within the configured PITR
retention window. No database content is exported on this path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.conninfo import conninfo_to_dict

from services.pitr.activation_runtime import pitr_admin_url
from services.pitr.activation_state import lock_path
from services.pitr.base_manifest import CandidateManifest, WalRange, _lsn
from services.pitr.restore_manifest import (
    ProtectedManifest,
    RestoreObject,
    required_archive_names,
    wal_objects_from_acks,
)
from services.pitr.retention_inventory import InventorySnapshot
from services.pitr.retention_manifest import RetentionObject
from services.pitr.store_factory import get_store_group
from shared.cluster import _swap_db
from shared.config import settings
from shared.paths import ava_home
from shared.platform import file_lock
from shared.private_storage import ensure_private_dir

RECOVERY_SUFFIX = ".pre-update.pitr.json"
WAL_WAIT_SECONDS = 300


class RecoveryPointError(RuntimeError):
    """Safe operator-facing refusal; never includes a database URL or SDK error."""


@dataclass(frozen=True)
class DatabaseIdentity:
    system_identifier: str
    database_name: str
    postgres_major: int
    timeline: int
    wal_segment_size: int


def _identity(conn: psycopg.Connection) -> DatabaseIdentity:
    row = conn.execute(
        "SELECT system_identifier::text, current_database(), "
        "current_setting('server_version_num')::int / 10000, timeline_id, "
        "pg_size_bytes(current_setting('wal_segment_size')) "
        "FROM pg_control_system(), pg_control_checkpoint() "
        "WHERE NOT pg_is_in_recovery() AND current_setting('archive_mode') = 'on'"
    ).fetchone()
    if row is None:
        raise RecoveryPointError("PITR update requires a writable PostgreSQL with archiving on")
    return DatabaseIdentity(str(row[0]), str(row[1]), int(row[2]), int(row[3]), int(row[4]))


def select_chain(root: Path, identity: DatabaseIdentity) -> ProtectedManifest:
    """Use the newest drilled scheduled chain; never silently choose an old one."""
    paths = sorted(
        path
        for path in (root / "protected-manifests").glob("*.json")
        if re.fullmatch(r"[0-9]{8}T[0-9]{6}Z\.json", path.name)
    )
    if not paths:
        raise RecoveryPointError("PITR update requires a protected scheduled base backup")
    path = paths[-1]
    proof = ProtectedManifest.from_json(path.read_text())
    candidate = proof.candidate
    authoritative = CandidateManifest.from_json(
        (root / "base-manifests" / f"{path.stem}.candidate.json").read_text()
    )
    if candidate != authoritative or proof.chain_id != path.stem:
        raise RecoveryPointError("PITR protected chain differs from its authoritative candidate")
    expected = DatabaseIdentity(
        candidate.system_identifier,
        candidate.database_name,
        candidate.postgres_major,
        candidate.timeline,
        candidate.wal_segment_size,
    )
    if expected != identity:
        raise RecoveryPointError("PITR protected base belongs to another database or timeline")
    if any(item.timeline != identity.timeline for item in candidate.wal_ranges):
        raise RecoveryPointError("PITR update requires a single authenticated timeline")
    return proof


def verify_inventory(objects: tuple[RestoreObject, ...], inventory: InventorySnapshot) -> None:
    """Require every byte range's ACK to match a fresh viewer-only remote listing."""
    by_name: dict[str, RetentionObject] = {}
    for item in inventory.objects:
        if item.object_name in by_name:
            raise RecoveryPointError("PITR inventory has ambiguous object generations")
        by_name[item.object_name] = item
    for expected in objects:
        actual = by_name.get(expected.object_name)
        if actual is None or (
            actual.pin_token,
            actual.size,
            actual.checksum_algo,
            actual.checksum_value,
            actual.metadata,
        ) != (
            expected.pin_token,
            expected.size,
            expected.checksum_algo,
            expected.checksum_value,
            expected.metadata,
        ):
            raise RecoveryPointError(
                "PITR recovery object is missing or differs from its immutable ACK"
            )


def _wait_wal(root: Path, names: tuple[str, ...]) -> tuple[RestoreObject, ...]:
    deadline = time.monotonic() + WAL_WAIT_SECONDS
    while True:
        missing = [name for name in names if not (root / "ack" / f"{name}.ack.json").is_file()]
        if not missing:
            return wal_objects_from_acks(ack_dir=root / "ack", archive_names=names)
        if time.monotonic() >= deadline:
            raise RecoveryPointError(f"PITR WAL archive has {len(missing)} unacknowledged segments")
        time.sleep(2)


def _write_receipt(path: Path, payload: bytes) -> None:
    with path.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def create_recovery_point(target_sha: str) -> Path:
    if re.fullmatch(r"[0-9a-f]{40}", target_sha) is None:
        raise ValueError("PITR recovery point requires the full target commit")
    config = settings.physical_backup
    if settings.data_plane.is_remote:
        raise RecoveryPointError("PITR update does not support a remotely managed data plane")
    if not config.pitr_enabled or not config.pitr_restore_proof_enabled:
        raise RecoveryPointError("PITR update requires enabled archiving and restore proofs")
    root = ava_home() / "physical-backup"
    # The same owner lock excludes activation and a new scheduled chain while
    # choosing the newest chain. Retention keeps at least the newest two chains.
    with file_lock(lock_path(ava_home()), timeout_s=10):
        database = str(conninfo_to_dict(settings.data_plane.db_url)["dbname"])
        with psycopg.connect(
            _swap_db(pitr_admin_url(), database),
            autocommit=True,
            connect_timeout=5,
            options="-c statement_timeout=10000",
        ) as conn:
            identity = _identity(conn)
            proof = select_chain(root, identity)
            point_name = f"ava_update_{target_sha[:12]}_{uuid4().hex[:12]}"
            row = conn.execute("SELECT pg_create_restore_point(%s)::text", (point_name,)).fetchone()
            if row is None:
                raise RecoveryPointError("PostgreSQL omitted the pre-update restore point")
            target_lsn = str(row[0])
            if _lsn(target_lsn) < _lsn(proof.target_lsn):
                raise RecoveryPointError("PITR restore point precedes the protected base")
            conn.execute("SELECT pg_switch_wal()")
            if _identity(conn) != identity:
                raise RecoveryPointError("PostgreSQL identity changed during the WAL switch")
        names = required_archive_names(
            (WalRange(identity.timeline, proof.candidate.start_lsn, target_lsn),),
            identity.wal_segment_size,
        )
        wal = _wait_wal(root, names)
        if wal[: len(proof.wal)] != proof.wal:
            raise RecoveryPointError("PITR archived WAL differs from the drilled base chain")
        objects = (proof.base, *wal)
        if any(dict(item.metadata)["ava-key-id"] != config.pitr_backup_key_id for item in objects):
            raise RecoveryPointError("PITR recovery chain requires a different decryption key")
        group = get_store_group()
        verify_inventory(objects, group.retention_inventory_reader().snapshot())
        receipt = {
            "version": 1,
            "kind": "pre-update-pitr",
            "target_sha": target_sha,
            "created_at": datetime.now(UTC).isoformat(),
            "restore_point_name": point_name,
            "target_lsn": target_lsn,
            "identity": asdict(identity),
            "protected_base": json.loads(proof.to_json()),
            "wal": [asdict(item) for item in wal],
            "retained_weekly_chains": config.pitr_retained_weekly_chains,
        }
        payload = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
        name = f"{point_name}{RECOVERY_SUFFIX}"
        group.protected_manifest_publisher().put_manifest_if_absent(
            payload=payload,
            object_name=f"{config.pitr_gcs_prefix.rstrip('/')}/protected/update-recovery/{name}",
            metadata={"ava-recovery-sha256": hashlib.sha256(payload).hexdigest()},
        )
        destination = ensure_private_dir(root / "update-recovery") / name
        _write_receipt(destination, payload)
        return destination


if __name__ == "__main__":
    try:
        print(create_recovery_point(sys.argv[1]), flush=True)
    except RecoveryPointError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        # SDK and libpq errors can contain credentials. Keep their details out
        # of the rollout stream, including chained exception tracebacks.
        print(f"PITR recovery point failed ({type(exc).__name__})", file=sys.stderr)
        sys.exit(1)
