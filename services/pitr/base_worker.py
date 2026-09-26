"""One foreground base-candidate operation for schedules and activation."""

from __future__ import annotations

import os
import sys
import threading
from contextlib import suppress
from functools import partial
from pathlib import Path

from services.pitr.base_candidate import (
    commit_base_candidate,
    prepare_base_candidate,
    quarantine_candidate_staging,
)
from services.pitr.base_manifest import CandidateManifest
from services.pitr.operation_custody import OperationDeferred, OperationKind, publish_result
from services.pitr.space_budget import CandidateSpaceBudget, InsufficientCandidateSpaceError
from services.pitr.store_factory import get_store_group
from services.pitr.worker_process import (
    CompletedOperation,
    StopSignal,
    run_operation,
    worker_request,
)
from shared.config import settings
from shared.paths import ava_home
from shared.platform import LockTimeoutError
from shared.process_env import inherited_process_env


def candidate_kind(root: Path | None = None) -> OperationKind:
    """Base candidates for the weekly schedule and for activation share one root."""
    root = ava_home() / "physical-backup" if root is None else root
    return OperationKind(
        "base-candidate",
        root / "base-control",
        root / "quarantine",
        partial(quarantine_candidate_staging, root),
    )


def _tree_bytes(path: Path) -> int:
    """Live PGDATA size; files may vanish while it is walked."""
    total = 0
    for directory, _dirs, files in os.walk(path):
        for name in files:
            with suppress(FileNotFoundError):
                total += (Path(directory) / name).stat().st_size
    return total


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
            _tree_bytes(home / "pg"), config.pitr_spool_hard_bytes, logical_peak, 4 * 1024**3
        ),
        replication_db_url=config.pitr_replication_db_url,
        stop=threading.Event(),
        forced_chain_id=chain_id,
    )


async def run_candidate(
    *, chain_id: str | None = None, stop: StopSignal | None = None
) -> CandidateManifest:
    """Commit a prepared candidate only after its worker group closed cleanly.

    A busy backup lock defers as `LockTimeoutError`, missing space as
    `OperationDeferred`; neither leaves evidence behind.
    """
    root = ava_home() / "physical-backup"
    try:
        completed = await run_operation(
            "services.pitr.base_worker",
            {"chain_id": chain_id},
            kind=candidate_kind(root),
            env=inherited_process_env(),
            stop=stop,
        )
    except OperationDeferred as deferred:
        if deferred.reason == "backup_lock":
            raise LockTimeoutError(deferred.detail) from deferred
        raise
    # Re-hashing the prepared tree is bounded local I/O off the health loop.
    return await completed.commit(partial(_commit, root, completed, chain_id, stop))


def _commit(
    root: Path, completed: CompletedOperation, chain_id: str | None, stop: StopSignal | None
) -> CandidateManifest:
    result = completed.result
    if set(result) != {"candidate_json"} or not isinstance(result["candidate_json"], str):
        raise RuntimeError("base worker returned an invalid candidate result")
    candidate = CandidateManifest.from_json(result["candidate_json"])
    if chain_id is not None and candidate.chain_id != chain_id:
        raise RuntimeError("base worker result differs from the requested activation chain")
    if stop is not None and stop.is_set():
        raise RuntimeError("base candidate lost ownership before controller commit")
    commit_base_candidate(root, candidate, completed.worker)
    return candidate


def main() -> None:
    request, output = worker_request(sys.argv)
    if set(request) != {"chain_id"}:
        raise ValueError("invalid base worker request")
    chain_id = request["chain_id"]
    if chain_id is not None and not isinstance(chain_id, str):
        raise TypeError("invalid base worker chain")
    # Both deferrals are raised before `_record_owner`: no evidence exists yet.
    try:
        candidate = _prepare(chain_id)
    except LockTimeoutError as exc:
        publish_result(output, {"deferred": "backup_lock", "detail": str(exc) or "busy"})
        return
    except InsufficientCandidateSpaceError as exc:
        publish_result(output, {"deferred": "space", "detail": str(exc)})
        return
    publish_result(output, {"candidate_json": candidate.to_json()})


if __name__ == "__main__":
    main()
