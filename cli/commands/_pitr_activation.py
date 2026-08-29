"""Explicit, resumable CLI boundary for activating physical PITR."""

from __future__ import annotations

# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
import hashlib
import json
import re
import shutil
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import LiteralString, cast

import psutil
import psycopg
from google.cloud import storage
from google.oauth2 import service_account
from psycopg.conninfo import make_conninfo

from services.pitr.activation_runtime import (
    _archive_settings,
    _desired_archive_settings,
    _enable_pitr_services,
    _file_evidence,
    _pitr_env_baseline,
    _restore_exact_file,
    _settings_digest,
    capture_pitr_env_baseline,
)
from services.pitr.activation_runtime import (
    forced_candidate as _forced_candidate,
)
from services.pitr.activation_runtime import (
    remote_wal_proof as _remote_wal_proof,
)
from services.pitr.activation_runtime import (
    restore_candidate as _restore_candidate,
)
from services.pitr.activation_state import (
    ActivationPhase,
    ActivationRecord,
    consume_restart_handoff,
    load_record,
    lock_path,
    mark_pre_mutation_rolled_back,
    rearm_restart_handoff,
    write_record,
    write_record_cas,
)
from shared.config import settings
from shared.paths import ava_home
from shared.pg_tools import pg_tool
from shared.platform import LockTimeoutError, file_lock
from shared.private_storage import ensure_private_dir

_EMERGENCY_FLOOR_BYTES = 4 * 1024**3


@dataclass(frozen=True)
class ShadowReadiness:
    pg: dict[str, str]
    credentials: dict[str, str]


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


def _validate_secrets() -> dict[str, str]:
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
    if uploader_id[0] == viewer_id[0]:
        raise RuntimeError("uploader and viewer service-account identities must differ")
    if uploader_id[1] != config.pitr_gcs_project or viewer_id[1] != config.pitr_gcs_project:
        raise RuntimeError("PITR service-account project differs from configured GCS project")
    uploader_location = _probe_bucket(uploader)
    viewer_location = _probe_bucket(viewer)
    if uploader_location != viewer_location:
        raise RuntimeError("uploader and viewer resolved different bucket regions")
    return {
        "uploader_client_email": uploader_id[0],
        "uploader_project_id": uploader_id[1],
        "uploader_private_key_id": uploader_id[2],
        "viewer_client_email": viewer_id[0],
        "viewer_project_id": viewer_id[1],
        "viewer_private_key_id": viewer_id[2],
        "bucket_location": uploader_location,
    }


def _read_pg_state() -> dict[str, str]:
    from cli.commands._cluster_instance import pg_admin_url
    from shared.cluster import get_record, record_postgres_port

    record = get_record(ava_home())
    if record is None:
        raise RuntimeError("cluster registry record is missing")

    def scalar(conn: psycopg.Connection[tuple[object, ...]], query: LiteralString) -> object:
        row = conn.execute(query).fetchone()
        if row is None:
            raise RuntimeError(f"PostgreSQL returned no row for {query!r}")
        return row[0]

    with psycopg.connect(pg_admin_url(record_postgres_port(record)), autocommit=True) as conn:
        system_id = str(scalar(conn, "SELECT system_identifier FROM pg_control_system()"))
        server_version = int(str(scalar(conn, "SHOW server_version_num")))
        current = {
            "archive_mode": str(scalar(conn, "SHOW archive_mode")),
            "archive_command": str(scalar(conn, "SHOW archive_command")),
            "archive_timeout": str(scalar(conn, "SHOW archive_timeout")),
            "wal_compression": str(scalar(conn, "SHOW wal_compression")),
        }
        current["data_directory"] = str(scalar(conn, "SHOW data_directory"))
        current["port"] = str(scalar(conn, "SHOW port"))
        current["postmaster_started_at"] = str(
            scalar(conn, "SELECT pg_postmaster_start_time()::text")
        )
    direct_url = make_conninfo(pg_admin_url(record_postgres_port(record)), dbname=record.db_name)
    with psycopg.connect(direct_url, autocommit=True) as conn:
        current["dbname"] = str(scalar(conn, "SELECT current_database()"))
        direct_system_id = str(scalar(conn, "SELECT system_identifier FROM pg_control_system()"))
    if current["dbname"] != record.db_name or direct_system_id != system_id:
        raise RuntimeError("direct dump target differs from the verified cluster database")
    current["direct_db_url"] = direct_url
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


def _shadow_readiness() -> ShadowReadiness:
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
    credential_evidence = _validate_secrets()
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
    return ShadowReadiness(pg=current, credentials=credential_evidence)


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
    latest = load_record(home)
    if latest is None or latest.operation_id != record.operation_id:
        raise RuntimeError("PITR activation operation changed while recording failure") from exc
    write_record(home, latest.advance(latest.phase, error=type(exc).__name__))


def _require_same_pg_state(expected: dict[str, str] | None, boundary: str) -> None:
    if expected is None or _read_pg_state() != expected:
        raise RuntimeError(f"PostgreSQL identity/settings changed {boundary}")


def _require_same_credentials(expected: dict[str, str] | None, boundary: str) -> None:
    if expected is None or _validate_secrets() != expected:
        raise RuntimeError(f"PITR credential/bucket evidence changed {boundary}")


def _prepare_snapshot(home: Path, record: ActivationRecord) -> ActivationRecord:
    from cli.commands._update_git import _verify_snapshot_artifact, snapshot_pre_activation_data
    from services.backup import activation_snapshot

    pg_settings = record.pre_activation_pg_settings
    if pg_settings is None:
        raise RuntimeError("PITR activation has no frozen PostgreSQL identity")
    _require_same_pg_state(pg_settings, "before snapshot")
    _require_same_credentials(record.pre_activation_credential_evidence, "before snapshot")
    existing = activation_snapshot(record.operation_id)
    if existing is not None:
        snapshot = existing
        _verify_snapshot_artifact(snapshot)
    else:
        snapshot = snapshot_pre_activation_data(
            operation_id=record.operation_id,
            db_url=pg_settings["direct_db_url"],
        )
    _require_same_pg_state(pg_settings, "during snapshot")
    _require_same_credentials(record.pre_activation_credential_evidence, "during snapshot")
    record = record.advance(
        "snapshot_verified",
        pre_activation_snapshot=str(snapshot),
        error=None,
    )
    write_record(home, record)
    return record


def _persist_transition(
    home: Path, record: ActivationRecord, phase: ActivationPhase, **changes: object
) -> ActivationRecord:
    replacement = record.advance(phase, **changes)
    write_record_cas(home, expected=record, replacement=replacement)
    return replacement


def _pg_auto_conf_baseline(home: Path, values: dict[str, str] | None = None) -> dict[str, str]:
    path = home / "pg" / "postgresql.auto.conf"
    text = path.read_text() if path.exists() else ""
    result: dict[str, str] = {}
    effective = _read_pg_state() if values is None else values
    for name in ("archive_mode", "archive_command", "archive_timeout", "wal_compression"):
        matches = [line for line in text.splitlines() if re.match(rf"^\s*{name}\s*=", line)]
        result[name] = effective[name] if matches else "__ABSENT__"
    return result


def _restart_ready(record: ActivationRecord, desired: dict[str, str]) -> bool:
    current = _read_pg_state()
    before = record.pre_activation_pg_settings
    identity_fields = ("system_identifier", "data_directory", "port", "dbname")
    if before is not None and any(
        before.get(name) != current.get(name) for name in identity_fields
    ):
        raise RuntimeError("PostgreSQL cluster identity changed across PITR restart")
    _require_same_credentials(record.pre_activation_credential_evidence, "across PITR restart")
    return (
        before is not None
        and current["postmaster_started_at"] != before["postmaster_started_at"]
        and _archive_settings(current) == desired
    )


def _prepare_wal_switch() -> dict[str, str]:
    from shared.db import direct_db_url

    with psycopg.connect(direct_db_url(), autocommit=True) as conn:
        row = conn.execute(
            "SELECT timeline_id::text, pg_walfile_name(pg_current_wal_lsn()), "
            "pg_current_wal_lsn()::text, failed_count::text, archived_count::text "
            "FROM pg_control_checkpoint(), pg_stat_archiver"
        ).fetchone()
    if row is None:
        raise RuntimeError("PostgreSQL omitted WAL switch evidence")
    evidence = {
        "timeline": str(row[0]),
        "segment": str(row[1]),
        "switch_lsn": str(row[2]),
        "failed_count": str(row[3]),
        "archived_count": str(row[4]),
        "switch_intent_at": datetime.now(UTC).isoformat(),
    }
    if (ava_home() / "physical-backup" / "ack" / f"{evidence['segment']}.ack.json").exists():
        raise RuntimeError("WAL switch target already has an ACK from an older operation")
    return evidence


def _switch_wal() -> str:
    from shared.db import direct_db_url

    with psycopg.connect(direct_db_url(), autocommit=True) as conn:
        row = conn.execute("SELECT pg_switch_wal()::text").fetchone()
    if row is None:
        raise RuntimeError("PostgreSQL omitted pg_switch_wal result")
    return str(row[0])


def _alter_archive_settings(values: dict[str, str]) -> None:
    from psycopg import sql

    from cli.commands._cluster_instance import pg_admin_url
    from shared.cluster import get_record, record_postgres_port

    cluster = get_record(ava_home())
    if cluster is None:
        raise RuntimeError("cluster registry record is missing")
    with psycopg.connect(pg_admin_url(record_postgres_port(cluster)), autocommit=True) as conn:
        for name, value in values.items():
            conn.execute(sql.SQL("ALTER SYSTEM SET {} = %s").format(sql.Identifier(name)), (value,))


def _advance_activation(home: Path, record: ActivationRecord) -> ActivationRecord:
    if record.phase == "shadow":
        readiness = _shadow_readiness()
        record = record.advance(
            "snapshot_pending",
            pre_activation_pg_settings=readiness.pg,
            pre_activation_credential_evidence=readiness.credentials,
            error=None,
        )
        write_record(home, record)
    if record.phase == "snapshot_pending":
        record = _prepare_snapshot(home, record)
    if record.phase == "snapshot_verified":
        _validate_snapshot(record)
        record = record.advance("wal_config_pending", error=None)
        write_record(home, record)
    desired = _desired_archive_settings(home)
    if record.phase == "wal_config_pending":
        _validate_snapshot(record)
        before = _archive_settings(_read_pg_state())
        env_b64, env_digest, env_baseline = capture_pitr_env_baseline(home / ".env")
        auto_b64, auto_digest = _file_evidence(home / "pg" / "postgresql.auto.conf")
        return _persist_transition(
            home,
            record,
            "wal_config_applying",
            wal_config_before_digest=_settings_digest(before),
            wal_config_desired_digest=_settings_digest(desired),
            pre_activation_pitr_env=env_baseline,
            pre_activation_pg_auto_conf=_pg_auto_conf_baseline(home, before),
            pre_activation_env_b64=env_b64,
            pre_activation_env_digest=env_digest,
            pre_activation_auto_conf_b64=auto_b64,
            pre_activation_auto_conf_digest=auto_digest,
            error=None,
        )
    if record.phase == "wal_config_applying":
        _alter_archive_settings(desired)
        owned_env = _enable_pitr_services()
        owned_env_digest = hashlib.sha256(owned_env).hexdigest()
        _owned_auto_b64, owned_auto_digest = _file_evidence(home / "pg" / "postgresql.auto.conf")
        handoff = str(uuid.uuid4())
        orchestration = str(uuid.uuid4())
        record = _persist_transition(
            home,
            record,
            "wal_restart_pending",
            restart_handoff=handoff,
            restart_orchestration=orchestration,
            rollback_expected_env_digest=owned_env_digest,
            rollback_expected_auto_conf_digest=owned_auto_digest,
            error=None,
        )
    if record.phase == "wal_restart_pending":
        if not _restart_ready(record, desired):
            return record
        exact = _prepare_wal_switch()
        record = _persist_transition(
            home,
            record,
            "wal_ack_pending",
            wal_exact_evidence=exact,
            wal_verification_deadline=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            error=None,
        )
    if record.phase == "wal_ack_pending":
        # Reissuing a switch after a crash is safe: immutable naming and the
        # persisted target segment make proof exact; the extra segment is retained.
        _switch_wal()
        ack, viewer = _remote_wal_proof(record)
        record = _persist_transition(
            home,
            record,
            "wal_remote_verified",
            wal_ack_evidence=ack,
            wal_viewer_proof=viewer,
            error=None,
        )
    if record.phase == "wal_remote_verified":
        stamp = datetime.fromisoformat(record.started_at).strftime("%Y%m%dT%H%M%SZ")
        chain_id = f"activation-{stamp}-{record.operation_id}"
        record = _persist_transition(
            home,
            record,
            "base_pending",
            candidate_chain_id=chain_id,
            error=None,
        )
    if record.phase == "base_pending":
        candidate_json, digest = _forced_candidate(record)
        record = _persist_transition(
            home,
            record,
            "restore_pending",
            protected_manifest=candidate_json,
            candidate_digest=digest,
            error=None,
        )
    if record.phase == "restore_pending":
        if record.protected_manifest is None:
            raise RuntimeError("activation candidate manifest is missing")
        protected, digest = _restore_candidate(record)
        record = _persist_transition(
            home,
            record,
            "protected",
            protected_manifest=protected,
            protected_digest=digest,
            error=None,
        )
    return record


def _rollback_record(home: Path, record: ActivationRecord) -> ActivationRecord:
    from shared.cluster_lock import acquire_update_lock, release_update_lock

    holder = f"pitr-rollback:{record.operation_id}"
    if not acquire_update_lock(holder, kind="update"):
        raise RuntimeError("cluster update/maintenance owner is already active")
    try:
        if record.phase == "rollback_restart_pending":
            current = _read_pg_state()
            expected = record.pre_activation_pg_settings or {}
            if current[
                "postmaster_started_at"
            ] == record.rollback_postmaster_started_at or _archive_settings(
                current
            ) != _archive_settings(expected):
                return record
            config = settings.physical_backup
            if any(
                (
                    config.pitr_enabled,
                    config.pitr_base_backup_enabled,
                    config.pitr_restore_proof_enabled,
                    config.pitr_retention_planner_enabled,
                )
            ):
                raise RuntimeError("PITR runtime gates remain enabled after rollback restart")
            if _pitr_env_baseline() != record.pre_activation_pitr_env:
                raise RuntimeError("PITR environment differs from rollback baseline")
            if _pg_auto_conf_baseline(home, current) != record.pre_activation_pg_auto_conf:
                raise RuntimeError("PostgreSQL ALTER SYSTEM ownership differs after rollback")
            if hashlib.sha256((home / ".env").read_bytes()).hexdigest() != (
                record.pre_activation_env_digest
            ):
                raise RuntimeError("exact PITR environment bytes differ after rollback")
            if (
                hashlib.sha256((home / "pg" / "postgresql.auto.conf").read_bytes()).hexdigest()
                != record.pre_activation_auto_conf_digest
            ):
                raise RuntimeError("exact PostgreSQL auto-conf bytes differ after rollback")
            return _persist_transition(home, record, "rolled_back", error=None)
        if record.phase == "rolled_back":
            return record
        if record.phase in {
            "shadow",
            "snapshot_pending",
            "snapshot_verified",
            "wal_config_pending",
        }:
            return mark_pre_mutation_rolled_back(home, record)
        current = _read_pg_state()
        before = record.pre_activation_pg_settings
        if before is None:
            raise RuntimeError("rollback has no frozen PostgreSQL settings")
        handoff = str(uuid.uuid4())
        orchestration = str(uuid.uuid4())
        if record.phase != "rollback_pending":
            record = _persist_transition(
                home,
                record,
                "rollback_pending",
                wal_config_before_digest=_settings_digest(_archive_settings(before)),
                restart_handoff=handoff,
                restart_orchestration=orchestration,
                rollback_postmaster_started_at=current["postmaster_started_at"],
                error=None,
            )
        if None in {
            record.pre_activation_env_b64,
            record.pre_activation_env_digest,
            record.pre_activation_auto_conf_b64,
            record.pre_activation_auto_conf_digest,
            record.rollback_expected_env_digest,
            record.rollback_expected_auto_conf_digest,
        }:
            raise RuntimeError("rollback has no exact config byte ownership evidence")
        _restore_exact_file(
            home / ".env",
            payload_b64=str(record.pre_activation_env_b64),
            target_digest=str(record.pre_activation_env_digest),
            expected_digest=str(record.rollback_expected_env_digest),
        )
        _restore_exact_file(
            home / "pg" / "postgresql.auto.conf",
            payload_b64=str(record.pre_activation_auto_conf_b64),
            target_digest=str(record.pre_activation_auto_conf_digest),
            expected_digest=str(record.rollback_expected_auto_conf_digest),
        )
        return _persist_transition(home, record, "rollback_restart_pending", error=None)
    finally:
        release_update_lock(holder)


def _dispatch_restart_handoff(home: Path, record: ActivationRecord) -> ActivationRecord:
    from ops.cluster_deploy import PitrRestartContinuation, spawn_restart

    action = "rollback" if record.phase == "rollback_restart_pending" else "activate"
    continuation = PitrRestartContinuation(
        record.operation_id,
        str(record.restart_orchestration),
        str(record.restart_handoff),
        action=action,
        expected_phase=record.phase,
        expected_digest=record.wal_config_desired_digest
        if action == "activate"
        else record.wal_config_before_digest,
    )
    consumed: list[ActivationRecord] = []
    session = _restart_session()

    def bind() -> None:
        with file_lock(lock_path(home), timeout_s=5):
            latest = load_record(home)
            if latest is None or (
                latest.operation_id != record.operation_id
                or latest.phase != record.phase
                or latest.restart_orchestration != record.restart_orchestration
                or latest.restart_handoff != record.restart_handoff
            ):
                raise RuntimeError("PITR restart handoff changed before dispatch")
            if latest.restart_handoff_consumed_at is not None:
                latest = rearm_restart_handoff(home, latest, session=session)
            consumed.append(consume_restart_handoff(home, latest, session=session))

    spawned = spawn_restart(
        continuation.origin(),
        mode="smooth",
        continuation=continuation,
        bind_continuation=bind,
    )
    if not consumed:
        raise RuntimeError("restart orchestration did not consume PITR handoff")
    if spawned["session"] != session:
        raise RuntimeError("restart orchestration returned a different bound session")
    return consumed[0]


def _restart_session() -> str:
    import shared.cluster
    from ops.cluster_session import _CLUSTER_RESTART_SERVICE

    return shared.cluster.session_name(_CLUSTER_RESTART_SERVICE)


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
            if origin.startswith("restart-continuation:"):
                parts = origin.split(":", 6)
                if len(parts) != 6 or record is None:
                    raise RuntimeError("invalid PITR restart continuation")  # noqa: TRY301
                _, operation_id, orchestration, handoff, phase, digest = parts
                if (
                    record.operation_id != operation_id
                    or record.restart_orchestration != orchestration
                    or record.restart_handoff != handoff
                    or record.restart_handoff_consumed_at is None
                    or record.restart_dispatch_session != _restart_session()
                    or record.phase != phase
                    or record.wal_config_desired_digest != digest
                ):
                    raise RuntimeError("stale PITR restart continuation")  # noqa: TRY301
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
                try:
                    release_update_lock(holder)
                except BaseException as exc:
                    _save_error(home, record, exc)
                    raise
    except (LockTimeoutError, RuntimeError, OSError, ValueError) as exc:
        print(f"PITR activation refused: {type(exc).__name__}", file=sys.stderr)
        return 1
    if record.phase == "wal_restart_pending" and not _restart_ready(
        record, _desired_archive_settings(home)
    ):
        try:
            record = _dispatch_restart_handoff(home, record)
        except (LockTimeoutError, RuntimeError, OSError, ValueError) as exc:
            print(f"PITR restart dispatch refused: {type(exc).__name__}", file=sys.stderr)
            return 1
    _print_record(record)
    return 0


def cmd_pitr_rollback(*, continuation: str | None = None) -> int:
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
            if continuation is not None:
                parts = continuation.split(":", 6)
                expected_digest = record.wal_config_before_digest or "none"
                if (
                    len(parts) != 6
                    or parts[0] != "restart-continuation"
                    or parts[1] != record.operation_id
                    or parts[2] != record.restart_orchestration
                    or parts[3] != record.restart_handoff
                    or record.restart_handoff_consumed_at is None
                    or record.restart_dispatch_session != _restart_session()
                    or parts[4] != record.phase
                    or parts[5] != expected_digest
                ):
                    raise RuntimeError("stale PITR rollback continuation")  # noqa: TRY301
            try:
                record = _rollback_record(home, record)
            except BaseException as exc:
                _save_error(home, record, exc)
                raise
    except (LockTimeoutError, RuntimeError, OSError, ValueError) as exc:
        print(f"PITR rollback refused: {type(exc).__name__}", file=sys.stderr)
        return 1
    if record.phase == "rollback_restart_pending":
        try:
            record = _dispatch_restart_handoff(home, record)
        except (LockTimeoutError, RuntimeError, OSError, ValueError) as exc:
            print(f"PITR rollback restart refused: {type(exc).__name__}", file=sys.stderr)
            return 1
    _print_record(record)
    print("PITR rollback preserves all logical and remote backup data")
    return 0
