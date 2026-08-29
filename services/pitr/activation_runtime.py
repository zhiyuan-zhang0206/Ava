"""Side-effecting WAL and candidate proof primitives for PITR activation."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shlex
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import psycopg

from services.pitr.activation_state import ActivationRecord
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
        detail=error or (activation.error if activation is not None else None),
        gate_readiness=False,
    )
    health["operation_id"] = None if activation is None else activation.operation_id
    health["protected"] = activation is not None and activation.phase == "protected"
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


def _enable_pitr_services() -> bytes:
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


def _restore_pitr_env(baseline: dict[str, str]) -> None:
    from shared.envfile import env_lock_path, snapshot_env
    from shared.platform import file_lock
    from shared.private_storage import write_private_bytes

    path = ava_home() / ".env"
    aliases = set(_PITR_ENV_FIELDS.values())
    with file_lock(env_lock_path(path), timeout_s=30):
        snapshot_env(path)
        lines = path.read_text().splitlines() if path.exists() else []
        kept = [line for line in lines if line.split("=", 1)[0].strip() not in aliases]
        for encoded in baseline.values():
            restored = json.loads(encoded)
            if not isinstance(restored, list) or not all(
                isinstance(line, str) for line in restored
            ):
                raise RuntimeError("PITR environment baseline is invalid")
            kept.extend(cast(list[str], restored))
        write_private_bytes(path, ("\n".join(kept) + "\n").encode())


def archiver_reached_target(*, last_archived: str, timeline: str, target: str) -> bool:
    return (
        len(last_archived) == 24
        and len(target) == 24
        and last_archived[:8] == target[:8] == f"{int(timeline):08X}"
        and int(last_archived[8:], 16) >= int(target[8:], 16)
    )


def remote_wal_proof(record: ActivationRecord) -> tuple[dict[str, str], dict[str, str]]:
    from services.pitr.gcs_store import GCSObjectStore
    from services.pitr.uploader import AckManifest
    from shared.db import direct_db_url

    exact, deadline_text = record.wal_exact_evidence, record.wal_verification_deadline
    config = settings.physical_backup
    viewer_credentials = config.pitr_restore_gcs_credentials_file
    if exact is None or deadline_text is None or viewer_credentials is None:
        raise RuntimeError("WAL verification state is incomplete")
    archive_name = exact["segment"]
    deadline = datetime.fromisoformat(deadline_text)
    switch_intent = datetime.fromisoformat(exact["switch_intent_at"])
    ack_path = ava_home() / "physical-backup" / "ack" / f"{archive_name}.ack.json"
    while datetime.now(UTC) <= deadline:
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
            ack = AckManifest(**json.loads(ack_path.read_text()))
            if ack.archive_name != archive_name:
                raise RuntimeError("durable WAL ACK targets a different archive")
            acknowledged = datetime.fromisoformat(ack.acknowledged_at)
            if not switch_intent <= acknowledged <= deadline:
                raise RuntimeError("durable WAL ACK falls outside the activation window")
            remote = GCSObjectStore(
                project=config.pitr_gcs_project,
                bucket=config.pitr_gcs_bucket,
                credentials_file=viewer_credentials,
            ).stat(ack.object_name)
            if remote is None or (remote.generation, remote.size, remote.crc32c) != (
                ack.generation,
                ack.ciphertext_size,
                ack.ciphertext_crc32c,
            ):
                raise RuntimeError("viewer observed WAL differs from durable ACK")
            metadata = {
                "ava-archive-name": ack.archive_name,
                "ava-source-sha256": ack.source_sha256,
                "ava-source-size": str(ack.source_size),
                "ava-ciphertext-crc32c": ack.ciphertext_crc32c,
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
                "object_name": ack.object_name,
                "generation": str(ack.generation),
                "ciphertext_size": str(ack.ciphertext_size),
                "ciphertext_crc32c": ack.ciphertext_crc32c,
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


def forced_candidate(record: ActivationRecord) -> tuple[str, str]:
    from services.pitr.activation_base import build_activation_candidate

    if record.candidate_chain_id is None:
        raise RuntimeError("activation candidate intent is missing")
    candidate = build_activation_candidate(
        operation_id=record.operation_id, chain_id=record.candidate_chain_id
    )
    payload = candidate.to_json()
    return payload, hashlib.sha256(payload.encode()).hexdigest()


def restore_candidate(record: ActivationRecord) -> tuple[str, str]:
    from services.pitr.activation_base import restore_activation_candidate
    from services.pitr.base_manifest import CandidateManifest

    if record.protected_manifest is None or record.candidate_digest is None:
        raise RuntimeError("activation candidate evidence is incomplete")
    candidate = CandidateManifest.from_json(record.protected_manifest)
    canonical = candidate.to_json()
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    if (
        candidate.chain_id != record.candidate_chain_id
        or not candidate.chain_id.endswith(f"-{record.operation_id}")
        or digest != record.candidate_digest
    ):
        raise RuntimeError("restore candidate differs from durable activation intent")
    protected = asyncio.run(restore_activation_candidate(candidate))
    if (
        protected.chain_id != candidate.chain_id
        or protected.candidate != candidate
        or (protected.candidate_sha256 != digest)
    ):
        raise RuntimeError("protected proof differs from exact activation candidate")
    payload = protected.to_json()
    return payload, hashlib.sha256(payload.encode()).hexdigest()
