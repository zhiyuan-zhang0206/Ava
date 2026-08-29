"""Operation-scoped base candidate and restore proof for PITR activation."""

from __future__ import annotations

from services.pitr.base_candidate import create_base_candidate
from services.pitr.base_manifest import CandidateManifest
from services.pitr.base_object_store import GCSRestartableStreamingObjectStore
from services.pitr.restore_manifest import ProtectedManifest
from services.pitr.space_budget import CandidateSpaceBudget
from shared.config import settings
from shared.paths import ava_home

_EMERGENCY_FLOOR_BYTES = 4 * 1024**3


def build_activation_candidate(*, operation_id: str, chain_id: str) -> CandidateManifest:
    from services.pitr.base_scheduler_daemon import _tree_bytes

    config = settings.physical_backup
    if not chain_id.endswith(f"-{operation_id}"):
        raise RuntimeError("activation candidate chain differs from operation")
    root = ava_home() / "physical-backup"
    manifest_path = root / "base-manifests" / f"{chain_id}.candidate.json"
    if manifest_path.is_file():
        candidate = CandidateManifest.from_json(manifest_path.read_text())
        if candidate.chain_id != chain_id:
            raise RuntimeError("durable activation candidate differs from intent")
        return candidate
    if not config.pitr_enabled:
        raise RuntimeError("activation candidate requires enabled WAL archival")
    key_path, credentials = config.pitr_backup_key_file, config.pitr_gcs_credentials_file
    if key_path is None or credentials is None:
        raise RuntimeError("validated PITR secrets are missing")
    logical_peak = max(
        (item.stat().st_size for item in (ava_home() / "backups" / "db").glob("*.enc")),
        default=0,
    )
    return create_base_candidate(
        root=root,
        prefix=config.pitr_gcs_prefix,
        key=key_path.read_bytes(),
        key_id=config.pitr_backup_key_id,
        store=GCSRestartableStreamingObjectStore(
            project=config.pitr_gcs_project,
            bucket=config.pitr_gcs_bucket,
            credentials_file=str(credentials),
        ),
        budget=CandidateSpaceBudget(
            compressed_staging_estimate=_tree_bytes(ava_home() / "pg"),
            spool_and_pg_wal_reserve=config.pitr_spool_hard_bytes,
            logical_backup_peak_reserve=logical_peak,
            emergency_floor=_EMERGENCY_FLOOR_BYTES,
        ),
        replication_db_url=config.pitr_replication_db_url,
        forced_chain_id=chain_id,
    )


async def restore_activation_candidate(candidate: CandidateManifest) -> ProtectedManifest:
    from services.pitr.base_scheduler_daemon import (
        _publish_restore_proof,
        _restore_worker_input_for,
        _run_restore_worker,
    )

    outcome = await _run_restore_worker(_restore_worker_input_for(candidate))
    _publish_restore_proof(candidate, outcome)
    path = ava_home() / "physical-backup" / "protected-manifests" / f"{candidate.chain_id}.json"
    return ProtectedManifest.from_json(path.read_text())
