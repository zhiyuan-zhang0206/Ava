"""Explicit, resumable CLI boundary for activating physical PITR."""

from __future__ import annotations

# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
import hashlib
import json
import shutil
import stat
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import cast

import psutil
import psycopg
from google.cloud import storage
from google.oauth2 import service_account
from psycopg import sql

from services.pitr.activation_state import (
    ActivationRecord,
    load_record,
    lock_path,
    write_record,
)
from shared.config import settings
from shared.paths import ava_home
from shared.pg_tools import pg_tool
from shared.platform import LockTimeoutError, file_lock
from shared.private_storage import ensure_private_dir

_EMERGENCY_FLOOR_BYTES = 4 * 1024**3


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _credential_identity(path: Path) -> tuple[str, str, str]:
    raw_value: object = json.loads(path.read_bytes())
    if not isinstance(raw_value, dict):
        raise TypeError(f"{path} is not a service-account credential")
    value = cast(dict[str, object], raw_value)
    if value.get("type") != "service_account":
        raise RuntimeError(f"{path} is not a service-account credential")
    fields: list[str] = []
    for name in ("client_email", "project_id", "private_key_id"):
        field = value[name]
        if not isinstance(field, str) or not field:
            raise RuntimeError(f"{path} service-account {name} is missing")
        fields.append(field)
    return fields[0], fields[1], fields[2]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _probe_bucket(credentials_path: Path) -> str:
    credentials = service_account.Credentials.from_service_account_file(str(credentials_path))
    client = storage.Client(
        project=settings.physical_backup.pitr_gcs_project, credentials=credentials
    )
    bucket = client.get_bucket(settings.physical_backup.pitr_gcs_bucket, timeout=30)
    location = str(bucket.location or "").upper()
    if not location:
        raise RuntimeError("PITR bucket metadata omitted its region")
    return location


def _validate_secrets() -> None:
    config = settings.physical_backup
    key = config.pitr_backup_key_file
    uploader = config.pitr_gcs_credentials_file
    viewer = config.pitr_restore_gcs_credentials_file
    if key is None or uploader is None or viewer is None:
        raise RuntimeError("backup key plus uploader and viewer credentials are required")
    for secret in (key, uploader, viewer):
        if not secret.is_file() or secret.is_symlink() or _mode(secret) != 0o600:
            raise RuntimeError(f"PITR secret is unsafe: {secret}")
    if key.stat().st_size != 32:
        raise RuntimeError("PITR backup key must be exactly 32 bytes")
    uploader_id = _credential_identity(uploader)
    viewer_id = _credential_identity(viewer)
    if uploader_id == viewer_id:
        raise RuntimeError("uploader and viewer service-account identities must differ")
    if uploader_id[1] != config.pitr_gcs_project or viewer_id[1] != config.pitr_gcs_project:
        raise RuntimeError("PITR service-account project differs from configured GCS project")
    uploader_location = _probe_bucket(uploader)
    viewer_location = _probe_bucket(viewer)
    if uploader_location != viewer_location:
        raise RuntimeError("uploader and viewer resolved different bucket regions")


def _read_pg_state() -> dict[str, str]:
    from cli.commands._cluster_instance import pg_admin_url
    from shared.cluster import get_record, record_postgres_port

    record = get_record(ava_home())
    if record is None:
        raise RuntimeError("cluster registry record is missing")

    def scalar(conn: psycopg.Connection[tuple[object, ...]], query: sql.Composable) -> object:
        row = conn.execute(query).fetchone()
        if row is None:
            raise RuntimeError(f"PostgreSQL returned no row for {query!r}")
        return row[0]

    with psycopg.connect(pg_admin_url(record_postgres_port(record)), autocommit=True) as conn:
        system_id = str(scalar(conn, sql.SQL("SELECT system_identifier FROM pg_control_system()")))
        server_version = int(str(scalar(conn, sql.SQL("SHOW server_version_num"))))
        current = {
            name: str(scalar(conn, sql.SQL("SHOW {}").format(sql.Identifier(name))))
            for name in ("archive_mode", "archive_command", "archive_timeout", "wal_compression")
        }
        current["data_directory"] = str(scalar(conn, sql.SQL("SHOW data_directory")))
        current["port"] = str(scalar(conn, sql.SQL("SHOW port")))
        current["postmaster_started_at"] = str(
            scalar(conn, sql.SQL("SELECT pg_postmaster_start_time()::text"))
        )
    if server_version // 10000 != 17:
        raise RuntimeError("running PostgreSQL server is not major version 17")
    current["system_identifier"] = system_id
    expected_data = (ava_home() / "pg").resolve(strict=True)
    if Path(current["data_directory"]).resolve(strict=True) != expected_data:
        raise RuntimeError("live PostgreSQL data_directory differs from this AVA_HOME")
    if int(current["port"]) != record_postgres_port(record):
        raise RuntimeError("live PostgreSQL port differs from the cluster registry")
    pid_path = expected_data / "postmaster.pid"
    pid = int(pid_path.read_text().splitlines()[0])
    process = psutil.Process(pid)
    current["postmaster_pid"] = str(pid)
    current["postmaster_create_time"] = str(process.create_time())
    postmaster_started = datetime.fromisoformat(current["postmaster_started_at"])
    if abs(postmaster_started.timestamp() - process.create_time()) > 5:
        raise RuntimeError("postmaster PID create-time differs from PostgreSQL start time")
    control = subprocess.run(
        [str(pg_tool("pg_controldata")), str(expected_data)],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    if control.returncode:
        raise RuntimeError("pg_controldata failed for the live cluster data directory")
    control_id = next(
        (
            line.split(":", 1)[1].strip()
            for line in control.stdout.splitlines()
            if line.startswith("Database system identifier:")
        ),
        None,
    )
    if control_id != system_id:
        raise RuntimeError("pg_controldata system identifier differs from the live server")
    return current


def _shadow_readiness() -> dict[str, str]:
    """Validate every local prerequisite without changing PostgreSQL or config."""
    config = settings.physical_backup
    root = ava_home() / "physical-backup"
    shim = ava_home() / "runtime" / "pg-archive" / "archive-shim"
    required_dirs = (root, root / "spool", root / "ack", root / "staging")
    if not shim.is_file() or shim.is_symlink() or _mode(shim) != 0o700:
        raise RuntimeError("stable archive shim is missing, symlinked, or not mode 0700")
    result = subprocess.run(
        [str(shim), "--self-check"], capture_output=True, check=False, timeout=10
    )
    if result.returncode:
        raise RuntimeError("stable archive shim self-check failed")
    source = Path(__file__).resolve().parents[2] / "services" / "pitr" / "archive_shim.py"
    if _sha256(source) != _sha256(shim):
        raise RuntimeError("installed archive shim differs from the current source")
    for directory in required_dirs:
        if not directory.is_dir() or directory.is_symlink() or _mode(directory) != 0o700:
            raise RuntimeError(f"private PITR directory is unsafe: {directory}")
    _validate_secrets()
    if not config.pitr_gcs_bucket or not config.pitr_gcs_prefix:
        raise RuntimeError("PITR bucket and prefix must be configured")
    if config.pitr_enabled or config.pitr_base_backup_enabled or config.pitr_restore_proof_enabled:
        raise RuntimeError("shadow readiness requires all PITR service flags to remain off")
    pg_version = (ava_home() / "pg" / "PG_VERSION").read_text().strip()
    if pg_version != "17":
        raise RuntimeError(f"PITR activation requires PostgreSQL 17, found {pg_version!r}")
    usage = shutil.disk_usage(root)
    required_free = config.pitr_spool_hard_bytes + _EMERGENCY_FLOOR_BYTES
    if usage.free < required_free:
        raise RuntimeError(
            f"PITR activation needs {required_free} free bytes for spool + emergency floor; "
            f"only {usage.free} available"
        )
    current = _read_pg_state()
    if current["archive_mode"] != "off" or current["archive_command"].strip():
        raise RuntimeError("shadow readiness requires archive_mode=off and no archive_command")
    return current


def _print_record(record: ActivationRecord | None) -> int:
    if record is None:
        print("PITR activation: not started")
        return 0
    print(f"PITR activation: {record.phase}")
    print(f"  operation: {record.operation_id}")
    print(f"  started_at: {record.started_at}")
    print(f"  updated_at: {record.updated_at}")
    if record.pre_activation_snapshot:
        print(f"  logical recovery floor: {record.pre_activation_snapshot}")
    if record.error:
        print(f"  error: {record.error}")
    return 0


def _validate_snapshot(record: ActivationRecord) -> None:
    if not record.pre_activation_snapshot:
        return
    path = Path(record.pre_activation_snapshot)
    if path.is_symlink() or not path.is_file() or _mode(path) != 0o600:
        raise RuntimeError("pre-activation snapshot is missing or unsafe")
    from cli.commands._update_git import _verify_snapshot_artifact

    _verify_snapshot_artifact(path)


def cmd_pitr_status() -> int:
    record = load_record(ava_home())
    if record is not None and record.phase not in {"protected", "rolled_back"}:
        _validate_snapshot(record)
    return _print_record(record)


def _save_error(home: Path, record: ActivationRecord, exc: BaseException) -> None:
    write_record(home, record.advance(record.phase, error=type(exc).__name__))


def _require_same_pg_state(expected: dict[str, str] | None, boundary: str) -> None:
    if expected is None or _read_pg_state() != expected:
        raise RuntimeError(f"PostgreSQL identity/settings changed {boundary}")


def _prepare_snapshot(home: Path, record: ActivationRecord) -> ActivationRecord:
    from cli.commands._update_git import _verify_snapshot_artifact, snapshot_pre_activation_data
    from services.backup import activation_snapshots_since

    _require_same_pg_state(record.pre_activation_pg_settings, "before snapshot")
    candidates = activation_snapshots_since(datetime.fromisoformat(record.started_at))
    if candidates:
        snapshot = candidates[-1]
        _verify_snapshot_artifact(snapshot)
    else:
        snapshot = snapshot_pre_activation_data()
    _require_same_pg_state(record.pre_activation_pg_settings, "during snapshot")
    record = record.advance(
        "snapshot_verified",
        pre_activation_snapshot=str(snapshot),
        error=None,
    )
    write_record(home, record)
    return record


def _advance_activation(home: Path, record: ActivationRecord) -> ActivationRecord:
    if record.phase == "shadow":
        record = record.advance(
            "snapshot_pending",
            pre_activation_pg_settings=_shadow_readiness(),
            error=None,
        )
        write_record(home, record)
    if record.phase == "snapshot_pending":
        record = _prepare_snapshot(home, record)
    if record.phase == "snapshot_verified":
        _validate_snapshot(record)
        record = record.advance("wal_config_pending", error=None)
        write_record(home, record)
    if record.phase != "wal_config_pending":
        raise RuntimeError(f"activation phase {record.phase!r} is not handled by this CLI")
    _validate_snapshot(record)
    return record


def _rollback_record(home: Path, record: ActivationRecord) -> None:
    allowed = {"shadow", "snapshot_pending", "snapshot_verified", "wal_config_pending"}
    if record.phase not in allowed:
        raise RuntimeError(f"rollback for phase {record.phase!r} is not implemented")
    from shared.cluster_lock import acquire_update_lock, release_update_lock

    holder = f"pitr-rollback:{record.operation_id}"
    if not acquire_update_lock(holder, kind="update"):
        raise RuntimeError("cluster update/maintenance owner is already active")
    try:
        write_record(home, record.advance("rolled_back", error=None))
    finally:
        release_update_lock(holder)


def cmd_pitr_activate(*, origin: str) -> int:
    """Advance only through shadow readiness and the first-rollout snapshot.

    PostgreSQL remains untouched at ``wal_config_pending``.  The next delivery
    owns the privileged archive configuration, restart and remote-ACK gate;
    keeping that boundary explicit prevents a foundation command from claiming
    protection it cannot yet prove.
    """
    home = ava_home()
    ensure_private_dir(home / "physical-backup" / "activation")
    try:
        with file_lock(lock_path(home), timeout_s=5):
            record = load_record(home)
            if record is None or record.phase == "rolled_back":
                record = ActivationRecord.start(operation_id=str(uuid.uuid4()), origin=origin)
                write_record(home, record)
            from shared.cluster_lock import acquire_update_lock, release_update_lock

            holder = f"pitr-activation:{record.operation_id}"
            if not acquire_update_lock(holder, kind="update"):
                exc = RuntimeError("cluster update/maintenance owner is already active")
                _save_error(home, record, exc)
                raise exc
            try:
                record = _advance_activation(home, record)
            except BaseException as exc:
                _save_error(home, record, exc)
                raise
            finally:
                release_update_lock(holder)
    except (LockTimeoutError, RuntimeError, OSError, ValueError) as exc:
        print(f"PITR activation refused: {type(exc).__name__}", file=sys.stderr)
        return 1
    _print_record(record)
    print(
        "  PostgreSQL remains unchanged in wal_config_pending. WAL activation requires the "
        "follow-on remote-smoke/restart gate; do not edit postgresql.conf manually."
    )
    return 0


def cmd_pitr_rollback() -> int:
    """Record a safe rollback request without deleting backups or remote objects."""
    home = ava_home()
    ensure_private_dir(home / "physical-backup" / "activation")
    record: ActivationRecord | None = None
    try:
        with file_lock(lock_path(home), timeout_s=5):
            record = load_record(home)
            if record is None:
                print("PITR activation: not started (rollback is a no-op)")
                return 0
            if record.phase == "rolled_back":
                return _print_record(record)
            try:
                _rollback_record(home, record)
            except BaseException as exc:
                _save_error(home, record, exc)
                raise
    except (LockTimeoutError, RuntimeError, OSError, ValueError) as exc:
        print(f"PITR rollback refused: {type(exc).__name__}", file=sys.stderr)
        return 1
    print("PITR activation rolled back; logical/remote backup data was preserved")
    return 0
