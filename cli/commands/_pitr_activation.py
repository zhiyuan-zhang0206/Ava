"""Explicit, resumable CLI boundary for activating physical PITR."""

from __future__ import annotations

# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
import hashlib
import shutil
import stat
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import LiteralString

import psycopg
from psycopg.conninfo import make_conninfo

from cli.commands._pitr_activation_config import (
    apply_wal_config,
    require_inactive_gate_posture,
    restore_archive_settings,
)
from services.pitr.activation_credentials import (
    credential_app_key,
    credential_identity,
    oss_credential_identity,
    probe_baidu_read_access,
    probe_bucket_read_access,
    probe_oss_read_access,
    require_store_config,
)
from services.pitr.activation_lease import run_while_renewing
from services.pitr.activation_runtime import (
    _PITR_ENV_FIELDS as _PITR_ENV_FIELDS,
)
from services.pitr.activation_runtime import (
    _archive_settings,
    _desired_archive_settings,
    _file_evidence,
    _settings_digest,
    _shadow_pg_gate,
    capture_pitr_env_baseline,
    probe_switch_privilege,
    rollback_effect_state,
)
from services.pitr.activation_runtime import (
    forced_candidate as _forced_candidate,
)
from services.pitr.activation_runtime import (
    prepare_wal_switch as _prepare_wal_switch,
)
from services.pitr.activation_runtime import (
    remote_wal_proof as _remote_wal_proof,
)
from services.pitr.activation_runtime import (
    restore_candidate as _restore_candidate,
)
from services.pitr.activation_runtime import (
    switch_wal as _switch_wal,
)
from services.pitr.activation_state import (
    ActivationPhase,
    ActivationRecord,
    load_record,
    mark_pre_mutation_rolled_back,
    write_record,
    write_record_cas,
)
from services.pitr.cos_client import credential_evidence as _cos_credential_evidence
from shared.config import settings
from shared.paths import ava_home
from shared.pg_tools import pg_tool

_EMERGENCY_FLOOR_BYTES = 4 * 1024**3


@dataclass(frozen=True)
class ShadowReadiness:
    pg: dict[str, str]
    credentials: dict[str, str]


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_secrets() -> dict[str, str]:
    config = settings.physical_backup
    key = config.pitr_backup_key_file
    if key is None:
        raise RuntimeError("backup key is required")
    if not key.is_file() or key.is_symlink() or _mode(key) != 0o600:
        raise RuntimeError(f"PITR secret is unsafe: {key}")
    if key.stat().st_size != 32:
        raise RuntimeError("PITR backup key must be exactly 32 bytes")
    common = {
        "object_prefix": config.pitr_gcs_prefix,
        "backup_key_id": config.pitr_backup_key_id,
        "backup_key_sha256": _sha256(key),
    }
    if config.pitr_store_backend == "gcs":
        uploader = config.pitr_gcs_credentials_file
        viewer = config.pitr_restore_gcs_credentials_file
        if uploader is None or viewer is None:
            raise RuntimeError("uploader and viewer credentials are required")
        for secret in (uploader, viewer):
            if not secret.is_file() or secret.is_symlink() or _mode(secret) != 0o600:
                raise RuntimeError(f"PITR secret is unsafe: {secret}")
        uploader_id = credential_identity(uploader)
        viewer_id = credential_identity(viewer)
        if uploader_id[0] == viewer_id[0]:
            raise RuntimeError("uploader and viewer service-account identities must differ")
        if uploader_id[1] != config.pitr_gcs_project or viewer_id[1] != config.pitr_gcs_project:
            raise RuntimeError("PITR service-account project differs from configured GCS project")
        # Data-plane uploader is objectCreator + objectViewer; no synthetic write/delete probe.
        bucket_name = probe_bucket_read_access(viewer)
        return {
            "backend": "gcs",
            "uploader_identity": uploader_id[0],
            "viewer_identity": viewer_id[0],
            "store_target": bucket_name,
            **common,
        }
    if config.pitr_store_backend == "cos":
        credentials_file = config.pitr_cos_credentials_file
        if credentials_file is None:
            raise RuntimeError("COS credentials are required")
        return {
            **_cos_credential_evidence(
                credentials_file, region=config.pitr_cos_region, bucket=config.pitr_cos_bucket
            ),
            **common,
        }
    if config.pitr_store_backend == "oss":
        uploader = config.pitr_oss_credentials_file
        viewer = config.pitr_oss_viewer_credentials_file
        if uploader is None or viewer is None:
            raise RuntimeError("uploader and viewer credentials are required")
        for secret in (uploader, viewer):
            if not secret.is_file() or secret.is_symlink() or _mode(secret) != 0o600:
                raise RuntimeError(f"PITR secret is unsafe: {secret}")
        uploader_id = oss_credential_identity(uploader)
        viewer_id = oss_credential_identity(viewer)
        if uploader_id == viewer_id:
            raise RuntimeError("uploader and viewer OSS identities must differ")
        # Data-plane uploader is bucket-scoped read-write; no synthetic
        # write/delete probe. One list page proves the viewer read scope.
        bucket_name = probe_oss_read_access(viewer)
        return {
            "backend": "oss",
            "uploader_identity": uploader_id,
            "viewer_identity": viewer_id,
            "store_target": bucket_name,
            **common,
        }
    if config.pitr_store_backend != "baidu":
        raise RuntimeError(f"unhandled PITR store backend {config.pitr_store_backend!r}")
    credentials_file = config.pitr_baidu_credentials_file
    if credentials_file is None:
        raise RuntimeError("Baidu credentials are required")
    if (
        not credentials_file.is_file()
        or credentials_file.is_symlink()
        or _mode(credentials_file) != 0o600
    ):
        raise RuntimeError(f"PITR secret is unsafe: {credentials_file}")
    # One OAuth app serves both roles; the probe proves the token and read scope.
    app_root = probe_baidu_read_access(credentials_file)
    return {
        "backend": "baidu",
        "uploader_identity": credential_app_key(credentials_file),
        "viewer_identity": credential_app_key(credentials_file),
        "store_target": app_root,
        **common,
    }


def _read_pg_state() -> dict[str, str]:
    from cli.commands._cluster_instance import pg_admin_url
    from shared.cluster import db_identity, get_record, ownership, record_postgres_port

    if (record := get_record(ava_home())) is None:
        raise RuntimeError("cluster registry record is missing")
    expected_db = db_identity()
    owner = ownership.require_postgres(ava_home() / "pg", record_postgres_port(record))
    if owner is None:
        raise RuntimeError("PITR requires this home's native PostgreSQL owner")

    def scalar(conn: psycopg.Connection[tuple[object, ...]], query: LiteralString) -> object:
        row = conn.execute(query).fetchone()
        if row is None:
            raise RuntimeError(f"PostgreSQL returned no row for {query!r}")
        return row[0]

    with psycopg.connect(pg_admin_url(record_postgres_port(record)), autocommit=True) as conn:
        ownership.require_postgres_connection(conn, ava_home() / "pg")
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
    direct_url = make_conninfo(pg_admin_url(record_postgres_port(record)), dbname=expected_db)
    with psycopg.connect(direct_url, autocommit=True) as conn:
        ownership.require_postgres_connection(conn, ava_home() / "pg")
        current["dbname"] = str(scalar(conn, "SELECT current_database()"))
        direct_system_id = str(scalar(conn, "SELECT system_identifier FROM pg_control_system()"))
    if current["dbname"] != expected_db or direct_system_id != system_id:
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
    current_owner = ownership.postgres(expected_data)
    if current_owner is None or not owner.same_birth(current_owner) or not owner.live():
        raise RuntimeError("PostgreSQL native birth changed during PITR observation")
    current["postmaster_pid"] = str(owner.pid)
    current["postmaster_create_time"] = str(owner.birth)
    current["postmaster_starttime"] = "" if owner.starttime is None else str(owner.starttime)
    _require_control_identity(expected_data, system_id)
    return current


def _require_control_identity(expected_data: Path, system_id: str) -> None:
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
    require_store_config(config)
    require_inactive_gate_posture("shadow readiness")
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
    probe_switch_privilege()
    current = _read_pg_state()
    if not _shadow_pg_gate(current):
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
    if record.error_code:
        print(f"  error: {record.error_code}: {record.error_detail}")
    return 0


def _validate_snapshot(record: ActivationRecord) -> None:
    if not record.pre_activation_snapshot:
        return
    path = Path(record.pre_activation_snapshot)
    if path.is_symlink() or not path.is_file() or _mode(path) != 0o600:
        raise RuntimeError("pre-activation snapshot is missing or unsafe")
    from services.gateway_side.backup import snapshot as backup_snapshot

    backup_snapshot.verify_snapshot(path)


def cmd_pitr_status() -> int:
    record = load_record(ava_home())
    if record is not None and record.phase not in {"protected", "rolled_back"}:
        _validate_snapshot(record)
    return _print_record(record)


def _require_same_pg_state(expected: dict[str, str] | None, boundary: str) -> None:
    current = _read_pg_state()
    if expected is not None and expected.get("postmaster_starttime"):
        from shared.native_process.ownership import OwnedProcess

        def native(state: dict[str, str]) -> OwnedProcess:
            return OwnedProcess(
                int(state["postmaster_pid"]),
                float(state["postmaster_create_time"]),
                int(state["postmaster_starttime"]) if state["postmaster_starttime"] else None,
            )

        if native(expected).same_birth(native(current)):
            # Linux's stable tick is identity; its derived wall time can move.
            current = current | {"postmaster_create_time": expected["postmaster_create_time"]}
    if expected is None or current != expected:
        raise RuntimeError(f"PostgreSQL identity/settings changed {boundary}")


def _require_same_credentials(expected: dict[str, str] | None, boundary: str) -> None:
    if expected is None or _validate_secrets() != expected:
        raise RuntimeError(f"PITR credential/bucket evidence changed {boundary}")


def _require_same_pre_mutation_state(record: ActivationRecord) -> None:
    expected = record.pre_activation_pg_settings
    current = _read_pg_state()
    immutable = (
        "system_identifier",
        "data_directory",
        "port",
        "dbname",
        "archive_mode",
        "archive_command",
        "archive_timeout",
        "wal_compression",
    )
    if expected is None or any(expected.get(name) != current.get(name) for name in immutable):
        raise RuntimeError("PostgreSQL identity/settings changed before PITR mutation")
    _require_same_credentials(record.pre_activation_credential_evidence, "before PITR mutation")


def _prepare_snapshot(home: Path, record: ActivationRecord) -> ActivationRecord:
    from services.backup import activation_snapshot
    from services.gateway_side.backup import snapshot as backup_snapshot

    pg_settings = record.pre_activation_pg_settings
    if pg_settings is None:
        raise RuntimeError("PITR activation has no frozen PostgreSQL identity")
    _require_same_pg_state(pg_settings, "before snapshot")
    _require_same_credentials(record.pre_activation_credential_evidence, "before snapshot")
    existing = activation_snapshot(record.operation_id)
    if existing is not None:
        snapshot = existing
        backup_snapshot.verify_snapshot(snapshot)
    else:
        snapshot = backup_snapshot.create_pre_activation_snapshot(
            operation_id=record.operation_id,
            db_url=pg_settings["direct_db_url"],
            progress=lambda line: print(f"→ pre-activation data snapshot: {line}", flush=True),
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


def _pg_auto_conf_baseline(home: Path) -> dict[str, str]:
    from cli.commands._pitr_activation_config import _persistent_archive_settings

    return _persistent_archive_settings(home)


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


def _advance_activation(
    home: Path, record: ActivationRecord, holder: str, *, stop_at_restart: bool = False
) -> ActivationRecord:
    if record.phase != "shadow":
        _require_same_credentials(
            record.pre_activation_credential_evidence, f"before {record.phase}"
        )
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
        require_inactive_gate_posture("the pre-activation baseline")
        _validate_snapshot(record)
        _require_same_pre_mutation_state(record)
        before = _archive_settings(_read_pg_state())
        env_b64, env_digest, env_baseline = capture_pitr_env_baseline(home / ".env")
        auto_b64, auto_digest = _file_evidence(home / "pg" / "postgresql.auto.conf")
        record = _persist_transition(
            home,
            record,
            "wal_config_applying",
            wal_config_before_digest=_settings_digest(before),
            wal_config_desired_digest=_settings_digest(desired),
            pre_activation_pitr_env=env_baseline,
            pre_activation_pg_auto_conf=_pg_auto_conf_baseline(home),
            pre_activation_env_b64=env_b64,
            pre_activation_env_digest=env_digest,
            pre_activation_auto_conf_b64=auto_b64,
            pre_activation_auto_conf_digest=auto_digest,
            rollback_expected_env_digest=env_digest,
            rollback_expected_auto_conf_digest=auto_digest,
            error=None,
        )
    if record.phase == "wal_config_applying":
        record = apply_wal_config(home, record, desired)
    if stop_at_restart and record.phase == "wal_restart_pending":
        return record
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
    return _prove_activation(home, record, holder)


def _prove_activation(home: Path, record: ActivationRecord, holder: str) -> ActivationRecord:
    if record.phase == "wal_ack_pending":
        # The verification deadline is a per-attempt window: an earlier attempt
        # that crashed before the proof loop (2026-08-30: the switch step died
        # on a privilege gap) must not strand the operation at an expired
        # non-renewable deadline. The switch intent stays immutable — the ACK
        # lower bound — only the upper bound is re-stamped.
        renewed = record.renew_wal_deadline((datetime.now(UTC) + timedelta(minutes=5)).isoformat())
        write_record_cas(home, expected=record, replacement=renewed)
        record = renewed
        # Reissuing a switch after a crash is safe: immutable naming and the
        # persisted target segment make proof exact; the extra segment is retained.
        _switch_wal()
        ack, viewer = run_while_renewing(holder, lambda stop: _remote_wal_proof(record, stop))
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
        candidate_json, digest = run_while_renewing(
            holder, lambda stop: _forced_candidate(record, stop)
        )
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
        protected, digest = run_while_renewing(
            holder, lambda stop: _restore_candidate(record, stop)
        )
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
    from shared.release_operation import require_pitr_authorized

    require_pitr_authorized(home)
    if record.phase == "rollback_restart_pending":
        current = _read_pg_state()
        expected = record.pre_activation_pg_settings or {}
        if current[
            "postmaster_started_at"
        ] == record.rollback_postmaster_started_at or _archive_settings(
            current
        ) != _archive_settings(expected):
            return record
        if _pg_auto_conf_baseline(home) != record.pre_activation_pg_auto_conf:
            raise RuntimeError("PostgreSQL ALTER SYSTEM ownership differs after rollback")
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
    if record.phase != "rollback_pending":
        record = _persist_transition(
            home,
            record,
            "rollback_pending",
            wal_config_before_digest=_settings_digest(_archive_settings(before)),
            rollback_postmaster_started_at=current["postmaster_started_at"],
            error=None,
        )
    # The PITR gate keys are config-owned (settable through the settings
    # path since the 2026-08-31 rollback rework), so rollback never
    # reverts them: undoing the activation leaves the operator's
    # enablement exactly as configured. The captured env evidence stays
    # in the record for audit, but only the PostgreSQL auto-conf bytes
    # are restored here.
    if None in {
        record.pre_activation_auto_conf_b64,
        record.pre_activation_auto_conf_digest,
        record.rollback_expected_auto_conf_digest,
    }:
        raise RuntimeError("rollback has no exact config byte ownership evidence")
    owned = _require_owned_rollback_config(home, record)
    if owned != record.rollback_expected_auto_conf_digest:
        replacement = record.journal_rollback(rollback_expected_auto_conf_digest=owned)
        write_record_cas(home, expected=record, replacement=replacement)
        record = replacement
    baseline = record.pre_activation_pg_auto_conf
    if baseline is None:
        raise RuntimeError("rollback lacks PostgreSQL owned-field baseline")
    record = restore_archive_settings(home, record, baseline)
    restored_digest = _file_evidence(home / "pg" / "postgresql.auto.conf")[1]
    return _persist_transition(
        home,
        record,
        "rollback_restart_pending",
        rollback_expected_auto_conf_digest=restored_digest,
        error=None,
    )


def _require_owned_rollback_config(home: Path, record: ActivationRecord) -> str:
    from cli.commands._pitr_activation_config import complete_auto_intent

    current_auto_digest = _file_evidence(home / "pg" / "postgresql.auto.conf")[1]
    try:
        rollback_effect_state(
            current=current_auto_digest,
            before=str(record.pre_activation_auto_conf_digest),
            owned=str(record.rollback_expected_auto_conf_digest),
        )
    except RuntimeError:
        intent = record.rollback_setting_intent or record.config_apply_intent
        if (
            intent is None
            or intent.get("kind", "postgresql_auto_conf") != "postgresql_auto_conf"
            or intent.get("expected_digest") != record.rollback_expected_auto_conf_digest
        ):
            raise
        complete_auto_intent(home, intent)
    return current_auto_digest


def cmd_pitr_activate(*, origin: str) -> int:
    """Submit or join the home's finite, retained-image PITR operation."""
    from cli.release_transition.pitr_submit import run_pitr

    return run_pitr("activate", origin=origin)


def cmd_pitr_rollback() -> int:
    """Serialize explicit rollback under the same home operation authority."""
    from cli.release_transition.pitr_submit import run_pitr

    return run_pitr("rollback", origin="operator")
