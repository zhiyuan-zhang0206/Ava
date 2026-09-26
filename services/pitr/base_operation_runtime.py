"""Shared controller implementation for scheduled and activation restore proofs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import cast

import psycopg

from services.pitr.base_manifest import CandidateManifest
from services.pitr.operation_custody import OperationKind
from services.pitr.restore_drill import validate_drill_inputs
from services.pitr.restore_manifest import ProtectedManifest
from services.pitr.restore_proof import (
    ProtectedManifestPublisher,
    RestoreSpaceBudget,
    publish_candidate_proof,
    quarantine_restore_staging,
    retire_restore_work,
    verify_candidate_proof,
)
from services.pitr.store_factory import get_store_group
from services.pitr.worker_process import run_operation
from shared.config import settings
from shared.config.physical_backup import PhysicalBackupSettings
from shared.db import direct_db_url
from shared.paths import ava_home
from shared.pg_tools import pg_tool
from shared.process_env import forwarded_proxy_env, restricted_process_env

_EMERGENCY_FLOOR_BYTES = 4 * 1024**3
# A stopped drill stops its sandbox postmaster (bounded at 20 s), scans for
# residue and writes its evidence before the controller's confirmed close.
DRILL_GRACE_S = 45.0


def restore_kind(root: Path) -> OperationKind:
    """Scheduled and activation restore proofs share one physical-backup root."""
    return OperationKind(
        "restore-proof",
        root / "restore-control",
        root / "quarantine" / "restore-proof",
        partial(quarantine_restore_staging, root),
    )


def drill_kind(root: Path) -> OperationKind:
    """Operator drills keep their evidence in the operator's scratch tree."""
    return OperationKind(
        "pitr-drill",
        root / "drill-control",
        root / "quarantine" / "pitr-drill",
        grace_s=DRILL_GRACE_S,
    )


@dataclass(frozen=True)
class RestoreWorkerInput:
    candidate_json: str
    root: Path
    ack_dir: Path
    key_path: Path
    backend: str
    store_args: tuple[tuple[str, str], ...]
    budget: RestoreSpaceBudget
    live_db_url: str
    data_directory: str
    pg_ctl: Path
    pg_verifybackup: Path


def restore_key_path(config: PhysicalBackupSettings) -> Path:
    """The validated viewer-only restore key path (shared by proof and drill)."""
    key_path = config.pitr_backup_key_file
    if key_path is None:
        raise RuntimeError("validated viewer-only restore proof key is missing")
    return key_path


def restore_store_args(config: PhysicalBackupSettings) -> tuple[tuple[str, str], ...]:
    """The per-backend store-args protocol shared by the restore proof and the
    operator drill. An unknown backend fails fast."""
    if config.pitr_store_backend == "gcs":
        read_credentials = config.pitr_restore_gcs_credentials_file
        if read_credentials is None:
            raise RuntimeError("validated viewer-only restore proof secrets are missing")
        return (
            ("project", config.pitr_gcs_project),
            ("bucket", config.pitr_gcs_bucket),
            ("viewer_credentials", str(read_credentials)),
        )
    if config.pitr_store_backend == "cos":
        cos_credentials = config.pitr_cos_credentials_file
        if cos_credentials is None:
            raise RuntimeError("validated COS restore-proof secrets are missing")
        return (
            ("bucket", config.pitr_cos_bucket),
            ("region", config.pitr_cos_region),
            ("credentials_file", str(cos_credentials)),
            ("prefix", config.pitr_gcs_prefix),
        )
    if config.pitr_store_backend == "baidu":
        baidu_credentials = config.pitr_baidu_credentials_file
        baidu_token = config.pitr_baidu_token_file
        if baidu_credentials is None or baidu_token is None:
            raise RuntimeError("validated Baidu restore-proof secrets are missing")
        return (
            ("app_root", config.pitr_baidu_app_root),
            ("prefix", config.pitr_gcs_prefix),
            ("credentials_file", str(baidu_credentials)),
            ("token_file", str(baidu_token)),
        )
    if config.pitr_store_backend == "oss":
        # The OSS backend: the restricted worker carries only the viewer-only
        # AccessKey pair — the reader/inventory roles alone never need the
        # uploader identity.
        read_credentials = config.pitr_oss_viewer_credentials_file
        if read_credentials is None:
            raise RuntimeError("validated viewer-only restore proof secrets are missing")
        return (
            ("endpoint", config.pitr_oss_endpoint),
            ("bucket", config.pitr_oss_bucket),
            ("prefix", config.pitr_gcs_prefix),
            ("viewer_credentials_file", str(read_credentials)),
        )
    raise RuntimeError(
        f"restore proof does not know the PITR store backend {config.pitr_store_backend!r}"
    )


def input_for(candidate: CandidateManifest) -> RestoreWorkerInput:
    config = settings.physical_backup
    if not config.pitr_restore_proof_enabled:
        raise RuntimeError("restore proof cannot run while its flag is off")
    key_path = restore_key_path(config)
    store_args = restore_store_args(config)
    root = ava_home() / "physical-backup"
    logical_peak = max(
        (item.stat().st_size for item in (ava_home() / "backups" / "db").glob("*.enc")),
        default=0,
    )
    return RestoreWorkerInput(
        candidate.to_json(),
        root,
        root / "ack",
        key_path,
        config.pitr_store_backend,
        store_args,
        RestoreSpaceBudget(config.pitr_spool_hard_bytes, logical_peak, _EMERGENCY_FLOOR_BYTES),
        direct_db_url(),
        live_data_directory(),
        pg_tool("pg_ctl"),
        pg_tool("pg_verifybackup"),
    )


def live_data_directory() -> str:
    """The live instance's PGDATA, certified on the admin connection.

    The restore worker's live-identity probe runs on the runtime role
    (AVA_DB_URL), which must stay free of settings-read privileges:
    PG 17 gates `current_setting('data_directory')` behind
    pg_read_all_settings, and the 2026-08-30 activation died on exactly that
    grant gap. The controller reads the value once on the admin connection
    and hands it to the worker in its request instead."""
    from shared.cluster import get_record, record_postgres_port
    from shared.pg_admin import pg_admin_url

    record = get_record(ava_home())
    if record is None:
        raise RuntimeError("cluster registry record is missing")
    with psycopg.connect(pg_admin_url(record_postgres_port(record))) as conn:
        row = conn.execute("SELECT current_setting('data_directory')").fetchone()
    if row is None:
        raise RuntimeError("PostgreSQL omitted its data directory")
    return str(row[0])


def _request(inputs: RestoreWorkerInput) -> dict[str, object]:
    return {
        "candidate_json": inputs.candidate_json,
        "root": str(inputs.root),
        "ack_dir": str(inputs.ack_dir),
        "key_path": str(inputs.key_path),
        "backend": inputs.backend,
        "store_args": dict(inputs.store_args),
        "budget": {
            "spool_and_pg_wal_reserve": inputs.budget.spool_and_pg_wal_reserve,
            "logical_backup_peak": inputs.budget.logical_backup_peak,
            "emergency_floor": inputs.budget.emergency_floor,
        },
        "data_directory": inputs.data_directory,
        "pg_ctl": str(inputs.pg_ctl),
        "pg_verifybackup": str(inputs.pg_verifybackup),
    }


async def run_restore(candidate: CandidateManifest) -> dict[str, str]:
    return await run_restore_input(input_for(candidate))


def _secrets(inputs: RestoreWorkerInput) -> dict[str, str]:
    """The live URL may embed a password: stdin only, never the retained request."""
    return {"live_db_url": inputs.live_db_url}


async def run_restore_input(inputs: RestoreWorkerInput) -> dict[str, str]:
    completed = await run_operation(
        "services.pitr.restore_worker",
        _request(inputs),
        kind=restore_kind(inputs.root),
        env=restricted_process_env() | forwarded_proxy_env(),
        secrets=_secrets(inputs),
    )
    candidate = CandidateManifest.from_json(inputs.candidate_json)

    def accept() -> dict[str, str]:
        outcome = restore_result(completed.work / "result.json")
        retire_restore_work(
            root=inputs.root, candidate=candidate, worker=completed.worker, outcome=outcome
        )
        return outcome

    # Scratch removal is bounded local I/O; commit keeps the health loop live.
    return await completed.commit(accept)


async def run_drill_input(
    inputs: RestoreWorkerInput,
    *,
    scratch: Path,
    target_lsn: str,
    target_wall: str,
    timeout_seconds: int,
    progress: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Run one operator drill; accept only passing evidence for this candidate.

    Operator input mistakes (a relative or used scratch, a target before the
    chain start) are refused before any operation exists. `progress` receives
    the drill's own progress lines while it runs.
    """
    candidate = CandidateManifest.from_json(inputs.candidate_json)
    validate_drill_inputs(candidate, scratch, target_lsn)
    request = _request(inputs)
    request["drill"] = {
        "scratch": str(scratch),
        "target_lsn": target_lsn,
        "target_wall": target_wall,
        "timeout_seconds": timeout_seconds,
    }
    completed = await run_operation(
        "services.pitr.restore_worker",
        request,
        kind=drill_kind(inputs.root),
        env=restricted_process_env() | forwarded_proxy_env(),
        secrets=_secrets(inputs),
        progress=progress,
    )

    def accept() -> dict[str, object]:
        payload = (scratch / "drill-evidence.json").read_bytes()
        if completed.result != {"evidence_sha256": hashlib.sha256(payload).hexdigest()}:
            raise RuntimeError("drill result differs from its retained evidence")
        evidence = cast(dict[str, object], json.loads(payload))
        if evidence["outcome"] != "pass" or evidence["chain_id"] != candidate.chain_id:
            raise RuntimeError("drill did not complete the requested proof")
        return evidence

    return await completed.commit(accept)


def restore_result(result: Path) -> dict[str, str]:
    """Validate the explicit completion result before acknowledging or reaping."""
    loaded: object = json.loads(result.read_text())
    if not isinstance(loaded, dict):
        raise TypeError("restricted restore worker result must be an object")
    raw = cast(dict[str, object], loaded)
    if set(raw) != {"chain_id", "candidate_sha256", "pending_sha256"}:
        raise RuntimeError("restricted restore worker returned an invalid result")
    if any(not isinstance(value, str) or not value for value in raw.values()):
        raise RuntimeError("restricted restore worker returned an invalid result")
    return cast(dict[str, str], raw)


def publish(
    candidate: CandidateManifest,
    outcome: dict[str, str],
    *,
    require_ownership: Callable[[], None] = lambda: None,
) -> None:
    config = settings.physical_backup
    root = ava_home() / "physical-backup"
    path = root / "base-manifests" / f"{candidate.chain_id}.candidate.json"
    authoritative = CandidateManifest.from_json(path.read_text())
    candidate_digest = hashlib.sha256(authoritative.to_json().encode()).hexdigest()
    pending = root / "protected-pending" / f"{authoritative.chain_id}.json"
    if (
        authoritative != candidate
        or outcome.get("chain_id") != authoritative.chain_id
        or outcome.get("candidate_sha256") != candidate_digest
        or outcome.get("pending_sha256") != hashlib.sha256(pending.read_bytes()).hexdigest()
    ):
        raise RuntimeError("restricted restore outcome differs from authoritative evidence")
    verified, publisher = verify_then_construct_publisher(
        candidate=authoritative,
        root=root,
        ack_dir=root / "ack",
    )
    publish_candidate_proof(
        candidate=authoritative,
        root=root,
        prefix=config.pitr_gcs_prefix,
        verified=verified,
        publisher=publisher,
        require_ownership=require_ownership,
    )


publish_restore = publish


def verify_then_construct_publisher(
    *,
    candidate: CandidateManifest,
    root: Path,
    ack_dir: Path,
) -> tuple[ProtectedManifest, ProtectedManifestPublisher]:
    verified = verify_candidate_proof(candidate=candidate, root=root, ack_dir=ack_dir)
    publisher = get_store_group().protected_manifest_publisher()
    return verified, publisher
