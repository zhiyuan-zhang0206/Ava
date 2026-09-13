"""Restricted restore-proof worker entrypoint with no publication authority."""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any, cast

from services.pitr.base_manifest import CandidateManifest
from services.pitr.restore_postgres import IsolatedPostgresRestoreExecutor
from services.pitr.restore_proof import RestoreSpaceBudget, prove_candidate
from services.pitr.store_factory import construct_store_group


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("restore worker input must be an object")
    return cast(dict[str, Any], value)


def _string_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise TypeError("restore worker store args must be an object")
    return {str(key): str(item) for key, item in cast(dict[str, object], value).items()}


def run(input_path: Path, output_path: Path) -> None:
    raw = _object(json.loads(input_path.read_text()))
    expected = {
        "candidate_json",
        "root",
        "ack_dir",
        "key_path",
        "backend",
        "store_args",
        "budget",
        "live_db_url",
        "data_directory",
        "pg_ctl",
        "pg_verifybackup",
    }
    if set(raw) != expected:
        raise ValueError("restore worker input fields differ from the restricted protocol")
    candidate = CandidateManifest.from_json(str(raw["candidate_json"]))
    budget = _object(raw["budget"])
    group = construct_store_group(str(raw["backend"]), _string_map(raw["store_args"]))
    protected = prove_candidate(
        candidate=candidate,
        root=Path(str(raw["root"])),
        ack_dir=Path(str(raw["ack_dir"])),
        key=Path(str(raw["key_path"])).read_bytes(),
        reader=group.generation_pinned_object_reader(),
        executor=IsolatedPostgresRestoreExecutor(
            live_db_url=str(raw["live_db_url"]),
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
    pending = Path(str(raw["root"])) / "protected-pending" / f"{candidate.chain_id}.json"
    payload = pending.read_bytes()
    result = {
        "chain_id": candidate.chain_id,
        "candidate_sha256": protected.candidate_sha256,
        "pending_sha256": hashlib.sha256(payload).hexdigest(),
    }
    output_path.write_text(json.dumps(result, sort_keys=True, separators=(",", ":")))
    acknowledgement = output_path.with_suffix(".ack")
    deadline = time.monotonic() + 60
    while not acknowledgement.is_file():
        if time.monotonic() >= deadline:
            raise TimeoutError("restore controller did not acknowledge the worker result")
        time.sleep(0.05)


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: python -m services.pitr.restore_worker INPUT OUTPUT")
    run(Path(sys.argv[1]), Path(sys.argv[2]))


if __name__ == "__main__":
    main()
