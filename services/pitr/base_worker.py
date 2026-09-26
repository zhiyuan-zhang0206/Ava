"""One foreground base-candidate operation for schedules and activation."""

from __future__ import annotations

import asyncio
import sys
import threading

from services.pitr.base_candidate import commit_base_candidate, prepare_base_candidate
from services.pitr.base_manifest import CandidateManifest
from services.pitr.space_budget import CandidateSpaceBudget
from services.pitr.store_factory import get_store_group
from services.pitr.worker_process import (
    StopSignal,
    publish_result,
    run_operation,
    worker_request,
)
from shared.config import settings
from shared.paths import ava_home
from shared.platform import LockTimeoutError
from shared.process_env import inherited_process_env

# Both backup-lock acquisitions in `prepare_base_candidate` precede any
# candidate evidence, so a busy lock is a clean deferral, not a failed operation.
_DEFERRED = {"deferred": "backup_lock"}


def _prepare(chain_id: str | None) -> CandidateManifest:
    config = settings.physical_backup
    if chain_id is None and not config.pitr_base_backup_enabled:
        raise RuntimeError("base candidate scheduler cannot run while its flag is off")
    if chain_id is not None and not config.pitr_enabled:
        raise RuntimeError("activation candidate requires enabled WAL archival")
    key = config.pitr_backup_key_file
    if key is None or config.pitr_gcs_credentials_file is None:
        raise RuntimeError("validated PITR secrets are missing")
    home = ava_home()
    pgdata_bytes = sum(item.stat().st_size for item in (home / "pg").rglob("*") if item.is_file())
    logical_peak = max(
        (item.stat().st_size for item in (home / "backups" / "db").glob("*.enc")), default=0
    )
    return prepare_base_candidate(
        root=home / "physical-backup",
        prefix=config.pitr_gcs_prefix,
        key=key.read_bytes(),
        key_id=config.pitr_backup_key_id,
        store=get_store_group().restartable_streaming_object_store(),
        budget=CandidateSpaceBudget(
            pgdata_bytes, config.pitr_spool_hard_bytes, logical_peak, 4 * 1024**3
        ),
        replication_db_url=config.pitr_replication_db_url,
        stop=threading.Event(),
        forced_chain_id=chain_id,
    )


async def run_candidate(
    *, chain_id: str | None = None, stop: StopSignal | None = None
) -> CandidateManifest:
    """Commit a prepared candidate only after its worker group closed cleanly."""
    root = ava_home() / "physical-backup"
    completed = await run_operation(
        "services.pitr.base_worker",
        {"chain_id": chain_id},
        control_root=root / "base-control",
        env=inherited_process_env(),
        stop=stop,
    )
    if completed.result == _DEFERRED:
        completed.retire()
        raise LockTimeoutError("base candidate deferred while another backup owns the lock")
    if set(completed.result) != {"candidate_json"} or not isinstance(
        completed.result["candidate_json"], str
    ):
        raise RuntimeError(f"base worker returned an invalid candidate result: {completed.work}")
    candidate = CandidateManifest.from_json(completed.result["candidate_json"])
    if chain_id is not None and candidate.chain_id != chain_id:
        raise RuntimeError("base worker result differs from the requested activation chain")
    if stop is not None and stop.is_set():
        raise RuntimeError("base candidate lost ownership before controller commit")
    # Re-hashing the prepared tree is bounded local I/O; keep the health loop live.
    await asyncio.to_thread(commit_base_candidate, root, candidate, completed.worker)
    completed.retire()
    return candidate


def main() -> None:
    request, output = worker_request(sys.argv)
    if set(request) != {"chain_id"}:
        raise ValueError("invalid base worker request")
    chain_id = request["chain_id"]
    if chain_id is not None and not isinstance(chain_id, str):
        raise TypeError("invalid base worker chain")
    try:
        candidate = _prepare(chain_id)
    except LockTimeoutError:
        publish_result(output, dict(_DEFERRED))
        return
    publish_result(output, {"candidate_json": candidate.to_json()})


if __name__ == "__main__":
    main()
