"""PITR inspection and rollback-snapshot archive commands."""

from __future__ import annotations

import json

from services.pitr.retention_planner import inspect_dry_run_plan
from services.pitr.rollback_snapshot_archive import (
    archive_rollback_snapshot,
    drop_rollback_snapshot_table,
    export_rollback_snapshot_table,
    restore_rollback_snapshot_table,
    retire_rollback_snapshot,
    verify_rollback_snapshot,
)
from services.pitr.store_factory import get_store_group
from shared.config import settings
from shared.paths import ava_home


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
    record = archive_rollback_snapshot(
        table,
        ava_home=ava_home(),
        key=_pitr_backup_key(),
        key_id=settings.physical_backup.pitr_backup_key_id,
        export_table=export_rollback_snapshot_table,
        store=get_store_group().object_store(),
    )
    print(record.to_json())
    return 0


def cmd_pitr_snapshot_verify(table: str) -> int:
    """Restore one archived rollback snapshot into disposable PostgreSQL."""
    record = verify_rollback_snapshot(
        table,
        ava_home=ava_home(),
        key=_pitr_backup_key(),
        reader=get_store_group().generation_pinned_object_reader(),
        restore_drill=restore_rollback_snapshot_table,
    )
    print(record.to_json())
    return 0


def cmd_pitr_snapshot_retire(table: str) -> int:
    """Drop one rollback snapshot after its exact archived generation is proven."""
    record = retire_rollback_snapshot(
        table,
        ava_home=ava_home(),
        drop_table=drop_rollback_snapshot_table,
    )
    print(record.to_json())
    return 0


def _pitr_backup_key() -> bytes:
    path = settings.physical_backup.pitr_backup_key_file
    if path is None:
        raise RuntimeError("PITR backup key file is not configured")
    return path.read_bytes()
