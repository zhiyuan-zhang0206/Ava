"""PITR inspection and rollback-snapshot archive commands."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

from services.pitr.base_manifest import CandidateManifest
from services.pitr.retention_planner import inspect_dry_run_plan
from services.pitr.rollback_snapshot_archive import (
    RollbackSnapshotArchive,
    archive_rollback_snapshot,
    drop_rollback_snapshot_table,
    export_rollback_snapshot_table,
    restore_rollback_snapshot_table,
    retire_rollback_snapshot,
    verify_rollback_snapshot,
)
from services.pitr.store_factory import construct_store_group, get_store_group
from shared.config import settings
from shared.db import direct_db_url
from shared.paths import ava_home
from shared.pg_tools import pg_tool


def cmd_pitr_retention_inspect() -> int:
    plan = inspect_dry_run_plan(ava_home() / "physical-backup")
    print(
        json.dumps(
            {
                "plan_digest": plan.digest(),
                "blocked_reasons": plan.blocked_reasons,
                "protected_chain_ids": plan.protected_chain_ids,
                "unprotected_chain_ids": plan.unprotected_chain_ids,
                "oldest_retained_chain_id": plan.oldest_retained_chain_id,
                "ack_high_water": plan.ack_high_water,
                "retained_objects": len(plan.retained),
                "eligible_objects": len(plan.eligible),
                "retained_bytes": plan.retained_bytes,
                "eligible_bytes": plan.eligible_bytes,
                "delete_enabled": False,
            },
            sort_keys=True,
        )
    )
    return 0 if not plan.blocked_reasons else 2


def cmd_pitr_snapshot_archive(table: str) -> int:
    """Export and publish one finite migration rollback snapshot."""
    return _run_snapshot_action(
        "archive",
        lambda: archive_rollback_snapshot(
            table,
            ava_home=ava_home(),
            key=_pitr_backup_key(),
            key_id=settings.physical_backup.pitr_backup_key_id,
            export_table=export_rollback_snapshot_table,
            store=get_store_group().object_store(),
        ),
    )


def cmd_pitr_snapshot_verify(table: str) -> int:
    """Restore one archived rollback snapshot into disposable PostgreSQL."""
    return _run_snapshot_action(
        "verify",
        lambda: verify_rollback_snapshot(
            table,
            ava_home=ava_home(),
            key=_pitr_backup_key(),
            reader=get_store_group().generation_pinned_object_reader(),
            restore_drill=restore_rollback_snapshot_table,
        ),
    )


def cmd_pitr_snapshot_retire(table: str) -> int:
    """Drop one rollback snapshot after its exact archived generation is proven."""
    return _run_snapshot_action(
        "retire",
        lambda: retire_rollback_snapshot(
            table,
            ava_home=ava_home(),
            drop_table=drop_rollback_snapshot_table,
        ),
    )


def _run_snapshot_action(action: str, run: Callable[[], RollbackSnapshotArchive]) -> int:
    """Report snapshot command failures without exposing a Python traceback."""
    try:
        record = run()
    except Exception as exc:
        print(f"pitr snapshot {action} failed: {exc}", file=sys.stderr)
        return 1
    print(record.to_json())
    return 0


def _pitr_backup_key() -> bytes:
    path = settings.physical_backup.pitr_backup_key_file
    if path is None:
        raise RuntimeError("PITR backup key file is not configured")
    return path.read_bytes()


def cmd_pitr_drill(
    *,
    chain: str | None,
    candidate: str | None,
    target_lsn: str,
    target_wall: str,
    scratch: str,
    timeout_seconds: int,
) -> int:
    """Restore one protected chain to an operator target in isolation.

    The scratch tree is kept as evidence on every outcome; the drill never
    publishes and never writes to the live cluster. The service imports are
    method-local on purpose: `cli.commands` sits in the agent-runner
    updater's pre-checkout import closure, and the drill's modules reach
    `shared.session_record`, which must stay outside it
    (`tests/cli/test_update_import_timing.py`).
    """
    from services.pitr.base_operation_runtime import (
        live_data_directory,
        restore_key_path,
        restore_store_args,
    )
    from services.pitr.restore_drill import DrillRequest, parse_target_wall, run_restore_drill

    try:
        candidate_manifest = _resolve_drill_candidate(chain, candidate)
        config = settings.physical_backup
        group = construct_store_group(config.pitr_store_backend, dict(restore_store_args(config)))
        request = DrillRequest(
            candidate=candidate_manifest,
            reader=group.generation_pinned_object_reader(),
            key=restore_key_path(config).read_bytes(),
            ack_dir=ava_home() / "physical-backup" / "ack",
            scratch=Path(scratch),
            target_lsn=target_lsn,
            target_wall=parse_target_wall(target_wall),
            pg_ctl=pg_tool("pg_ctl"),
            pg_verifybackup=pg_tool("pg_verifybackup"),
            live_db_url=direct_db_url(),
            data_directory=live_data_directory(),
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:
        print(f"pitr drill failed before start: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "chain_id": request.candidate.chain_id,
                "target_lsn": request.target_lsn,
                "target_wall": request.target_wall.isoformat(),
                "scratch": str(request.scratch),
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    evidence_path = request.scratch / "drill-evidence.json"
    try:
        evidence = run_restore_drill(
            request, progress=lambda message: print(message, file=sys.stderr)
        )
    except Exception as exc:
        print(f"pitr drill failed: {exc} (evidence: {evidence_path})", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "outcome": evidence.outcome,
                "chain_id": evidence.chain_id,
                "target_lsn": evidence.target_lsn,
                "evidence": str(evidence_path),
                "counts_restored": evidence.criteria.get("counts_restored"),
                "counts_live": evidence.criteria.get("counts_live"),
                "timings": evidence.timings,
            },
            sort_keys=True,
        )
    )
    return 0


def _resolve_drill_candidate(chain: str | None, candidate: str | None) -> CandidateManifest:
    if (chain is None) == (candidate is None):
        raise ValueError("pass exactly one of --chain or --candidate")
    if candidate is not None:
        return CandidateManifest.from_json(Path(candidate).read_text())
    path = ava_home() / "physical-backup" / "base-manifests" / f"{chain}.candidate.json"
    if not path.is_file():
        raise ValueError(f"no candidate manifest for chain {chain!r} at {path}")
    return CandidateManifest.from_json(path.read_text())
