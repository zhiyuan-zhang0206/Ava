"""Side-effecting WAL and candidate proof primitives for PITR activation."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shlex
import tempfile
import threading
import time
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import cast

import psycopg
from dotenv import dotenv_values

from services.pitr.activation_evidence import stored_digest_matches
from services.pitr.activation_state import ActivationRecord
from services.pitr.restore_manifest import candidate_sha256
from shared.config import settings
from shared.paths import ava_home


def activation_health_component() -> dict[str, object]:
    from shared.health_schema import DEGRADED, OK, component

    try:
        activation = ActivationRecord.from_json(
            (ava_home() / "physical-backup" / "activation" / "operation.json").read_text()
        )
        error = None
    except FileNotFoundError:
        activation, error = None, None
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        activation, error = None, f"activation state unreadable: {type(exc).__name__}"
    status = (
        OK
        if error is None
        and (activation is None or activation.phase in {"protected", "rolled_back"})
        else DEGRADED
    )
    health = component(
        "pitr_activation",
        status,
        progress="unknown" if error else "not_started" if activation is None else activation.phase,
        detail=error or (activation.error_detail if activation is not None else None),
        gate_readiness=False,
    )
    health["operation_id"] = None if activation is None else activation.operation_id
    health["protected"] = activation is not None and activation.phase == "protected"
    health["error_code"] = None if activation is None else activation.error_code
    return health


def _archive_settings(pg: dict[str, str]) -> dict[str, str]:
    return {
        name: pg[name]
        for name in ("archive_mode", "archive_command", "archive_timeout", "wal_compression")
    }


def _settings_digest(values: dict[str, str]) -> str:
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _file_evidence(path: Path) -> tuple[str, str]:
    payload = path.read_bytes()
    return base64.b64encode(payload).decode("ascii"), hashlib.sha256(payload).hexdigest()


def _restore_exact_file(
    path: Path, *, payload_b64: str, target_digest: str, expected_digest: str
) -> None:
    payload = base64.b64decode(payload_b64, validate=True)
    if path.name == ".env":
        from shared.envfile import replace_env_bytes_cas

        replace_env_bytes_cas(
            path,
            payload=payload,
            expected_digest=expected_digest,
            target_digest=target_digest,
        )
        return
    if hashlib.sha256(payload).hexdigest() != target_digest:
        raise RuntimeError(f"{path.name} rollback payload differs from durable digest")
    current = path.read_bytes()
    current_digest = hashlib.sha256(current).hexdigest()
    if current_digest == target_digest:
        return
    if current_digest != expected_digest:
        raise RuntimeError(f"{path.name} changed concurrently before rollback")
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.rollback-", dir=path.parent)
    staged = Path(raw)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        staged.replace(path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        staged.unlink(missing_ok=True)


def _shadow_pg_gate(current: dict[str, str]) -> bool:
    """archive_command displays '(disabled)' under PG17 archive_mode=off."""
    return current["archive_mode"] == "off" and current["archive_command"].strip() in (
        "",
        "(disabled)",
    )


def _desired_archive_settings(home: Path) -> dict[str, str]:
    config = settings.physical_backup
    shim = home / "runtime" / "pg-archive" / "archive-shim"
    spool = home / "physical-backup" / "spool"
    command = " ".join(
        shlex.quote(token)
        for token in (
            str(shim),
            "%p",
            "%f",
            "--spool",
            str(spool),
            "--hard-bytes",
            str(config.pitr_spool_hard_bytes),
        )
    )
    timeout = config.pitr_archive_timeout_seconds
    return {
        "archive_mode": "on",
        "archive_command": command,
        "archive_timeout": f"{timeout // 60}min" if timeout % 60 == 0 else f"{timeout}s",
        "wal_compression": "pglz",
    }


def _enable_pitr_services(expected_digest: str) -> bytes:
    from shared.runtime_config import write_fields

    captured = write_fields(
        {
            "pitr_enabled": True,
            "pitr_base_backup_enabled": True,
            "pitr_restore_proof_enabled": True,
            "pitr_retention_planner_enabled": False,
        },
        set(),
        capture_bytes=True,
        expected_digest=expected_digest,
    )
    if captured is None:
        raise RuntimeError("PITR environment write did not return owned bytes")
    return captured


_PITR_ENV_FIELDS = {
    "pitr_enabled": "AVA_PITR_ENABLED",
    "pitr_base_backup_enabled": "AVA_PITR_BASE_BACKUP_ENABLED",
    "pitr_restore_proof_enabled": "AVA_PITR_RESTORE_PROOF_ENABLED",
    "pitr_retention_planner_enabled": "AVA_PITR_RETENTION_PLANNER_ENABLED",
}


def _pitr_env_baseline(payload: bytes | None = None) -> dict[str, str]:
    path = ava_home() / ".env"
    if payload is None:
        from shared.envfile import capture_env_bytes

        payload = capture_env_bytes(path)
    lines = payload.decode().splitlines()
    return {
        field: json.dumps(
            [line for line in lines if line.split("=", 1)[0].strip() == alias],
            separators=(",", ":"),
        )
        for field, alias in _PITR_ENV_FIELDS.items()
    }


def capture_pitr_env_baseline(path: Path) -> tuple[str, str, dict[str, str]]:
    from shared.envfile import capture_env_bytes

    payload = capture_env_bytes(path)
    return (
        base64.b64encode(payload).decode("ascii"),
        hashlib.sha256(payload).hexdigest(),
        _pitr_env_baseline(payload),
    )


def pitr_env_is_desired(payload: bytes) -> bool:
    values = dotenv_values(stream=StringIO(payload.decode()))
    return all(
        values.get(alias) == desired
        for alias, desired in {
            "AVA_PITR_ENABLED": "true",
            "AVA_PITR_BASE_BACKUP_ENABLED": "true",
            "AVA_PITR_RESTORE_PROOF_ENABLED": "true",
            "AVA_PITR_RETENTION_PLANNER_ENABLED": "false",
        }.items()
    )


def rollback_effect_state(*, current: str, before: str, owned: str) -> bool:
    """Return whether rollback owns a mutation; reject every third-party byte state."""

    if current == before:
        return False
    if current == owned:
        return True
    raise RuntimeError("config is neither pre-effect nor exact owned post-effect")


def _restore_pitr_env(baseline: dict[str, str]) -> None:
    from shared.envfile import env_lock_path, snapshot_env
    from shared.platform import file_lock
    from shared.private_storage import write_private_bytes

    path = ava_home() / ".env"
    aliases = set(_PITR_ENV_FIELDS.values())
    with file_lock(env_lock_path(path), timeout_s=30):
        current = path.read_bytes() if path.exists() else b""
        if not pitr_env_is_desired(current):
            raise RuntimeError("PITR-owned environment keys changed before rollback")
        snapshot_env(path)
        lines = current.decode().splitlines()
        kept = [line for line in lines if line.split("=", 1)[0].strip() not in aliases]
        for encoded in baseline.values():
            restored = json.loads(encoded)
            if not isinstance(restored, list):
                raise TypeError("PITR environment baseline is invalid")
            restored_items = cast(list[object], restored)
            if not all(isinstance(item, str) for item in restored_items):
                raise RuntimeError("PITR environment baseline is invalid")
            kept.extend(cast(list[str], restored_items))
        write_private_bytes(path, ("\n".join(kept) + "\n").encode())


def archiver_reached_target(*, last_archived: str, timeline: str, target: str) -> bool:
    return (
        len(last_archived) == 24
        and len(target) == 24
        and last_archived[:8] == target[:8] == f"{int(timeline):08X}"
        and int(last_archived[8:], 16) >= int(target[8:], 16)
    )


def pitr_admin_url() -> str:
    """The admin-plane connection the activation's Postgres mutations run on:
    the initdb superuser over the live unix socket (same face `_read_pg_state`
    reads through).

    Deliberately NOT `shared.db.direct_db_url()` — that derives from
    `AVA_DB_URL`, whose identity is the runtime role, which lacks
    `pg_switch_wal` (2026-08-30 activation failure: the WAL-switch step
    crashed with InsufficientPrivilege while every read-only preflight check
    had passed on the superuser connection). One URL for both the probe and
    the mutation means the probe can never certify a different connection
    than the switch runs on.
    """
    from cli.commands._cluster_instance import pg_admin_url
    from shared.cluster import get_record, record_postgres_port

    record = get_record(ava_home())
    if record is None:
        raise RuntimeError("cluster registry record is missing")
    return pg_admin_url(record_postgres_port(record))


def prepare_wal_switch() -> dict[str, str]:
    """Capture the exact WAL segment the proof will demand, on the admin
    connection the switch runs on (2026-08-30: the old runtime-identity dial
    made this capture and the switch diverge from the certified connection)."""
    with psycopg.connect(pitr_admin_url(), autocommit=True) as conn:
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


def switch_wal() -> str:
    """Force-rotate the current WAL segment on the admin connection — the
    runtime identity lacks pg_switch_wal (2026-08-30 InsufficientPrivilege)."""
    with psycopg.connect(pitr_admin_url(), autocommit=True) as conn:
        row = conn.execute("SELECT pg_switch_wal()::text").fetchone()
    if row is None:
        raise RuntimeError("PostgreSQL omitted pg_switch_wal result")
    return str(row[0])


def probe_switch_privilege() -> None:
    """Read-only privilege probe: the connection the activation's WAL switch
    will run on must be able to EXECUTE pg_switch_wal.

    The probe asks PostgreSQL rather than assuming the superuser role: a
    future provisioning change that strips the grant fails the shadow gate
    closed BEFORE any config mutation, instead of failing the activation
    mid-flight (the 2026-08-30 failure mode)."""
    with psycopg.connect(pitr_admin_url(), autocommit=True) as conn:
        row = conn.execute("SELECT has_function_privilege('pg_switch_wal()', 'EXECUTE')").fetchone()
    if row is None or not row[0]:
        raise RuntimeError(
            "PITR admin connection cannot execute pg_switch_wal — "
            "activation refused before any config mutation"
        )


def remote_wal_proof(
    record: ActivationRecord, stop: threading.Event | None = None
) -> tuple[dict[str, str], dict[str, str]]:
    from services.pitr.store_factory import get_store_group
    from services.pitr.uploader import ack_manifest_from_raw
    from shared.db import direct_db_url

    exact, deadline_text = record.wal_exact_evidence, record.wal_verification_deadline
    config = settings.physical_backup
    if exact is None or deadline_text is None:
        raise RuntimeError("WAL verification state is incomplete")
    if config.pitr_store_backend == "gcs" and config.pitr_restore_gcs_credentials_file is None:
        raise RuntimeError("WAL verification state is incomplete")
    viewer_store = get_store_group().viewer_object_store()
    archive_name = exact["segment"]
    deadline = datetime.fromisoformat(deadline_text)
    switch_intent = datetime.fromisoformat(exact["switch_intent_at"])
    ack_path = ava_home() / "physical-backup" / "ack" / f"{archive_name}.ack.json"
    while datetime.now(UTC) <= deadline:
        if stop is not None and stop.is_set():
            raise RuntimeError("PITR WAL proof lost its deployment lease")
        with psycopg.connect(direct_db_url(), autocommit=True) as conn:
            row = conn.execute(
                "SELECT last_archived_wal, failed_count::text, archived_count::text "
                "FROM pg_stat_archiver"
            ).fetchone()
        last_archived = "" if row is None or row[0] is None else str(row[0])
        reached = archiver_reached_target(
            last_archived=last_archived, timeline=exact["timeline"], target=archive_name
        )
        if (
            row is not None
            and reached
            and str(row[1]) == exact["failed_count"]
            and int(str(row[2])) > int(exact["archived_count"])
            and ack_path.is_file()
        ):
            ack = ack_manifest_from_raw(json.loads(ack_path.read_text()))
            if ack.archive_name != archive_name:
                raise RuntimeError("durable WAL ACK targets a different archive")
            acknowledged = datetime.fromisoformat(ack.acknowledged_at)
            if not switch_intent <= acknowledged <= deadline:
                raise RuntimeError("durable WAL ACK falls outside the activation window")
            if stop is not None and stop.is_set():
                raise RuntimeError("PITR WAL proof lost its deployment lease")
            remote = viewer_store.stat(ack.object_name)
            if remote is None or (
                remote.pin_token,
                remote.size,
                remote.checksum.algo,
                remote.checksum.value,
            ) != (
                ack.pin_token,
                ack.ciphertext_size,
                ack.ciphertext_checksum_algo,
                ack.ciphertext_checksum_value,
            ):
                raise RuntimeError("viewer observed WAL differs from durable ACK")
            metadata = {
                "ava-archive-name": ack.archive_name,
                "ava-source-sha256": ack.source_sha256,
                "ava-source-size": str(ack.source_size),
                "ava-ciphertext-crc32c": ack.ciphertext_checksum_value,
                "ava-encryption-format": ack.encryption_format,
                "ava-key-id": ack.key_id,
            }
            if dict(remote.metadata) != metadata:
                raise RuntimeError("viewer observed WAL metadata differs from durable ACK")
            observed_at = datetime.now(UTC)
            if observed_at > deadline:
                raise RuntimeError("viewer WAL proof completed after the activation deadline")
            common = {
                "timeline": exact["timeline"],
                "segment": archive_name,
                "bucket_name": str(config.pitr_gcs_bucket),
                "object_prefix": config.pitr_gcs_prefix,
                "object_name": ack.object_name,
                "generation": ack.pin_token,
                "ciphertext_size": str(ack.ciphertext_size),
                "ciphertext_crc32c": ack.ciphertext_checksum_value,
                "source_sha256": ack.source_sha256,
                "source_size": str(ack.source_size),
                "key_id": ack.key_id,
                "encryption_format": ack.encryption_format,
            }
            return (
                {**common, "acknowledged_at": ack.acknowledged_at},
                {
                    **common,
                    "viewer_id": str(
                        (record.pre_activation_credential_evidence or {})["viewer_client_email"]
                    ),
                    "observed_at": observed_at.isoformat(),
                },
            )
        time.sleep(2)
    raise RuntimeError("WAL remote proof deadline expired")


def forced_candidate(record: ActivationRecord, stop: threading.Event) -> tuple[str, str]:
    from services.pitr.activation_base import build_activation_candidate

    if record.candidate_chain_id is None:
        raise RuntimeError("activation candidate intent is missing")
    candidate = build_activation_candidate(
        operation_id=record.operation_id, chain_id=record.candidate_chain_id, stop=stop
    )
    payload = candidate.to_json()
    return payload, hashlib.sha256(payload.encode()).hexdigest()


def restore_candidate(record: ActivationRecord, stop: threading.Event) -> tuple[str, str]:
    from services.pitr.activation_base import restore_activation_candidate
    from services.pitr.base_manifest import CandidateManifest

    if record.protected_manifest is None or record.candidate_digest is None:
        raise RuntimeError("activation candidate evidence is incomplete")
    candidate = CandidateManifest.from_json(record.protected_manifest)
    canonical = candidate.to_json()
    if (
        candidate.chain_id != record.candidate_chain_id
        or not candidate.chain_id.endswith(f"-{record.operation_id}")
        or not stored_digest_matches(
            raw=record.protected_manifest, canonical=canonical, expected=record.candidate_digest
        )
    ):
        raise RuntimeError("restore candidate differs from durable activation intent")
    protected = asyncio.run(restore_activation_candidate(candidate, stop))
    if (
        protected.chain_id != candidate.chain_id
        or protected.candidate != candidate
        or protected.candidate_sha256 != candidate_sha256(candidate)
    ):
        raise RuntimeError("protected proof differs from exact activation candidate")
    payload = protected.to_json()
    return payload, hashlib.sha256(payload.encode()).hexdigest()
