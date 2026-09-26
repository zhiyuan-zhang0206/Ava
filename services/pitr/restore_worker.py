"""Restricted restore-proof worker entrypoint with no publication authority."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any, cast

from services.pitr.base_manifest import CandidateManifest
from services.pitr.operation_custody import publish_result
from services.pitr.restore_object_store import GenerationPinnedObjectReader
from services.pitr.restore_postgres import IsolatedPostgresRestoreExecutor
from services.pitr.restore_proof import (
    RestoreProofDeferredError,
    RestoreSpaceBudget,
    prove_candidate,
)
from services.pitr.store_factory import construct_store_group
from services.pitr.worker_process import worker_request, worker_secrets


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("restore worker input must be an object")
    return cast(dict[str, Any], value)


def _string_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise TypeError("restore worker store args must be an object")
    return {str(key): str(item) for key, item in cast(dict[str, object], value).items()}


def _progress(message: str) -> None:
    """Drill progress: one unbuffered stderr line the controller forwards."""
    sys.stderr.write(f"{message}\n")
    sys.stderr.flush()


def run(raw: dict[str, Any], output_path: Path, secrets: dict[str, str]) -> None:
    expected = {
        "candidate_json",
        "root",
        "ack_dir",
        "key_path",
        "backend",
        "store_args",
        "budget",
        "data_directory",
        "pg_ctl",
        "pg_verifybackup",
    }
    if set(raw) not in (expected, expected | {"drill"}):
        raise ValueError("restore worker input fields differ from the restricted protocol")
    if set(secrets) != {"live_db_url"}:
        raise ValueError("restore worker secrets differ from the restricted protocol")
    candidate = CandidateManifest.from_json(str(raw["candidate_json"]))
    budget = _object(raw["budget"])
    group = construct_store_group(str(raw["backend"]), _string_map(raw["store_args"]))
    if "drill" in raw:
        _run_drill(raw, secrets, candidate, group.generation_pinned_object_reader(), output_path)
        return
    try:
        protected = prove_candidate(
            candidate=candidate,
            root=Path(str(raw["root"])),
            ack_dir=Path(str(raw["ack_dir"])),
            key=Path(str(raw["key_path"])).read_bytes(),
            reader=group.generation_pinned_object_reader(),
            executor=IsolatedPostgresRestoreExecutor(
                live_db_url=secrets["live_db_url"],
                data_directory=str(raw["data_directory"]),
                pg_ctl=Path(str(raw["pg_ctl"])),
                pg_verifybackup=Path(str(raw["pg_verifybackup"])),
            ),
            budget=RestoreSpaceBudget(
                int(budget["spool_and_pg_wal_reserve"]),
                int(budget["logical_backup_peak"]),
                int(budget["emergency_floor"]),
            ),
        )
    except RestoreProofDeferredError as exc:  # Raised before any restore evidence exists.
        publish_result(output_path, {"deferred": "space", "detail": str(exc)})
        return
    pending = Path(str(raw["root"])) / "protected-pending" / f"{candidate.chain_id}.json"
    payload = pending.read_bytes()
    result = {
        "chain_id": candidate.chain_id,
        "candidate_sha256": protected.candidate_sha256,
        "pending_sha256": hashlib.sha256(payload).hexdigest(),
    }
    publish_result(output_path, result)


def _run_drill(
    raw: dict[str, Any],
    secrets: dict[str, str],
    candidate: CandidateManifest,
    reader: GenerationPinnedObjectReader,
    output: Path,
) -> None:
    from services.pitr.restore_drill import DrillRequest, parse_target_wall, run_restore_drill

    drill = _object(raw["drill"])
    if set(drill) != {"scratch", "target_lsn", "target_wall", "timeout_seconds"}:
        raise ValueError("invalid drill worker input")
    scratch = Path(str(drill["scratch"]))
    run_restore_drill(
        DrillRequest(
            candidate=candidate,
            reader=reader,
            key=Path(str(raw["key_path"])).read_bytes(),
            ack_dir=Path(str(raw["ack_dir"])),
            scratch=scratch,
            target_lsn=str(drill["target_lsn"]),
            target_wall=parse_target_wall(str(drill["target_wall"])),
            pg_ctl=Path(str(raw["pg_ctl"])),
            pg_verifybackup=Path(str(raw["pg_verifybackup"])),
            live_db_url=secrets["live_db_url"],
            data_directory=str(raw["data_directory"]),
            timeout_seconds=int(drill["timeout_seconds"]),
        ),
        progress=_progress,
    )
    payload = (scratch / "drill-evidence.json").read_bytes()
    publish_result(output, {"evidence_sha256": hashlib.sha256(payload).hexdigest()})


def main() -> None:
    request, output = worker_request(sys.argv)
    run(request, output, worker_secrets())


if __name__ == "__main__":
    main()
