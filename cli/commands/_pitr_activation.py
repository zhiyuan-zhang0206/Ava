"""Explicit, resumable CLI boundary for activating physical PITR."""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg

from services.pitr.activation_state import ActivationRecord, load_record, write_record
from shared.config import settings
from shared.paths import ava_home

_EMERGENCY_FLOOR_BYTES = 4 * 1024**3


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _credential_identity(path: Path) -> tuple[str, str, str]:
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("type") != "service_account":
        raise RuntimeError(f"{path} is not a service-account credential")
    return str(value["client_email"]), str(value["project_id"]), str(value["private_key_id"])


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


def _read_pg_state() -> dict[str, str]:
    from cli.commands._cluster_instance import pg_admin_url
    from shared.cluster import get_record, record_postgres_port

    record = get_record(ava_home())
    if record is None:
        raise RuntimeError("cluster registry record is missing")
    with psycopg.connect(pg_admin_url(record_postgres_port(record)), autocommit=True) as conn:
        system_id = str(
            conn.execute("SELECT system_identifier FROM pg_control_system()").fetchone()[0]
        )
        server_version = int(conn.execute("SHOW server_version_num").fetchone()[0])
        current = {
            name: str(conn.execute(f"SHOW {name}").fetchone()[0])
            for name in ("archive_mode", "archive_command", "archive_timeout", "wal_compression")
        }
    if server_version // 10000 != 17:
        raise RuntimeError("running PostgreSQL server is not major version 17")
    current["system_identifier"] = system_id
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
    for directory in required_dirs:
        if not directory.is_dir() or directory.is_symlink() or _mode(directory) != 0o700:
            raise RuntimeError(f"private PITR directory is unsafe: {directory}")
    _validate_secrets()
    if not config.pitr_gcs_bucket or not config.pitr_gcs_prefix:
        raise RuntimeError("PITR bucket and prefix must be configured")
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
    return _read_pg_state()


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


def cmd_pitr_status() -> int:
    return _print_record(load_record(ava_home()))


def cmd_pitr_activate(*, origin: str) -> int:
    """Advance only through shadow readiness and the first-rollout snapshot.

    PostgreSQL remains untouched at ``wal_config_pending``.  The next delivery
    owns the privileged archive configuration, restart and remote-ACK gate;
    keeping that boundary explicit prevents a foundation command from claiming
    protection it cannot yet prove.
    """
    home = ava_home()
    record = load_record(home)
    if record is None or record.phase == "rolled_back":
        record = ActivationRecord.start(operation_id=str(uuid.uuid4()), origin=origin)
        write_record(home, record)
    if record.phase == "shadow":
        pg_settings = _shadow_readiness()
        from cli.commands._update_git import snapshot_pre_activation_data

        snapshot = snapshot_pre_activation_data()
        record = record.advance(
            "snapshot_verified",
            pre_activation_snapshot=str(snapshot),
            pre_activation_pg_settings=pg_settings,
        )
        write_record(home, record)
    if record.phase == "snapshot_verified":
        record = record.advance("wal_config_pending")
        write_record(home, record)
    _print_record(record)
    print(
        "  PostgreSQL remains unchanged. WAL activation requires the follow-on "
        "remote-smoke/restart gate; do not edit postgresql.conf manually."
    )
    return 0


def cmd_pitr_rollback() -> int:
    """Record a safe rollback request without deleting backups or remote objects."""
    home = ava_home()
    record = load_record(home)
    if record is None:
        print("PITR activation: not started (rollback is a no-op)")
        return 0
    if record.phase == "rolled_back":
        return _print_record(record)
    # PR-A never mutates PostgreSQL.  Later activation phases must teach this
    # same verb how to restore the exact settings snapshotted above before they
    # can become reachable.
    if record.phase not in {"shadow", "snapshot_verified", "wal_config_pending"}:
        print("PITR rollback is unavailable for this newer activation phase", file=sys.stderr)
        return 1
    write_record(home, record.advance("rolled_back"))
    print("PITR activation rolled back; logical/remote backup data was preserved")
    return 0
